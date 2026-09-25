"""Behavioral tests for the candidate-bound approval command adapter."""

import pytest

from approve_command import ApprovalCommandError, execute_approve
from exact_sha_verifier import VerificationResult
from state_journal import StateJournal


class FakeCoordinator:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def test_approve_requires_exact_candidate_and_message_scope():
    coordinator = FakeCoordinator(result=type("Result", (), {"status": "closed"})())
    result = execute_approve(
        reply={
            "command": "approve", "bead": "bead-1", "candidate_id": "candidate-1",
            "head_sha": "a" * 40, "repository": "example/school-core",
            "message_id": "message-1", "from": "human@example.com",
        },
        coordinator=coordinator,
        expected_bead="bead-1", expected_repository="example/school-core",
        allowed_approvers=("human@example.com",), dry_run=True,
    )
    assert result.status == "dry_run"
    assert coordinator.calls == []


@pytest.mark.parametrize("field", ["candidate_id", "head_sha", "repository", "message_id"])
def test_approve_rejects_missing_identity(field):
    coordinator = FakeCoordinator()
    reply = {
        "command": "approve", "bead": "bead-1", "candidate_id": "candidate-1",
        "head_sha": "a" * 40, "repository": "example/school-core",
        "message_id": "message-1", "from": "human@example.com",
    }
    reply.pop(field)
    with pytest.raises(ApprovalCommandError, match=field):
        execute_approve(
            reply=reply, coordinator=coordinator, expected_bead="bead-1",
            expected_repository="example/school-core",
            allowed_approvers=("human@example.com",), dry_run=True,
        )
    assert coordinator.calls == []


def test_approve_rejects_wrong_bead_repository_or_approver():
    coordinator = FakeCoordinator()
    base = {
        "command": "approve", "bead": "bead-1", "candidate_id": "candidate-1",
        "head_sha": "a" * 40, "repository": "example/school-core",
        "message_id": "message-1", "from": "human@example.com",
    }
    for change, expected in [({"bead": "bead-2"}, "Bead"), ({"repository": "other/repo"}, "repository"), ({"from": "agent@example.com"}, "approver")]:
        reply = {**base, **change}
        with pytest.raises(ApprovalCommandError, match=expected):
            execute_approve(
                reply=reply, coordinator=coordinator, expected_bead="bead-1",
                expected_repository="example/school-core",
                allowed_approvers=("human@example.com",), dry_run=True,
            )


def test_approve_delegates_exact_scope_to_coordinator():
    coordinator = FakeCoordinator(result=type("Result", (), {"status": "closed"})())
    execute_approve(
        reply={
            "command": "approve", "bead": "bead-1", "candidate_id": "candidate-1",
            "head_sha": "a" * 40, "repository": "example/school-core",
            "message_id": "message-1", "from": "human@example.com",
            "approval_id": "approval-1", "operation_id": "operation-1",
            "idempotency_key": "merge:candidate-1",
        },
        coordinator=coordinator, expected_bead="bead-1", expected_repository="example/school-core",
        allowed_approvers=("human@example.com",), dry_run=False,
    )
    assert coordinator.calls[0]["candidate_id"] == "candidate-1"
    assert coordinator.calls[0]["head_sha"] == "a" * 40
    assert coordinator.calls[0]["bead_id"] == "bead-1"
