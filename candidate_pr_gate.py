"""Candidate-bound PR-creation gate.

This gate sits between a validated candidate manifest and
`publish_candidate_pr_idempotent`. It is the seam that turns trusted checks
and required teacher approval into an authorization decision, and it enforces
fail-closed behavior: a skipped or unrunnable trusted gate is a refusal, not a
pass and not a silent skip.

Source of truth: docs/pr-provider-boundary.md (publish seam) and the
school-core-sjv.11 bead description (PR-creation gate).

Default policy
--------------
A candidate-bound PR may be opened when all of the following hold:

1. The candidate manifest validates against the repo the bridge actually cloned
   (`CandidateStore.validate(...) == "current"`).
2. The trusted verification evidence attached to the same candidate is
   `passed`.
3. The configured teacher approval exists for the same candidate_id and
   head_sha.

When the gate refuses, it raises `GateDenied`. The caller must not write to the
provider; the issue stays retryable / open depending on the refusal class.

Non-goals
---------
- Merge is human-owned; this gate only authorizes opening the PR.
- This module does not fetch teacher approvals or verification evidence itself —
  those are injected by the caller so the seam stays deterministic and testable.
"""


from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from candidate_manifest import CandidateManifest, CandidateStore, ValidationResult


def trusted_verification_from_binding(binding: "CandidateBinding") -> "VerificationEvidence":
    """Re-surface a binding's stored verification posture as VerificationEvidence."""
    return _BindingVerificationAdapter(binding)


def teacher_approval_from_binding(binding: "CandidateBinding") -> "TeacherApproval":
    """Re-surface a binding's stored approval posture as TeacherApproval."""
    return _BindingApprovalAdapter(binding)


class _BindingVerificationAdapter:
    def __init__(self, binding: "CandidateBinding") -> None:
        self.candidate_id = binding.candidate_id
        self.head_sha = binding.head_sha
        self.passed = binding.verification_passed
        self.skipped = binding.verification_skipped
        self.failures = binding.verification_failures


class _BindingApprovalAdapter:
    def __init__(self, binding: "CandidateBinding") -> None:
        self.candidate_id = binding.candidate_id
        self.head_sha = binding.head_sha
        self.state = binding.approval_state
        self.approval_id = binding.approval_id


class GateDenied(Exception):
    """The candidate-bound PR-creation gate refused a provider write.

    The refusal carries a stable machine-readable reason so the caller can
    classify the outcome without inventing one.
    """

    def __init__(self, reason: str, *, candidate_id: str, head_sha: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.candidate_id = candidate_id
        self.head_sha = head_sha


class VerificationEvidence(Protocol):
    """Structural seam for trusted verification evidence bound to a candidate.

    The gate only reads the fields it needs. Callers may pass any object that
    carries the same shape — a candidate-bound trusted verifier or test stub.
    """

    candidate_id: str
    head_sha: str
    passed: bool
    skipped: bool
    failures: tuple[dict[str, Any], ...]


class TeacherApproval(Protocol):
    """Structural seam for a teacher/operator approval bound to a candidate.

    The gate only reads the fields it needs. Callers may pass any object that
    carries the same shape — a trusted approval ingress or test stub.
    """

    candidate_id: str
    head_sha: str
    state: str
    approval_id: str


@dataclasses.dataclass(frozen=True)
class GateDecision:
    """The gate's decision for one candidate.

    When `allowed` is False the caller MUST NOT publish and MUST treat the
    candidate as refused for this attempt.
    """

    allowed: bool
    candidate_id: str
    head_sha: str
    reason: str
    failure_class: str


def _validate_manifest(
    store: CandidateStore,
    manifest: CandidateManifest,
    repo_path,
) -> ValidationResult:
    """Validate the exact candidate manifest against the current repo state.

    This is the authoritative identity check: it binds the manifest to the repo
    the bridge cloned, and it refuses stale head / changed base / dirty tree /
    changed branch / changed diff.
    """
    return store.validate(repo_path, manifest)


def _trusts_verification(evidence: VerificationEvidence) -> bool:
    """True when the trusted verification evidence authorizes publication.

    A skipped or unrunnable trusted gate is a refusal. Only an explicitly passed
    trusted check counts.
    """
    if not evidence.passed:
        return False
    if evidence.skipped:
        return False
    return True


def _has_required_approval(approval: TeacherApproval | None) -> bool:
    """True when an explicit approved state authorizes this candidate.

    Identity is checked separately before this predicate. None is not approval.
    """
    if approval is None:
        return False
    return approval.state == "approved"


def gate_candidate_publication(
    *,
    store: CandidateStore,
    manifest: CandidateManifest,
    repo_path,
    verification: VerificationEvidence | None,
    approval: TeacherApproval | None,
    require_approval: bool = True,
) -> GateDecision:
    """Decide whether the candidate-bound PR may be opened for this candidate.

    Fail-closed:
    - stale / dirty / mismatched candidate — refuse
    - missing / skipped / failed trusted verification — refuse
    - missing approval when the policy requires one — refuse

    The decision is bound to (candidate_id, head_sha) so any later retry must
    present the same identity; a guess is not an authorization.
    """
    candidate_id = manifest.candidate_id
    head_sha = manifest.head_sha

    validation = _validate_manifest(store, manifest, repo_path)
    if validation.status != "current":
        return GateDecision(
            allowed=False,
            candidate_id=candidate_id,
            head_sha=head_sha,
            reason=(
                f"candidate {candidate_id} is not current against the repo the bridge cloned: "
                f"{validation.reason}"
            ),
            failure_class="candidate_not_current",
        )

    if verification is None:
        return GateDecision(
            allowed=False,
            candidate_id=candidate_id,
            head_sha=head_sha,
            reason=(
                f"candidate {candidate_id} has no trusted verification evidence attached"
            ),
            failure_class="trusted_verification_missing",
        )

    if (
        getattr(verification, "candidate_id", None) != candidate_id
        or getattr(verification, "head_sha", None) != head_sha
    ):
        return GateDecision(
            allowed=False,
            candidate_id=candidate_id,
            head_sha=head_sha,
            reason=(
                f"trusted verification evidence is not bound to candidate {candidate_id} "
                f"at head {head_sha}"
            ),
            failure_class="trusted_verification_identity_mismatch",
        )

    if not _trusts_verification(verification):
        return GateDecision(
            allowed=False,
            candidate_id=candidate_id,
            head_sha=head_sha,
            reason=(
                f"candidate {candidate_id} does not have a passed trusted verification "
                f"(passed={verification.passed}, skipped={verification.skipped})"
            ),
            failure_class="trusted_verification_not_passed",
        )

    if require_approval and approval is not None and (
        getattr(approval, "candidate_id", None) != candidate_id
        or getattr(approval, "head_sha", None) != head_sha
    ):
        return GateDecision(
            allowed=False,
            candidate_id=candidate_id,
            head_sha=head_sha,
            reason=(
                f"teacher approval evidence is not bound to candidate {candidate_id} "
                f"at head {head_sha}"
            ),
            failure_class="teacher_approval_identity_mismatch",
        )

    if require_approval and not _has_required_approval(approval):
        return GateDecision(
            allowed=False,
            candidate_id=candidate_id,
            head_sha=head_sha,
            reason=(
                f"candidate {candidate_id} lacks the required teacher approval "
                f"(require_approval=True, approval="
                f"{getattr(approval, 'state', None)!r})"
            ),
            failure_class="teacher_approval_required",
        )

    return GateDecision(
        allowed=True,
        candidate_id=candidate_id,
        head_sha=head_sha,
        reason="candidate-bound publication is authorized",
        failure_class="authorized",
    )


def gate_candidate_publication_from_binding(
    *,
    store: CandidateStore,
    manifest: CandidateManifest,
    repo_path,
    binding: "CandidateBinding",
    require_approval: bool = True,
) -> GateDecision:
    """Re-run the gate from a binding record.

    This is the resume-safe path: the gate consumes the posture stored on the
    binding instead of re-deriving it from raw verification/approval stubs.
    """
    return gate_candidate_publication(
        store=store,
        manifest=manifest,
        repo_path=repo_path,
        verification=trusted_verification_from_binding(binding),
        approval=teacher_approval_from_binding(binding),
        require_approval=require_approval,
    )
