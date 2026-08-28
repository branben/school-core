"""Test for school-core-qb4: retry-budget-exhausted silent failures don't eat backlog."""
import pytest
from unittest.mock import patch, MagicMock
import issue_bridge


class TestRetryBudgetGate:
    """The retry-budget-exhausted branch must not eat the backlog."""

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.build_codebase_context")
    @patch("director.run_task")
    def test_retry_budget_silent_failure_keeps_issue_eligible(
        self, mock_task, mock_build, mock_clone, mock_cleanup,
        mock_fetch, tmp_path, monkeypatch, store,
    ):
        """A crew that dies silent (status=timeout) when retry budget is exhausted
        should NOT mark the issue processed."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry.json")
        mock_clone.return_value = tmp_path / "repo"
        mock_build.return_value = "context"

        num = 999
        mock_fetch.return_value = [
            {"issue_number": num, "title": "Silent failure", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "test",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {"status": "timeout", "error": "timed out"}

        # Attempt 1 → retry scheduled, NOT processed
        issue_bridge.bridge_issues("user/test", store=store)
        assert not issue_bridge.is_processed(num)
        # Attempt 2 (retry budget exhausted) → silent failure stays eligible
        issue_bridge.bridge_issues("user/test", store=store)
        assert not issue_bridge.is_processed(num), "Silent failure should NOT mark issue processed"

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.build_codebase_context")
    @patch("director.run_task")
    def test_retry_budget_done_marks_processed(
        self, mock_task, mock_build, mock_clone, mock_cleanup,
        mock_fetch, tmp_path, monkeypatch, store,
    ):
        """A crew that completes (status=done) when retry budget is exhausted
        SHOULD mark the issue processed."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry.json")
        mock_clone.return_value = tmp_path / "repo"
        mock_build.return_value = "context"

        num = 998
        mock_fetch.return_value = [
            {"issue_number": num, "title": "Done", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "test",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {"status": "done", "score": 85}

        # Attempt 1 → retry scheduled
        issue_bridge.bridge_issues("user/test", store=store)
        assert not issue_bridge.is_processed(num)
        # Attempt 2 (retry budget exhausted) → done marks processed
        issue_bridge.bridge_issues("user/test", store=store)
        assert issue_bridge.is_processed(num), "Done status should mark issue processed"
