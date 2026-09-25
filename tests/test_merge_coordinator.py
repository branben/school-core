"""Behavioral tests for the durable merge/closure coordinator."""

import pytest

from exact_sha_verifier import CheckRun, HumanApproval, PullRequestSnapshot, RequiredPolicy, VerificationResult
from merge_coordinator import CoordinatorError, MergeCoordinator
from state_journal import ConflictError, StateJournal


class FakeEligibility:
    def __init__(self, result: VerificationResult):
        self.result = result

    def verify(self, **kwargs):
        return self.result


class FakeProvider:
    def __init__(self):
        self.merge_calls = 0
        self.closed = []

    def read_merge_state(self, *, candidate_id, head_sha):
        return {"merged": True, "merge_commit": "m" * 40, "head_sha": head_sha}

    def merge(self, *, candidate_id, head_sha, operation_id):
        self.merge_calls += 1
        return {"status": "accepted"}

    def close_bead(self, *, bead_id, candidate_id, head_sha, operation_id):
        self.closed.append((bead_id, candidate_id, head_sha, operation_id))
        return {"status": "closed"}


def test_coordinator_merges_confirms_then_closes_once(tmp_path):
    journal = StateJournal(tmp_path / "state.sqlite3")
    provider = FakeProvider()
    coordinator = MergeCoordinator(journal, provider, FakeEligibility(VerificationResult("eligible", "a" * 40)))
    journal.issue_approval(
        approval_id="approval-1", candidate_id="candidate-1", head_sha="a" * 40,
        actor="human@example.com", scope="merge_this_candidate",
    )
    result = coordinator.run(
        candidate_id="candidate-1", bead_id="bead-1", head_sha="a" * 40,
        approval_id="approval-1", operation_id="operation-1", idempotency_key="merge:candidate-1",
    )

    assert result.status == "closed"
    assert provider.merge_calls == 1
    assert provider.closed == [("bead-1", "candidate-1", "a" * 40, "operation-1")]

    replay = coordinator.run(
        candidate_id="candidate-1", bead_id="bead-1", head_sha="a" * 40,
        approval_id="approval-1", operation_id="operation-1", idempotency_key="merge:candidate-1",
    )
    assert replay.status == "closed"
    assert provider.merge_calls == 1
    assert len(provider.closed) == 1


def test_ineligible_candidate_cannot_merge_or_close(tmp_path):
    journal = StateJournal(tmp_path / "state.sqlite3")
    provider = FakeProvider()
    coordinator = MergeCoordinator(journal, provider, FakeEligibility(VerificationResult("not_ready", "a" * 40, ("missing approval",))))
    with pytest.raises(CoordinatorError, match="not eligible"):
        coordinator.run(
            candidate_id="candidate-1", bead_id="bead-1", head_sha="a" * 40,
            approval_id="approval-1", operation_id="operation-1", idempotency_key="merge:candidate-1",
        )
    assert provider.merge_calls == 0
    assert provider.closed == []


def test_merge_timeout_re_reads_provider_state_without_second_merge(tmp_path):
    journal = StateJournal(tmp_path / "state.sqlite3")
    provider = FakeProvider()
    coordinator = MergeCoordinator(journal, provider, FakeEligibility(VerificationResult("eligible", "a" * 40)))
    journal.issue_approval(
        approval_id="approval-1", candidate_id="candidate-1", head_sha="a" * 40,
        actor="human@example.com", scope="merge_this_candidate",
    )
    coordinator.run(
        candidate_id="candidate-1", bead_id="bead-1", head_sha="a" * 40,
        approval_id="approval-1", operation_id="operation-1", idempotency_key="merge:candidate-1",
    )
    assert journal.operation("operation-1").status == "closed"
    assert provider.merge_calls == 1


def test_approval_consumption_conflict_stops_coordinator(tmp_path):
    journal = StateJournal(tmp_path / "state.sqlite3")
    provider = FakeProvider()
    coordinator = MergeCoordinator(journal, provider, FakeEligibility(VerificationResult("eligible", "a" * 40)))
    with pytest.raises(CoordinatorError, match="approval"):
        coordinator.run(
            candidate_id="candidate-1", bead_id="bead-1", head_sha="a" * 40,
            approval_id="missing-approval", operation_id="operation-1", idempotency_key="merge:candidate-1",
        )
    assert provider.merge_calls == 0
