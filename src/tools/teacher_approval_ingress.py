"""Trusted, authenticated teacher-approval ingress for the PR-creation gate.

Why this module exists
----------------------
The bead noted "the repo's pre-PR authenticated teacher-approval producer is not
yet found". It *does* exist: `StateJournal.issue_approval` persists an approval
that names an `actor`, a `scope`, and the exact `(candidate_id, head_sha)`, and
`approve_command.execute_approve` already enforces an approver allowlist and
bead/repository scope. What was missing was the adapter that turns such a
persisted record into the `TeacherApproval` shape the gate reads.

This module is that adapter, and nothing more.

Trust boundary (read this before widening it)
---------------------------------------------
An approval authorizes opening a PR for **one exact candidate at one exact
head**. Therefore:

* An approval is only surfaced when its `candidate_id` AND `head_sha` both
  match the manifest being gated. An approval for a different candidate or a
  superseded head is not authorization.
* The `actor` must be in the caller's `allowed_approvers` allowlist. The
  journal records who approved; this module refuses to invent trust.
* The approval `scope` must be `TRUSTED_APPROVAL_SCOPE`. The journal also
  carries `merge_this_candidate` approvals; **merge authority is human-owned
  and this gate must not consume it**, so a merge-scoped approval is rejected.
* Only an `issued` approval authorizes. A consumed / revoked / expired record
  is history, not permission.

Automated review acceptance (`review.accepted`) and any synthesized approval id
are deliberately NOT inputs here. `None` is returned rather than a guess, so the
gate fails closed with `teacher_approval_required`.

Merge remains human-owned: this module authorizes *opening* a PR, never merging.
"""

from __future__ import annotations

from typing import Any, Protocol

# Pre-PR publication authorization. Distinct from merge authorization, which
# this gate must never consume.
TRUSTED_APPROVAL_SCOPE = "open_pr_for_candidate"

# Journal approval states. Only `issued` is live authorization; the others are
# terminal history.
_LIVE_APPROVAL_STATE = "issued"


class _ApprovalRecordLike(Protocol):
    approval_id: str
    candidate_id: str
    head_sha: str
    actor: str
    scope: str
    state: str


class _JournalLike(Protocol):
    def approvals(self) -> list[_ApprovalRecordLike]:
        ...


class _TrustedApproval:
    """The `TeacherApproval` shape the gate reads.

    Deliberately a plain object, not a Protocol instance: the gate only reads
    these five fields, and a real object here keeps the identity explicit and
    inspectable in a refusal message.
    """

    __slots__ = ("candidate_id", "head_sha", "state", "approval_id", "actor", "scope")

    def __init__(
        self,
        *,
        candidate_id: str,
        head_sha: str,
        state: str,
        approval_id: str,
        actor: str,
        scope: str,
    ) -> None:
        self.candidate_id = candidate_id
        self.head_sha = head_sha
        self.state = state
        self.approval_id = approval_id
        self.actor = actor
        self.scope = scope

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"_TrustedApproval(approval_id={self.approval_id!r}, "
            f"candidate_id={self.candidate_id!r}, state={self.state!r})"
        )


def trusted_approval_from_journal(
    *,
    journal: Any,
    manifest: Any,
    allowed_approvers: tuple[str, ...],
) -> _TrustedApproval | None:
    """Return the live trusted approval for this exact candidate, or None.

    None is the common case and is not an error: it means "no authenticated
    approval for this candidate exists (yet)", which the gate reports as
    `teacher_approval_required`. Every rejection path returns None rather than
    raising, because a missing approval is a normal fail-closed outcome, not an
    exception the caller should have to handle.
    """
    if journal is None or manifest is None:
        return None
    allowed = set(allowed_approvers or ())
    if not allowed:
        # No configured approvers means nobody can authorize. Fail closed.
        return None

    try:
        records = journal.approvals()
    except Exception:
        # An unreadable journal is not an authorization. Never treat a
        # persistence failure as permission.
        return None

    candidate_id = getattr(manifest, "candidate_id", None)
    head_sha = getattr(manifest, "head_sha", None)
    if not candidate_id or not head_sha:
        return None

    # Most specific match first; among equals prefer the newest issued record.
    matching = [
        record
        for record in (records or ())
        if getattr(record, "candidate_id", None) == candidate_id
        and getattr(record, "head_sha", None) == head_sha
    ]
    if not matching:
        return None

    for record in matching:
        if getattr(record, "scope", None) != TRUSTED_APPROVAL_SCOPE:
            continue
        if getattr(record, "actor", None) not in allowed:
            continue
        if getattr(record, "state", None) != _LIVE_APPROVAL_STATE:
            continue
        return _TrustedApproval(
            candidate_id=candidate_id,
            head_sha=head_sha,
            state="approved",
            approval_id=str(getattr(record, "approval_id", "")),
            actor=str(getattr(record, "actor", "")),
            scope=TRUSTED_APPROVAL_SCOPE,
        )
    return None


__all__ = ["TRUSTED_APPROVAL_SCOPE", "trusted_approval_from_journal"]