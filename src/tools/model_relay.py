"""Task-scoped model relay capability.

The relay is the ONLY path from the student guest to model inference. It is a
narrow capability, not a proxy: explicit allowed operations, per-task quota and
deadline, bounded request/response, task binding, idempotent revocation on
terminal state, and fail-closed transport errors. The upstream credential lives
only inside the operator-owned transport callable and is never observable from
the capability, the payloads, or the evidence snapshot.

Threat model and contract: docs/student-vm-boundary.md,
"Task-scoped model relay".
"""

import re
import threading
import time
from typing import Any, Callable, Protocol

__all__ = [
    "ModelRelayBlocked",
    "RelayPolicy",
    "RelayTransport",
    "TaskScopedModelRelay",
]

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,99}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")


class ModelRelayBlocked(Exception):
    """The relay refused or failed; the task blocks. No direct-model fallback."""


class RelayTransport(Protocol):
    """Operator-owned transport holding the upstream credential.

    Exactly one upstream endpoint is pinned inside the implementation; the
    capability surface carries no URL, method, or header parameter, so the
    relay cannot be steered at arbitrary targets (no SSRF surface).
    """

    def __call__(self, operation: str, payload: bytes) -> bytes: ...


class RelayPolicy:
    """Immutable per-task relay limits. All values are operator-controlled."""

    def __init__(
        self,
        *,
        task_id: str,
        allowed_operations: tuple[str, ...],
        max_requests: int,
        max_request_bytes: int,
        max_response_bytes: int,
        max_total_bytes: int,
        max_duration_seconds: int,
    ) -> None:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is invalid: {task_id!r}")
        if not isinstance(allowed_operations, tuple) or not allowed_operations:
            raise ValueError("allowed_operations must be a non-empty tuple")
        for operation in allowed_operations:
            if not isinstance(operation, str) or not _OPERATION_RE.fullmatch(operation):
                raise ValueError(f"allowed operation is invalid: {operation!r}")
        if len(set(allowed_operations)) != len(allowed_operations):
            raise ValueError("allowed_operations must not contain duplicates")
        for name, value in (
            ("max_requests", max_requests),
            ("max_request_bytes", max_request_bytes),
            ("max_response_bytes", max_response_bytes),
            ("max_total_bytes", max_total_bytes),
            ("max_duration_seconds", max_duration_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        self.task_id = task_id
        self.allowed_operations = allowed_operations
        self.max_requests = max_requests
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.max_total_bytes = max_total_bytes
        self.max_duration_seconds = max_duration_seconds

    def __repr__(self) -> str:
        return (
            f"RelayPolicy(task_id={self.task_id!r}, "
            f"allowed_operations={self.allowed_operations!r}, "
            f"max_requests={self.max_requests}, "
            f"max_request_bytes={self.max_request_bytes}, "
            f"max_response_bytes={self.max_response_bytes}, "
            f"max_total_bytes={self.max_total_bytes}, "
            f"max_duration_seconds={self.max_duration_seconds})"
        )


class TaskScopedModelRelay:
    """One task's narrow model capability. Usage order is deliberate:

    revoked? -> expired? -> task binding -> operation scope -> request bound ->
    quota -> transport -> response bound. Every refusal happens before the
    transport is touched except response-bound rejection, which fails closed
    after the fact (the upstream cost is spent; the output is discarded).
    """

    def __init__(
        self,
        *,
        task_id: str,
        policy: RelayPolicy,
        transport: RelayTransport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(policy, RelayPolicy):
            raise ValueError("policy must be a RelayPolicy")
        if task_id != policy.task_id:
            raise ValueError(
                f"task_id {task_id!r} does not match the policy binding {policy.task_id!r}"
            )
        if not callable(transport):
            raise ValueError("transport must be callable")
        self.policy = policy
        self._transport = transport
        self._clock = clock
        self._deadline = clock() + policy.max_duration_seconds
        self._lock = threading.Lock()
        self._requests_used = 0
        self._bytes_used = 0
        self._revoked = False
        self._revoke_reason = ""

    def request(self, *, task_id: str, operation: str, payload: bytes) -> bytes:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ValueError(f"payload must be bytes-like, got {type(payload).__name__}")
        if not isinstance(operation, str):
            raise ValueError(f"operation must be a str, got {type(operation).__name__}")
        data = bytes(payload)

        with self._lock:
            if self._revoked:
                raise ModelRelayBlocked(f"relay revoked: {self._revoke_reason}")
            if self._clock() >= self._deadline:
                self._revoke_locked("deadline exceeded")
                raise ModelRelayBlocked("relay revoked: deadline exceeded")
            if task_id != self.policy.task_id:
                raise ModelRelayBlocked(
                    f"relay is scoped to task {self.policy.task_id!r}, "
                    f"refusing call for task {task_id!r}"
                )
            if operation not in self.policy.allowed_operations:
                raise ModelRelayBlocked(f"operation not allowed: {operation!r}")
            if len(data) > self.policy.max_request_bytes:
                raise ModelRelayBlocked(
                    f"request exceeds configured size limit: "
                    f"{len(data)} > {self.policy.max_request_bytes}"
                )
            if self._requests_used >= self.policy.max_requests:
                raise ModelRelayBlocked("relay request quota exhausted")
            if self._bytes_used + len(data) > self.policy.max_total_bytes:
                raise ModelRelayBlocked("relay byte quota exhausted")

            self._requests_used += 1
            # Charge request bytes at admission: once the transport is called
            # the upstream spend is committed, even if the response is later
            # refused on budget grounds.
            self._bytes_used += len(data)

        # Transport runs outside the lock but the attempt is already counted:
        # an exhausted upstream still consumed a request.
        try:
            response = self._transport(operation, data)
        except Exception as exc:  # noqa: BLE001 — every transport failure blocks
            raise ModelRelayBlocked(f"relay transport failed: {type(exc).__name__}") from exc

        if not isinstance(response, (bytes, bytearray, memoryview)):
            raise ModelRelayBlocked(
                f"relay transport returned a non-bytes response: {type(response).__name__}"
            )
        out = bytes(response)
        if not out:
            # Fail closed: an upstream that answers HTTP 200 with an empty body
            # is an upstream FAULT, not an inference. Returning b"" would hand
            # the guest an empty result indistinguishable from a real one, so
            # the task would read as succeeded while nothing was produced.
            raise ModelRelayBlocked("relay transport returned an empty response")
        if len(out) > self.policy.max_response_bytes:
            # Fail closed: a truncated model answer would masquerade as a
            # complete one. Discard it entirely.
            raise ModelRelayBlocked(
                f"response exceeds configured size limit: "
                f"{len(out)} > {self.policy.max_response_bytes}"
            )

        with self._lock:
            if self._bytes_used + len(out) > self.policy.max_total_bytes:
                # Hard cap: refuse the whole response rather than hand back a
                # partial answer or let the budget silently overshoot.
                raise ModelRelayBlocked(
                    "response would exceed the total byte budget"
                )
            self._bytes_used += len(out)
        return out

    def revoke(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("revoke reason must be a non-empty string")
        with self._lock:
            self._revoke_locked(reason)

    def _revoke_locked(self, reason: str) -> None:
        if self._revoked:
            return  # idempotent: the first reason stands
        self._revoked = True
        self._revoke_reason = reason

    def snapshot(self) -> dict[str, Any]:
        """Bounded, redacted usage evidence. Never contains payload or response
        content — only counters, limits, and revocation state."""
        with self._lock:
            expired = self._clock() >= self._deadline
            return {
                "task_id": self.policy.task_id,
                "requests_used": self._requests_used,
                "requests_remaining": max(
                    0, self.policy.max_requests - self._requests_used
                ),
                "bytes_used": self._bytes_used,
                "bytes_remaining": max(
                    0, self.policy.max_total_bytes - self._bytes_used
                ),
                "revoked": self._revoked,
                "revoke_reason": self._revoke_reason,
                "expired": expired,
            }
