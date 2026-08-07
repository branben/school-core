"""Tests for the bd (beads) kanban bridge in conductor.py.

Covers:
  - _map_domain_from_issue_type: issue_type → domain mapping
  - _build_task_from_issue: issue dict → task string
  - _fetch_ready_from_kanban: bd ready --json parsing + 4-tuple mapping
  - _fetch_dispatch_tasks: kanban-first, _default_tasks fallback
  - _complete_kanban_task: bd close invocation on success/failure paths
  - _run_sync_loop: 4-tuple + 2-tuple unpacking, bd close after dispatch
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock, mock_open

import pytest

from conductor import (
    _map_domain_from_issue_type,
    _build_task_from_issue,
    _fetch_ready_from_kanban,
    _fetch_dispatch_tasks,
    _complete_kanban_task,
    _default_tasks,
    DOMAIN_ROLE,
)


# ── _map_domain_from_issue_type ──────────────────────────────────────────

class TestMapDomain:
    def test_bug_maps_to_debugging(self):
        assert _map_domain_from_issue_type("bug", "title", "desc") == "debugging"

    def test_bug_with_test_in_title_not_misclassified(self):
        # The whole point: title keyword matching would map this to python-testing
        assert _map_domain_from_issue_type("bug", "test failures in CI", "desc") == "debugging"

    def test_enhancement_maps_to_code_implementation(self):
        assert _map_domain_from_issue_type("enhancement", "title", "desc") == "code-implementation"

    def test_chore_maps_to_default(self):
        assert _map_domain_from_issue_type("chore", "title", "desc") == "_default"

    def test_task_maps_to_default(self):
        assert _map_domain_from_issue_type("task", "title", "desc") == "_default"

    def test_unknown_type_maps_to_default(self):
        assert _map_domain_from_issue_type("unknown", "title", "desc") == "_default"

    def test_empty_type_maps_to_default(self):
        assert _map_domain_from_issue_type("", "title", "desc") == "_default"


# ── _build_task_from_issue ───────────────────────────────────────────────

class TestBuildTask:
    def test_builds_task_from_title_and_description(self):
        issue = {"title": "Fix the bug", "description": "GitHub: https://github.com/owner/repo/issues/1\nDo something"}
        task = _build_task_from_issue(issue)
        assert "Fix the bug" in task
        assert "GitHub: https://github.com/owner/repo/issues/1" in task

    def test_handles_missing_description(self):
        issue = {"title": "Just a title"}
        task = _build_task_from_issue(issue)
        assert task == "Just a title"

    def test_handles_missing_title(self):
        issue = {"description": "Just a description"}
        task = _build_task_from_issue(issue)
        assert "Just a description" in task

    def test_handles_empty_dict(self):
        assert _build_task_from_issue({}) == ""


# ── _fetch_ready_from_kanban ─────────────────────────────────────────────

class TestFetchReadyFromKanban:
    @patch("conductor.subprocess.run")
    def test_parses_valid_json_into_4_tuples(self, mock_run):
        issues = [
            {"id": "school-core-123", "title": "Fix bug", "description": "Fix it", "issue_type": "bug"},
            {"id": "school-core-456", "title": "Add feature", "description": "Add it", "issue_type": "enhancement"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(issues),
            stderr="",
        )
        tasks = _fetch_ready_from_kanban()
        assert len(tasks) == 2
        assert tasks[0] == ("debugging", "Fix bug\n\nFix it", "coder", "school-core-123")
        assert tasks[1] == ("code-implementation", "Add feature\n\nAdd it", "coder", "school-core-456")

    @patch("conductor.subprocess.run")
    def test_returns_empty_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert _fetch_ready_from_kanban() == []

    @patch("conductor.subprocess.run")
    def test_returns_empty_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        assert _fetch_ready_from_kanban() == []

    @patch("conductor.subprocess.run")
    def test_returns_empty_on_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert _fetch_ready_from_kanban() == []

    @patch("conductor.subprocess.run")
    def test_returns_empty_on_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        assert _fetch_ready_from_kanban() == []

    @patch("conductor.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        mock_run.side_effect = __import__("subprocess").TimeoutExpired(cmd="bd", timeout=30)
        assert _fetch_ready_from_kanban() == []

    @patch("conductor.subprocess.run")
    def test_maps_chore_to_default_role(self, mock_run):
        issues = [{"id": "task-1", "title": "Cleanup", "description": "", "issue_type": "chore"}]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
        tasks = _fetch_ready_from_kanban()
        domain, task, role, bd_id = tasks[0]
        assert domain == "_default"
        assert role == DOMAIN_ROLE["_default"]
        assert bd_id == "task-1"

    @patch("conductor.subprocess.run")
    def test_skips_non_dict_issues(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(["not a dict", {"id": "ok", "title": "ok", "description": "", "issue_type": "bug"}]),
            stderr="",
        )
        tasks = _fetch_ready_from_kanban()
        assert len(tasks) == 1
        assert tasks[0][3] == "ok"


# ── _fetch_dispatch_tasks ────────────────────────────────────────────────

class TestFetchDispatchTasks:
    @patch("conductor._fetch_ready_from_kanban")
    @patch("conductor._default_tasks")
    def test_returns_kanban_tasks_when_available(self, mock_default, mock_kanban):
        mock_kanban.return_value = [("debugging", "task", "tester", "bd-1")]
        mock_default.return_value = [("code-search", "fallback")]
        result = _fetch_dispatch_tasks()
        assert result == [("debugging", "task", "tester", "bd-1")]
        mock_default.assert_not_called()

    @patch("conductor._fetch_ready_from_kanban")
    @patch("conductor._default_tasks")
    def test_falls_back_to_default_when_kanban_empty(self, mock_default, mock_kanban):
        mock_kanban.return_value = []
        mock_default.return_value = [("code-search", "fallback"), ("testing", "another")]
        result = _fetch_dispatch_tasks()
        assert result == [("code-search", "fallback"), ("testing", "another")]


# ── _complete_kanban_task ────────────────────────────────────────────────

class TestCompleteKanbanTask:
    @patch("conductor.subprocess.run")
    def test_closes_task_successfully(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert _complete_kanban_task("school-core-abc") is True

    def test_returns_false_on_none_id(self):
        assert _complete_kanban_task(None) is False
        assert _complete_kanban_task("") is False

    @patch("conductor.subprocess.run")
    def test_returns_false_on_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="issue not found")
        assert _complete_kanban_task("bad-id") is False

    @patch("conductor.subprocess.run")
    def test_returns_false_on_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        assert _complete_kanban_task("bd-1") is False

    @patch("conductor.subprocess.run")
    def test_returns_false_on_timeout(self, mock_run):
        mock_run.side_effect = __import__("subprocess").TimeoutExpired(cmd="bd", timeout=15)
        assert _complete_kanban_task("bd-1") is False


# ── _run_sync_loop: 4-tuple integration ───────────────────────────────────

class TestSyncLoopKanbanIntegration:
    @patch("conductor._principal_dispatch")
    @patch("conductor._complete_kanban_task")
    @patch("conductor._fetch_dispatch_tasks")
    @patch("conductor._score_and_print_round")
    def test_sync_loop_handles_4_tuple_and_closes_bd_id(self, mock_score, mock_fetch, mock_close, mock_dispatch):
        from conductor import _run_sync_loop

        class FakeArgs:
            rounds = 1
            difficulty = "easy"
            repo = "__global__"
            doubt_enabled = False

        mock_fetch.return_value = [("debugging", "test task", "tester", "school-core-test")]
        mock_dispatch.return_value = {"status": "success", "bead": "test-bead"}
        mock_score.return_value = None

        _run_sync_loop(FakeArgs(), MagicMock())

        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs["task"] == "test task"
        assert call_kwargs["role"] == "tester"
        assert call_kwargs["domain"] == "debugging"
        mock_close.assert_called_once_with("school-core-test")

    @patch("conductor._principal_dispatch")
    @patch("conductor._complete_kanban_task")
    @patch("conductor._fetch_dispatch_tasks")
    @patch("conductor._score_and_print_round")
    def test_sync_loop_handles_2_tuple_and_skips_close(self, mock_score, mock_fetch, mock_close, mock_dispatch):
        from conductor import _run_sync_loop

        class FakeArgs:
            rounds = 1
            difficulty = "easy"
            repo = "__global__"
            doubt_enabled = False

        mock_fetch.return_value = [("code-search", "fallback task")]
        mock_dispatch.return_value = {"status": "success", "bead": "test-bead"}
        mock_score.return_value = None

        _run_sync_loop(FakeArgs(), MagicMock())

        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs["task"] == "fallback task"
        mock_close.assert_called_once_with(None)


# ── Live smoke test ──────────────────────────────────────────────────────

class TestLiveSmoke:
    def test_live_bd_ready_returns_list_or_skips(self):
        """Smoke test: if bd is installed and configured, bd ready --json
        returns a list. If not, skips (pytest skip)."""
        import subprocess
        try:
            result = subprocess.run(["bd", "ready", "--json"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0 or not result.stdout.strip():
                pytest.skip("bd not configured or no ready issues")
            data = json.loads(result.stdout)
            assert isinstance(data, list)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("bd not installed")
