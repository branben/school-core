"""Candidate identity binding for the trusted-verification and teacher-approval posture.

This is the seam that carries one candidate's identity plus the exact
verification/approval posture used by the PR-creation gate. The gate already
binds to (candidate_id, head_sha); this module makes the same binding durable
enough to survive restart and resume: a later cycle can reload the binding and
re-run the gate against the same candidate identity instead of inventing a new
posture from fragile in-memory state.

Source of truth for the next slice: school-core/goals/school-core-complete/plan.md
Phase 1 items (3) and (4).

Non-goals here:
- This module does not replace CandidateManifest, CandidateStore, verification,
  or approval. It is a thin binding record plus a resume-safe store seam.
- Merge and provider writes are still human-owned and fail-closed.
"""


from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from candidate_manifest import CandidateManifest


@dataclasses.dataclass(frozen=True)
class CandidateBinding:
    """One candidate's identity plus the trusted-evidence posture bound to it.

    The gate cares about candidate_id and head_sha. This record carries the same
    identity plus enough posture to re-evaluate the gate after restart without
    re-deriving the verification/approval decision from scratch.
    """

    candidate_id: str
    head_sha: str
    repository: str
    issue_number: int
    branch: str
    base_sha: str
    diff_digest: str
    verification_passed: bool
    verification_skipped: bool
    verification_failures: tuple[dict[str, Any], ...]
    approval_state: str
    approval_id: str
    bound_at: str


def bind_trusted_evidence(
    *,
    manifest: CandidateManifest,
    verification: "VerificationEvidence",
    approval: "TeacherApproval | None",
) -> CandidateBinding:
    """Bind one candidate manifest to its trusted verification and approval posture.

    Fail-closed at the binding boundary: a verification or approval object whose
    candidate identity does not match the manifest cannot be bound here. A later
    gate run must refuse on the stored posture, not on a mismatched object.
    """
    if not isinstance(verification, object):
        raise TypeError("verification must be an object")
    if verification.candidate_id != manifest.candidate_id:
        raise ValueError(
            f"verification candidate_id {verification.candidate_id!r} does not match manifest "
            f"{manifest.candidate_id!r}"
        )
    if verification.head_sha != manifest.head_sha:
        raise ValueError(
            f"verification head_sha {verification.head_sha!r} does not match manifest "
            f"{manifest.head_sha!r}"
        )

    if approval is not None and approval.candidate_id != manifest.candidate_id:
        raise ValueError(
            f"approval candidate_id {approval.candidate_id!r} does not match manifest "
            f"{manifest.candidate_id!r}"
        )
    if approval is not None and approval.head_sha != manifest.head_sha:
        raise ValueError(
            f"approval head_sha {approval.head_sha!r} does not match manifest "
            f"{manifest.head_sha!r}"
        )

    return CandidateBinding(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        repository=manifest.repository,
        issue_number=manifest.issue_number,
        branch=manifest.branch,
        base_sha=manifest.base_sha,
        diff_digest=manifest.diff_digest,
        verification_passed=bool(verification.passed),
        verification_skipped=bool(verification.skipped),
        verification_failures=tuple(verification.failures or ()),
        approval_state=(approval.state if approval is not None else "pending"),
        approval_id=(approval.approval_id if approval is not None else ""),
        bound_at="",  # set by the store seam on persist
    )


def trusted_verification_from_binding(binding: CandidateBinding) -> "VerificationEvidence":
    """Re-surface the binding's stored verification posture as a VerificationEvidence.

    This is what the gate uses on resume: the same posture that was bound earlier,
    not a fresh re-derivation.
    """
    return _BindingVerificationAdapter(binding)


def teacher_approval_from_binding(binding: CandidateBinding) -> "TeacherApproval":
    """Re-surface the binding's stored approval posture as a TeacherApproval."""
    return _BindingApprovalAdapter(binding)


class _BindingVerificationAdapter:
    def __init__(self, binding: CandidateBinding) -> None:
        self.candidate_id = binding.candidate_id
        self.head_sha = binding.head_sha
        self.passed = binding.verification_passed
        self.skipped = binding.verification_skipped
        self.failures = binding.verification_failures


class _BindingApprovalAdapter:
    def __init__(self, binding: CandidateBinding) -> None:
        self.candidate_id = binding.candidate_id
        self.head_sha = binding.head_sha
        self.state = binding.approval_state
        self.approval_id = binding.approval_id


class CandidateBindingStore:
    """Resume-safe store for candidate bindings.

    This is a thin durability seam for the binding record. It is intentionally
    separate from CandidateStore: CandidateStore owns immutable manifests; this
    store owns the evaluation posture bound to them.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def put(self, binding: CandidateBinding) -> CandidateBinding:
        """Persist one binding, preserving any other candidate's binding.

        This previously wrote a single-entry dict, so persisting candidate B
        silently evicted candidate A. A cycle that bound several candidates and
        then resumed from disk would find a candidate missing its posture and
        re-derive it from scratch — the exact restart fragility the binding
        record exists to prevent.
        """
        binding = dataclasses.replace(binding, bound_at=_now_iso())
        data = self._load()
        data[binding.candidate_id] = _binding_to_dict(binding)
        self._save(data)
        return binding

    def get(self, candidate_id: str) -> CandidateBinding | None:
        data = self._load()
        raw = data.get(candidate_id)
        if raw is None:
            return None
        return _binding_from_dict(raw)

    def peek(self, candidate_id: str) -> CandidateBinding | None:
        return self.get(candidate_id)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        import json
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        import json
        import tempfile
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def lookup_binding(store: CandidateBindingStore, *, candidate_id: str) -> CandidateBinding | None:
    """Resume a candidate binding by candidate_id."""
    return store.get(candidate_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _binding_to_dict(binding: CandidateBinding) -> dict:
    return {
        "candidate_id": binding.candidate_id,
        "head_sha": binding.head_sha,
        "repository": binding.repository,
        "issue_number": binding.issue_number,
        "branch": binding.branch,
        "base_sha": binding.base_sha,
        "diff_digest": binding.diff_digest,
        "verification_passed": binding.verification_passed,
        "verification_skipped": binding.verification_skipped,
        "verification_failures": list(binding.verification_failures),
        "approval_state": binding.approval_state,
        "approval_id": binding.approval_id,
        "bound_at": binding.bound_at,
    }


def _binding_from_dict(raw: dict) -> CandidateBinding:
    return CandidateBinding(
        candidate_id=raw["candidate_id"],
        head_sha=raw["head_sha"],
        repository=raw["repository"],
        issue_number=raw["issue_number"],
        branch=raw["branch"],
        base_sha=raw["base_sha"],
        diff_digest=raw["diff_digest"],
        verification_passed=bool(raw["verification_passed"]),
        verification_skipped=bool(raw["verification_skipped"]),
        verification_failures=tuple(raw["verification_failures"] or ()),
        approval_state=raw["approval_state"],
        approval_id=raw["approval_id"],
        bound_at=raw.get("bound_at", ""),
    )
