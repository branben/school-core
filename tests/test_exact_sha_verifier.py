"""Behavioral tests for exact-SHA CI and approval verification."""

from exact_sha_verifier import (
    CheckRun,
    HumanApproval,
    PullRequestSnapshot,
    RequiredPolicy,
    verify_exact_sha,
)


def test_matching_current_ci_and_approval_are_eligible():
    result = verify_exact_sha(
        candidate_id="candidate-1",
        head_sha="a" * 40,
        pull_request=PullRequestSnapshot(number=7, head_sha="a" * 40, state="open", mergeable=True),
        checks=[
            CheckRun(name="test", head_sha="a" * 40, status="completed", conclusion="success", sequence=2),
        ],
        approvals=[
            HumanApproval(actor="human@example.com", head_sha="a" * 40, state="approved", sequence=1),
        ],
        policy=RequiredPolicy(required_checks=("test",), allowed_approvers=("human@example.com",)),
    )
    assert result.status == "eligible"
    assert result.head_sha == "a" * 40
    assert result.reasons == ()


def test_older_or_newer_head_evidence_is_stale():
    result = verify_exact_sha(
        candidate_id="candidate-1",
        head_sha="a" * 40,
        pull_request=PullRequestSnapshot(number=7, head_sha="a" * 40, state="open", mergeable=True),
        checks=[CheckRun(name="test", head_sha="b" * 40, status="completed", conclusion="success", sequence=2)],
        approvals=[HumanApproval(actor="human@example.com", head_sha="a" * 40, state="approved", sequence=1)],
        policy=RequiredPolicy(required_checks=("test",), allowed_approvers=("human@example.com",)),
    )
    assert result.status == "stale"
    assert any("check test head" in reason for reason in result.reasons)


def test_missing_required_check_and_missing_approval_are_not_ready():
    result = verify_exact_sha(
        candidate_id="candidate-1",
        head_sha="a" * 40,
        pull_request=PullRequestSnapshot(number=7, head_sha="a" * 40, state="open", mergeable=True),
        checks=[],
        approvals=[],
        policy=RequiredPolicy(required_checks=("test",), allowed_approvers=("human@example.com",)),
    )
    assert result.status == "not_ready"
    assert any("test" in reason for reason in result.reasons)
    assert any("approval" in reason for reason in result.reasons)


def test_duplicate_and_reordered_events_do_not_change_result():
    result = verify_exact_sha(
        candidate_id="candidate-1",
        head_sha="a" * 40,
        pull_request=PullRequestSnapshot(number=7, head_sha="a" * 40, state="open", mergeable=True),
        checks=[
            CheckRun(name="test", head_sha="a" * 40, status="completed", conclusion="success", sequence=4),
            CheckRun(name="test", head_sha="a" * 40, status="queued", conclusion=None, sequence=3),
        ],
        approvals=[
            HumanApproval(actor="human@example.com", head_sha="a" * 40, state="approved", sequence=5),
            HumanApproval(actor="human@example.com", head_sha="a" * 40, state="approved", sequence=5),
        ],
        policy=RequiredPolicy(required_checks=("test",), allowed_approvers=("human@example.com",)),
    )
    assert result.status == "eligible"
    assert result.check_ids == (4,)


def test_conflicting_current_approvals_are_blocked():
    result = verify_exact_sha(
        candidate_id="candidate-1",
        head_sha="a" * 40,
        pull_request=PullRequestSnapshot(number=7, head_sha="a" * 40, state="open", mergeable=True),
        checks=[CheckRun(name="test", head_sha="a" * 40, status="completed", conclusion="success", sequence=2)],
        approvals=[
            HumanApproval(actor="human@example.com", head_sha="a" * 40, state="approved", sequence=3),
            HumanApproval(actor="human@example.com", head_sha="a" * 40, state="dismissed", sequence=4),
        ],
        policy=RequiredPolicy(required_checks=("test",), allowed_approvers=("human@example.com",)),
    )
    assert result.status == "conflicted"
    assert any("approval" in reason for reason in result.reasons)


def test_mergeable_pr_without_required_evidence_is_not_eligible():
    result = verify_exact_sha(
        candidate_id="candidate-1",
        head_sha="a" * 40,
        pull_request=PullRequestSnapshot(number=7, head_sha="a" * 40, state="open", mergeable=True),
        checks=[],
        approvals=[],
        policy=RequiredPolicy(required_checks=(), allowed_approvers=()),
    )
    assert result.status == "not_ready"
    assert any("approval" in reason for reason in result.reasons)
