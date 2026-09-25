"""Coordinator for merge confirmation and Beads closure.

The coordinator is the only side-effect owner. Providers are injected so tests
can exercise the full lifecycle without live GitHub or Beads writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from state_journal import ConflictError, InvalidStateError, StateJournal


class CoordinatorError(RuntimeError):
    """The candidate cannot safely advance."""


class Eligibility(Protocol):
    def verify(self, **kwargs) -> object: ...


class Provider(Protocol):
    def merge(self, *, candidate_id: str, head_sha: str, operation_id: str): ...
    def read_merge_state(self, *, candidate_id: str, head_sha: str): ...
    def close_bead(self, *, bead_id: str, candidate_id: str, head_sha: str, operation_id: str): ...


@dataclass(frozen=True)
class CoordinatorResult:
    status: str
    candidate_id: str
    head_sha: str
    operation_id: str
    merge_commit: str | None = None


class MergeCoordinator:
    def __init__(self, journal: StateJournal, provider: Provider, eligibility: Eligibility):
        self.journal = journal
        self.provider = provider
        self.eligibility = eligibility

    def run(
        self, *, candidate_id: str, bead_id: str, head_sha: str,
        approval_id: str, operation_id: str, idempotency_key: str,
    ) -> CoordinatorResult:
        try:
            existing = self.journal.operation(operation_id)
        except InvalidStateError:
            existing = None
        if existing is not None and existing.status == "confirmed" and any(
            event["kind"] == "closed" for event in existing.events
        ):
            return CoordinatorResult("closed", candidate_id, head_sha, operation_id)

        eligibility = self.eligibility.verify(
            candidate_id=candidate_id, head_sha=head_sha,
        )
        if getattr(eligibility, "status", None) != "eligible":
            reasons = getattr(eligibility, "reasons", ())
            raise CoordinatorError(f"candidate is not eligible: {', '.join(reasons) or 'unknown reason'}")

        operation = self.journal.start_operation(
            operation_id=operation_id, idempotency_key=idempotency_key,
            candidate_id=candidate_id, head_sha=head_sha, kind="merge_request",
        )
        try:
            self.journal.consume_approval(
                approval_id=approval_id, candidate_id=candidate_id, head_sha=head_sha,
                operation_id=f"{operation_id}:approval", idempotency_key=f"approval:{idempotency_key}",
            )
        except (ConflictError, InvalidStateError) as exc:
            raise CoordinatorError(f"approval cannot authorize this merge: {exc}") from exc

        if operation.status == "started" and not any(e["kind"] == "requested" for e in operation.events):
            try:
                self.provider.merge(candidate_id=candidate_id, head_sha=head_sha, operation_id=operation_id)
                self.journal.record_event(operation_id, "requested", {"provider": "injected"})
            except Exception as exc:
                self.journal.record_event(operation_id, "failed", {"error": str(exc)[:4096]})
                raise CoordinatorError(f"merge request failed: {exc}") from exc

        state = self.provider.read_merge_state(candidate_id=candidate_id, head_sha=head_sha)
        if not isinstance(state, dict) or state.get("merged") is not True:
            self.journal.record_event(operation_id, "failed", {"reason": "merge not confirmed"})
            raise CoordinatorError("merge state is not confirmed")
        merge_commit = state.get("merge_commit")
        if not isinstance(merge_commit, str) or not merge_commit:
            self.journal.record_event(operation_id, "failed", {"reason": "merge commit missing"})
            raise CoordinatorError("merge confirmation has no merge commit")
        if state.get("head_sha") != head_sha:
            self.journal.record_event(operation_id, "failed", {"reason": "merge head mismatch"})
            raise CoordinatorError("merge confirmation head does not match candidate")
        self.journal.record_event(operation_id, "confirmed", {"merge_commit": merge_commit})

        try:
            if not any(event["kind"] == "closed" for event in self.journal.operation(operation_id).events):
                self.provider.close_bead(
                    bead_id=bead_id, candidate_id=candidate_id, head_sha=head_sha,
                    operation_id=operation_id,
                )
                self.journal.record_event(operation_id, "closed", {"bead_id": bead_id})
        except Exception as exc:
            self.journal.record_event(operation_id, "failed", {"phase": "close", "error": str(exc)[:4096]})
            raise CoordinatorError(f"Beads closure failed after confirmed merge: {exc}") from exc
        return CoordinatorResult("closed", candidate_id, head_sha, operation_id, merge_commit)


__all__ = ["CoordinatorError", "CoordinatorResult", "MergeCoordinator"]
