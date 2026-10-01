"""OmniRoute-backed transport for the task-scoped model relay.

This is the missing bridge. `model_relay.RelayTransport` is a one-function
capability -- `__call__(operation, payload) -> bytes` -- and until now nothing
implemented it, so the relay could be tested with a stub but never run.

What this module adds, and why each part is load-bearing:

  * The endpoint and the model are pinned at construction. There is no URL,
    method, or model parameter on the request path, so the guest cannot steer
    inference at an arbitrary target or swap in a different model. That is the
    no-SSRF property the relay contract promises.
  * The upstream credential lives only in this object. It is never placed in
    the request body, never in `repr`, and never in the evidence snapshot.
  * Every fault raises `TransportError`. The relay turns any exception into
    `ModelRelayBlocked`, so raising here is what keeps the path fail-closed.

Three upstream behaviours are treated as FAULTS, not answers, because each one
silently masquerades as a successful inference:

  * an empty body behind HTTP 200 (observed from the Longcat route)
  * `finish_reason: length` -- a truncated answer is not a complete one
  * `finish_reason: tool_calls` with no content -- the model wanted to read the
    repo and was not served. Returning b"" here is what made a healthy free
    model look incapable during qualification.

Live-network use goes through `tests/test_omni_relay_live.py` (opt-in). The
tests in `tests/test_omni_relay.py` are hermetic.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "OmniRouteTransport",
    "TransportError",
    "build_completion_request",
]

#: Operations the relay may ask the upstream for. Anything else is refused
#: before a socket is opened, so the operation surface stays as narrow as the
#: capability contract requires.
ALLOWED_OPERATIONS = frozenset({"complete"})

#: Header names whose values are replaced in any evidence or debug output.
_SECRET_KEYS = frozenset({
    "authorization", "api_key", "apikey", "x-api-key", "token", "access_token",
})


class TransportError(Exception):
    """The upstream could not be used. Always fatal; never a fallback.

    `model_relay.TaskScopedModelRelay` converts any exception into
    `ModelRelayBlocked`, so raising here is what keeps the path fail-closed
    instead of handing the guest an empty or truncated answer.
    """


def _redact(obj: Any) -> Any:
    """Return a copy of `obj` with secret-bearing header values replaced.

    Used for evidence snapshots and error context. Payload content is left
    intact: it is the task's own data, not a credential.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def build_completion_request(
    *, operation: str, prompt: str, model: str, max_tokens: int = 4000,
) -> dict[str, Any]:
    """Build the upstream request body.

    Split out so the shape can be tested without a socket. The model is an
    explicit argument so the caller cannot smuggle one in through the payload,
    and the transport always passes its own pinned value.
    """
    if operation not in ALLOWED_OPERATIONS:
        raise TransportError(f"operation not permitted: {operation!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise TransportError("prompt must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise TransportError("model must be a non-empty string")
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "temperature": 0.0,
    }


class OmniRouteTransport:
    """A `RelayTransport` that calls one pinned OmniRoute endpoint.

    The credential is supplied by the operator (or read from the environment)
    and is never observable from the capability surface.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "openrouter/inclusionai/ling-3.0-flash-sante:free",
        base_url: str = "http://localhost:20128",
        timeout: float = 180.0,
        max_tokens: int = 4000,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        self._api_key = api_key or ""
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        #: Both are recorded on every call so a substituted model is visible
        #: in evidence rather than hidden behind a friendly route name.
        self.requested_model = model
        self.returned_model: str | None = None
        self.last_usage: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"OmniRouteTransport(model={self.model!r}, "
            f"base_url={self.base_url!r}, "
            f"credential={'set' if self._api_key else 'unset'})"
        )

    @property
    def evidence(self) -> dict[str, Any]:
        """Bounded, redacted usage evidence for a run record."""
        return _redact({
            "requested_model": self.requested_model,
            "returned_model": self.returned_model,
            "base_url": self.base_url,
            "credential": "[REDACTED]" if self._api_key else "unset",
            "usage": self.last_usage,
        })

    def _open(self, req: urllib.request.Request, timeout: float):
        """Indirection so tests can inject a fake response object."""
        return urllib.request.urlopen(req, timeout=timeout)

    def __call__(self, operation: str, payload: bytes) -> bytes:
        """Perform one completion. Any fault raises `TransportError`."""
        if not self._api_key:
            raise TransportError("no upstream credential configured")

        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TransportError(
                f"payload must be bytes-like, got {type(payload).__name__}")
        try:
            prompt = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportError("payload is not valid UTF-8") from exc

        body = build_completion_request(
            operation=operation,
            prompt=prompt,
            model=self.model,          # pinned, never caller-supplied
            max_tokens=self.max_tokens,
        )
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        try:
            with self._open(req, self.timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Never echo the credential or the full body into the message.
            raise TransportError(
                f"upstream HTTP {exc.code}") from None
        except Exception as exc:  # noqa: BLE001 — every fault is fatal
            raise TransportError(
                f"upstream transport failure: {type(exc).__name__}") from None

        if status >= 400:
            raise TransportError(f"upstream HTTP {status}")

        if not raw:
            # HTTP 200 with no body. Observed from the Longcat route. This is
            # an upstream fault, NOT an inference.
            raise TransportError("upstream returned an empty body")

        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise TransportError("upstream returned malformed JSON") from exc

        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise TransportError("upstream response carried no choices")

        choice = choices[0]
        if not isinstance(choice, dict):
            raise TransportError("upstream choice was malformed")

        finish = choice.get("finish_reason")
        if finish == "length":
            # A truncated answer must never be handed back as if complete.
            raise TransportError("upstream response was truncated (finish=length)")
        if finish == "tool_calls":
            # The model asked for a tool it was not served. Returning b""
            # here is the fail-open that made free models look incapable.
            raise TransportError(
                "upstream requested tools; this transport serves none")

        message = choice.get("message")
        if not isinstance(message, dict):
            raise TransportError("upstream message was malformed")
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise TransportError("upstream returned no content")

        self.returned_model = parsed.get("model")
        self.last_usage = parsed.get("usage")
        return content.encode("utf-8")


def from_env(**over: Any) -> OmniRouteTransport:
    """Build a transport using `OMNIROUTE_API_KEY` from the environment.

    The key is read here and nowhere else, so it never has to be threaded
    through a config file, a command line, or a payload.
    """
    key = os.environ.get("OMNIROUTE_API_KEY", "")
    if not key:
        raise TransportError("OMNIROUTE_API_KEY is not set")
    return OmniRouteTransport(api_key=key, **over)
