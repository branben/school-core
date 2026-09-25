"""Read-only exact-head CI and human-approval verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    head_sha: str
    state: str
    mergeable: bool | None = None


@dataclass(frozen=True)
class CheckRun:
    name: str
    head_sha: str
    status: str
    conclusion: str | None
    sequence: int


@dataclass(frozen=True)
class HumanApproval:
    actor: str
    head_sha: str
    state: str
    sequence: int


@dataclass(frozen=True)
class RequiredPolicy:
    required_checks: tuple[str, ...]
    allowed_approvers: tuple[str, ...]


@dataclass(frozen=True)
class VerificationResult:
    status: str
    head_sha: str
    reasons: tuple[str, ...] = ()
    check_ids: tuple[int, ...] = ()


def verify_exact_sha(
    *, candidate_id: str, head_sha: str, pull_request: PullRequestSnapshot,
    checks: Iterable[CheckRun], approvals: Iterable[HumanApproval],
    policy: RequiredPolicy,
) -> VerificationResult:
    reasons: list[str] = []
    if pull_request.head_sha != head_sha:
        return VerificationResult("stale", head_sha, ("pull request head changed",))
    if pull_request.state not in {"open", "merged"}:
        return VerificationResult("blocked", head_sha, (f"pull request is {pull_request.state}",))

    latest_checks: dict[str, CheckRun] = {}
    for check in checks:
        previous = latest_checks.get(check.name)
        if previous is None or check.sequence > previous.sequence:
            latest_checks[check.name] = check
    for name in policy.required_checks:
        check = latest_checks.get(name)
        if check is None:
            reasons.append(f"required check {name} missing")
        elif check.head_sha != head_sha:
            reasons.append(f"check {name} head is stale")
        elif check.status != "completed" or check.conclusion != "success":
            reasons.append(f"required check {name} is not successful")

    latest_approvals: dict[str, HumanApproval] = {}
    approval_states: dict[str, set[str]] = {}
    for approval in approvals:
        previous = latest_approvals.get(approval.actor)
        if previous is None or approval.sequence > previous.sequence:
            latest_approvals[approval.actor] = approval
        if approval.head_sha == head_sha:
            approval_states.setdefault(approval.actor, set()).add(approval.state)
    allowed = [a for a in latest_approvals.values() if a.actor in policy.allowed_approvers]
    current = [a for a in allowed if a.head_sha == head_sha and a.state == "approved"]
    for approval in allowed:
        if approval.head_sha != head_sha:
            reasons.append(f"approval {approval.actor} head is stale")
    if not current:
        reasons.append("current human approval missing")
    if any(a.head_sha == head_sha and a.state not in {"approved", "dismissed"} for a in allowed):
        reasons.append("approval state is unresolved")
    if any(
        actor in policy.allowed_approvers
        and {"approved", "dismissed"}.issubset(states)
        for actor, states in approval_states.items()
    ):
        reasons.append("approval evidence is conflicting")

    if any("stale" in reason for reason in reasons):
        status = "stale"
    elif any("unresolved" in reason or "conflicting" in reason for reason in reasons):
        status = "conflicted"
    elif reasons:

        status = "not_ready"
    else:
        status = "eligible"
    return VerificationResult(
        status=status,
        head_sha=head_sha,
        reasons=tuple(reasons),
        check_ids=tuple(sorted(c.sequence for c in latest_checks.values())),
    )


__all__ = [
    "CheckRun", "HumanApproval", "PullRequestSnapshot", "RequiredPolicy",
    "VerificationResult", "verify_exact_sha",
]
