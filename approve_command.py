"""Candidate-bound `/approve` command adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ApprovalCommandError(ValueError):
    """Approval input cannot authorize a merge."""


@dataclass(frozen=True)
class DryRunResult:
    status: str
    candidate_id: str
    head_sha: str
    bead_id: str


def execute_approve(
    *, reply: dict[str, Any], coordinator: Any, expected_bead: str,
    expected_repository: str, allowed_approvers: tuple[str, ...], dry_run: bool = False,
) -> Any:
    if reply.get("command") != "approve":
        raise ApprovalCommandError("not an approve command")
    required = ("bead", "candidate_id", "head_sha", "repository", "message_id", "from")
    for field in required:
        if not str(reply.get(field, "")).strip():
            raise ApprovalCommandError(f"{field} is required")
    if reply["bead"] != expected_bead:
        raise ApprovalCommandError("Bead scope does not match")
    if reply["repository"] != expected_repository:
        raise ApprovalCommandError("repository scope does not match")
    if reply["from"] not in allowed_approvers:
        raise ApprovalCommandError("approver is not allowlisted")
    if len(reply["head_sha"]) != 40 or any(c not in "0123456789abcdef" for c in reply["head_sha"]):
        raise ApprovalCommandError("head_sha must be a full Git SHA")
    if dry_run:
        return DryRunResult("dry_run", reply["candidate_id"], reply["head_sha"], reply["bead"])
    for field in ("approval_id", "operation_id", "idempotency_key"):
        if not str(reply.get(field, "")).strip():
            raise ApprovalCommandError(f"{field} is required for coordinator execution")
    try:
        return coordinator.run(
            candidate_id=reply["candidate_id"], bead_id=reply["bead"],
            head_sha=reply["head_sha"], approval_id=reply["approval_id"],
            operation_id=reply["operation_id"], idempotency_key=reply["idempotency_key"],
        )
    except Exception as exc:
        raise ApprovalCommandError(f"coordinator rejected approval: {exc}") from exc


__all__ = ["ApprovalCommandError", "DryRunResult", "execute_approve"]
