"""Test for school-core-qb4: retry-budget-exhausted silent failures don't eat backlog."""
import pytest
from unittest.mock import patch, MagicMock
import issue_bridge


@patch("issue_bridge.fetch_issues")
@patch("issue_bridge.dispatch_crew")
def test_retry_budget_silent_failure_keeps_issue_eligible(
    mock_dispatch, mock_fetch, tmp_path, monkeypatch, store
):
    """A crew that dies silent (status != done) when retry budget is exhausted
    should NOT mark the issue processed — it stays eligible for retry."""
    monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry.json")

    num = 999
    mock_fetch.return_value = [
        {"issue_number": num, "title": "Silent failure", "body": "",
         "domain": "debugging", "difficulty": "easy", "prompt": "test",
         "category": "bug", "state": "ready-for-agent"},
    ]
    # Crew dispatch returns a silent failure
    mock_dispatch.return_value = MagicMock(
        crew_id="fm-test-crew",
        status="timeout",
        report_path=None,
        fallback_reason="timeout",
        teardown_ok=True,
    )

    results = issue_bridge.bridge_issues("user/test", store=store, crew_enabled=True)

    processed = issue_bridge._load_processed()
    assert num not in processed, "Silent failure should NOT mark issue processed"


@patch("issue_bridge.fetch_issues")
@patch("issue_bridge.dispatch_crew")
def test_retry_budget_done_marks_processed(
    mock_dispatch, mock_fetch, tmp_path, monkeypatch, store
):
    """A crew that completes (status=done) when retry budget is exhausted
    SHOULD mark the issue processed."""
    monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry.json")

    num = 998
    mock_fetch.return_value = [
        {"issue_number": num, "title": "Done", "body": "",
         "domain": "debugging", "difficulty": "easy", "prompt": "test",
         "category": "bug", "state": "ready-for-agent"},
    ]
    # Crew dispatch returns success
    mock_dispatch.return_value = MagicMock(
        crew_id="fm-test-crew",
        status="done",
        report_path=str(tmp_path / "report.md"),
        fallback_reason=None,
        teardown_ok=True,
    )

    results = issue_bridge.bridge_issues("user/test", store=store, crew_enabled=True)

    processed = issue_bridge._load_processed()
    assert num in processed, "Done status should mark issue processed"
