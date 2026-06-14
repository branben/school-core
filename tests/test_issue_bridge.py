"""
Tests for U2: Issue→Task Bridge (issue_bridge.py)

Run: python -m pytest tests/test_issue_bridge.py -v
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from issue_bridge import (
    _load_processed,
    _save_processed,
    mark_processed,
    is_processed,
    bridge_issues,
    _run_adversarial_review,
    _heuristic_score,
    PROCESSED_FILE,
)


# ── Processed Issue Tracking ──────────────────────────────────────────────

class TestProcessedTracking:
    def test_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        assert _load_processed() == set()

    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        _save_processed({1, 2, 3})
        assert _load_processed() == {1, 2, 3}

    def test_mark_and_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        assert not is_processed(42)
        mark_processed(42)
        assert is_processed(42)

    def test_invalid_json_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "processed.json"
        f.write_text("not json")
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", f)
        assert _load_processed() == set()

    def test_multiple_marks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        for n in range(10):
            mark_processed(n)
            assert is_processed(n)
        assert len(_load_processed()) == 10


# ── Bridge Issues ─────────────────────────────────────────────────────────

class TestBridgeIssues:
    @patch("issue_bridge.fetch_issues")
    def test_empty_issues_returns_empty(self, mock_fetch):
        mock_fetch.return_value = []
        results = bridge_issues("user/test")
        assert results == []

    @patch("issue_bridge.fetch_issues")
    def test_skips_already_processed(self, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mark_processed(1)
        mock_fetch.return_value = [
            {"issue_number": 1, "title": "Already done", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "done", "category": "bug", "state": "ready-for-agent"},
        ]
        results = bridge_issues("user/test")
        assert len(results) == 0

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_dry_run_does_not_execute(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 5, "title": "Dry run test", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "test", "category": "bug", "state": "ready-for-agent"},
        ]
        results = bridge_issues("user/test", dry_run=True)
        assert len(results) == 1
        assert results[0]["status"] == "dry_run"
        mock_task.assert_not_called()

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_successful_bridge(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 10, "title": "Fix the thing", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "fix this",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "medium",
            "prompt": "fix this", "response": "ok",
        }
        results = bridge_issues("user/test")
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert results[0]["issue_number"] == 10
        # Should be marked processed
        assert is_processed(10)

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_task_failure_still_marks_processed(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 20, "title": "Flaky issue", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "fix",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {"status": "error", "error": "model unavailable"}
        results = bridge_issues("user/test")
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert is_processed(20)

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_handles_run_task_exception(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 30, "title": "Boom", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "boom",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.side_effect = RuntimeError("unexpected error")
        results = bridge_issues("user/test")
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "unexpected error" in results[0]["error"]


# ── Adversarial Review Integration ─────────────────────────────────────────

class TestAdversarialReviewStep:
    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_adversarial_review_attached_to_result(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 50, "title": "Review me", "body": "body",
             "domain": "code-implementation", "difficulty": "medium",
             "prompt": "implement", "category": "feature", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "code-implementation", "difficulty": "medium",
            "prompt": "implement", "response": "def foo(): pass",
        }
        results = bridge_issues("user/test")
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert "adversarial_review" in results[0]
        adv = results[0]["adversarial_review"]
        assert "verdict" in adv
        assert "score" in adv
        assert "findings" in adv

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_adversarial_review_failure_falls_back(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 51, "title": "Fallback test", "body": "",
             "domain": "debugging", "difficulty": "easy",
             "prompt": "fix", "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-1.5b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "fix", "response": "fixed",
        }
        # Patch the executor.call_model used by _run_adversarial_review to simulate failure
        with patch("executor.call_model", side_effect=RuntimeError("model unavailable")):
            results = bridge_issues("user/test")
        assert len(results) == 1
        assert results[0]["status"] == "success"
        adv = results[0]["adversarial_review"]
        assert adv["verdict"] == "PASS"
        assert adv["lens_used"] == "fallback"
        assert "error" in adv

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_combined_score_uses_all_three_signals(self, mock_task, mock_fetch, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 52, "title": "Score test", "body": "",
             "domain": "debugging", "difficulty": "medium",
             "prompt": "test", "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "medium",
            "prompt": "test", "response": "x" * 500,
        }
        results = bridge_issues("user/test")
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert "new_score" in results[0]

    def test_run_adversarial_review_returns_dict(self):
        task_result = {
            "status": "success",
            "response": "def hello(): return 'world'",
        }
        issue = {
            "title": "Hello world",
            "body": "Write a hello function",
            "domain": "code-implementation",
            "difficulty": "easy",
            "prompt": "Write a hello function",
        }
        result = _run_adversarial_review(task_result, issue, "")
        assert isinstance(result, dict)
        assert "verdict" in result
        assert "score" in result
        assert "findings" in result

    def test_run_adversarial_review_fallback_on_error(self):
        task_result = {"status": "success", "response": "code"}
        issue = {"title": "T", "body": "", "domain": "debugging", "difficulty": "easy", "prompt": "p"}
        with patch("executor.call_model", side_effect=ImportError("no module")):
            result = _run_adversarial_review(task_result, issue, "")
        assert result["verdict"] == "PASS"
        assert result["lens_used"] == "fallback"
        assert "error" in result

    def test_heuristic_score_easy(self):
        task_result = {"response": "x" * 200}
        issue = {"difficulty": "easy"}
        score = _heuristic_score(task_result, issue)
        assert 0.0 <= score <= 100.0

    def test_heuristic_score_hard(self):
        task_result = {"response": "x" * 200}
        issue = {"difficulty": "hard"}
        score = _heuristic_score(task_result, issue)
        hard_score = _heuristic_score(task_result, {"difficulty": "hard"})
        easy_score = _heuristic_score(task_result, {"difficulty": "easy"})
        assert hard_score >= easy_score

    def test_heuristic_score_empty_response(self):
        task_result = {"response": ""}
        issue = {"difficulty": "medium"}
        assert _heuristic_score(task_result, issue) == 0.0
