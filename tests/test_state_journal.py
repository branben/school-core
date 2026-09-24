"""Behavioral tests for the transactional approval/CAS journal."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from state_journal import ConflictError, InvalidStateError, StateJournal


def test_approval_consumption_is_atomic_and_idempotent(tmp_path):
    journal = StateJournal(tmp_path / "merge-state.sqlite3")
    journal.issue_approval(
        approval_id="approval-1",
        candidate_id="candidate-1",
        head_sha="a" * 40,
        actor="human@example.com",
        scope="merge_this_candidate",
    )

    first = journal.consume_approval(
        approval_id="approval-1",
        candidate_id="candidate-1",
        head_sha="a" * 40,
        operation_id="operation-1",
        idempotency_key="consume:approval-1",
    )
    replay = journal.consume_approval(
        approval_id="approval-1",
        candidate_id="candidate-1",
        head_sha="a" * 40,
        operation_id="operation-1",
        idempotency_key="consume:approval-1",
    )

    assert first.status == "confirmed"
    assert replay == first
    assert len(journal.operations()) == 1
    assert journal.approval("approval-1").operation_id == "operation-1"


def test_only_one_concurrent_worker_can_consume_an_approval(tmp_path):
    journal = StateJournal(tmp_path / "merge-state.sqlite3")
    journal.issue_approval(
        approval_id="approval-1",
        candidate_id="candidate-1",
        head_sha="a" * 40,
        actor="human@example.com",
        scope="merge_this_candidate",
    )

    def consume(index):
        try:
            return journal.consume_approval(
                approval_id="approval-1",
                candidate_id="candidate-1",
                head_sha="a" * 40,
                operation_id=f"operation-{index}",
                idempotency_key=f"consume:approval-1:{index}",
            ).operation_id
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))

    assert sorted(results) == ["conflict", "operation-0"] or sorted(results) == ["conflict", "operation-1"]
    assert journal.approval("approval-1").state == "consumed"
    assert len(journal.operations()) == 1


def test_approval_identity_and_already_consumed_requests_fail_closed(tmp_path):
    journal = StateJournal(tmp_path / "merge-state.sqlite3")
    journal.issue_approval(
        approval_id="approval-1", candidate_id="candidate-1", head_sha="a" * 40,
        actor="human@example.com", scope="merge_this_candidate",
    )
    with pytest.raises(InvalidStateError, match="head_sha"):
        journal.consume_approval(
            approval_id="approval-1", candidate_id="candidate-1", head_sha="b" * 40,
            operation_id="operation-1", idempotency_key="wrong-head",
        )
    journal.consume_approval(
        approval_id="approval-1", candidate_id="candidate-1", head_sha="a" * 40,
        operation_id="operation-1", idempotency_key="consume:approval-1",
    )
    with pytest.raises(ConflictError, match="consumed"):
        journal.consume_approval(
            approval_id="approval-1", candidate_id="candidate-1", head_sha="a" * 40,
            operation_id="operation-2", idempotency_key="second-consume",
        )


def test_operation_journal_is_idempotent_and_preserves_retry_events(tmp_path):
    journal = StateJournal(tmp_path / "merge-state.sqlite3")
    started = journal.start_operation(
        operation_id="operation-1", idempotency_key="merge:candidate-1",
        candidate_id="candidate-1", head_sha="a" * 40, kind="merge_request",
    )
    replay = journal.start_operation(
        operation_id="operation-2", idempotency_key="merge:candidate-1",
        candidate_id="candidate-1", head_sha="a" * 40, kind="merge_request",
    )
    assert replay == started
    journal.record_event("operation-1", "requested", {"provider": "fake"})
    journal.record_event("operation-1", "failed", {"provider": "fake", "error": "timeout"})
    journal.record_event("operation-1", "confirmed", {"provider": "fake", "result": "merged"})
    journal.record_event("operation-1", "confirmed", {"provider": "fake", "result": "merged"})

    operation = journal.operation("operation-1")
    assert operation.status == "confirmed"
    assert [event["kind"] for event in operation.events] == ["requested", "failed", "confirmed", "confirmed"]


def test_malformed_approval_records_are_rejected(tmp_path):
    journal = StateJournal(tmp_path / "merge-state.sqlite3")
    with pytest.raises(InvalidStateError, match="head_sha"):
        journal.issue_approval(
            approval_id="approval-1", candidate_id="candidate-1", head_sha="short",
            actor="human@example.com", scope="merge_this_candidate",
        )
