"""Lifecycle regression tests for late verification rejection.

When _select_verification() returns a real failure and reject_verification()
is called on the canonical packet, the bridge must NOT:
  1. Enqueue grading (evaluate_and_update with positive score)
  2. Record positive scores
  3. Report success
  4. Invoke publication (PR creation)

These tests exercise the actual bridge_issues() code path with mocked
dependencies to verify the lifecycle guard works correctly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import issue_bridge
from issue_bridge import bridge_issues
from review_packet import ReviewPacket
from scoring import ScoreStore


@pytest.fixture(autouse=True)
def _hermetic_bridge(monkeypatch, tmp_path):
    """Keep every bridge test hermetic."""
    def fake_gh(args, timeout=30):
        if args[:2] == ["label", "list"]:
            return "[]"
        return None
    monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
    monkeypatch.setattr(
        "issue_bridge.create_pr_for_issue",
        lambda **kwargs: "https://github.com/user/test/pull/1",
    )
    monkeypatch.setattr("issue_bridge._LABELS_ENSURED", set())
    monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry_issues.json")
    monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed_issues.json")
    monkeypatch.setattr("issue_bridge.CREW_RUNS_FILE", tmp_path / "crew_runs.json")
    monkeypatch.delenv("CREW_ENABLED", raising=False)
    monkeypatch.delenv("CREW_MAX_PER_CYCLE", raising=False)
    monkeypatch.delenv("CANDIDATE_PR_ENABLED", raising=False)
    monkeypatch.setattr("issue_bridge.notify_issue_alert", lambda *a, **k: True)
    monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
    monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
        "verdict": "PASS", "score": 90.0, "findings": [],
    })


def _make_task_result_with_packet(accepted=True):
    """Create a task result with a canonical review packet."""
    packet = ReviewPacket.create(
        accepted=accepted,
        cto={"verdict": "PASS", "score": 90},
        coo={"verdict": "PASS", "score": 85},
    )
    return {
        "status": "success",
        "agent": "foundry-coder-7b",
        "domain": "debugging",
        "difficulty": "easy",
        "prompt": "fix this",
        "response": "fixed",
        "review_packet": packet.to_dict(),
    }


def _make_failed_verify():
    """Create a failed verification result."""
    return {
        "passed": False,
        "ran": 2,
        "failures": [{"cmd": "pytest", "exit": 1, "stderr": "test failed"}],
    }


class TestLateVerificationLifecycleGuard:
    """Bridge must skip grading, scoring, and publication after reject."""

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_late_verify_failure_skips_pr_creation(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """After reject_verification(), PR creation must NOT be called."""
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [{
            "issue_number": 20, "title": "Late verify failure", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.return_value = _make_task_result_with_packet(accepted=True)

        # Mock _select_verification to return a failed verify result
        monkeypatch.setattr("issue_bridge._select_verification", lambda **kwargs: _make_failed_verify())

        # Track PR creation calls
        pr_calls = []
        def track_pr(**kwargs):
            pr_calls.append(kwargs)
            return "https://github.com/user/test/pull/1"
        monkeypatch.setattr("issue_bridge.create_pr_for_issue", track_pr)

        results = bridge_issues("user/test", store=store)

        # PR creation must NOT be called
        assert len(pr_calls) == 0, "PR creation must not be called after late verify failure"
        # Result must be error, not success
        assert results[0]["status"] == "error"
        assert "late verification failure" in results[0]["error"]

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_late_verify_failure_records_zero_score(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """After reject_verification(), score must be 0, not positive."""
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [{
            "issue_number": 21, "title": "Late verify failure", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.return_value = _make_task_result_with_packet(accepted=True)

        monkeypatch.setattr("issue_bridge._select_verification", lambda **kwargs: _make_failed_verify())

        # Track evaluate_and_update calls (patched at source: director module)
        eval_calls = []
        def track_eval(task_result, score, store=None):
            eval_calls.append(score)
            return {"old_score": 0, "new_score": score, "gate_crossed": False}
        monkeypatch.setattr("director.evaluate_and_update", track_eval)

        results = bridge_issues("user/test", store=store)

        # Score must be 0, not positive
        assert len(eval_calls) == 1
        assert eval_calls[0] == 0.0, f"Score must be 0 after late verify failure, got {eval_calls[0]}"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_accepted_packet_proceeds_normally(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """When packet is accepted, bridge proceeds normally."""
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [{
            "issue_number": 22, "title": "Accepted packet", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.return_value = _make_task_result_with_packet(accepted=True)

        # Mock _select_verification to return a passed verify result
        monkeypatch.setattr("issue_bridge._select_verification", lambda **kwargs: {
            "passed": True, "ran": 2, "failures": [],
        })

        # Track PR creation calls
        pr_calls = []
        def track_pr(**kwargs):
            pr_calls.append(kwargs)
            return "https://github.com/user/test/pull/1"
        monkeypatch.setattr("issue_bridge.create_pr_for_issue", track_pr)

        results = bridge_issues("user/test", store=store)

        # PR creation MUST be called
        assert len(pr_calls) == 1, "PR creation must be called when packet is accepted"
        # Result must be success
        assert results[0]["status"] == "success"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_no_canonical_packet_proceeds_normally(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """When there's no canonical packet, bridge proceeds normally."""
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [{
            "issue_number": 23, "title": "No canonical packet", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        # Task result without review_packet
        mock_task.return_value = {
            "status": "success",
            "agent": "foundry-coder-7b",
            "domain": "debugging",
            "difficulty": "easy",
            "prompt": "fix",
            "response": "fixed",
        }

        # Mock _select_verification to return a failed verify result
        monkeypatch.setattr("issue_bridge._select_verification", lambda **kwargs: _make_failed_verify())

        # Track PR creation calls
        pr_calls = []
        def track_pr(**kwargs):
            pr_calls.append(kwargs)
            return "https://github.com/user/test/pull/1"
        monkeypatch.setattr("issue_bridge.create_pr_for_issue", track_pr)

        results = bridge_issues("user/test", store=store)

        # PR creation MUST be called (no canonical packet to reject)
        assert len(pr_calls) == 1, "PR creation must be called when there's no canonical packet"
        # Result must be success
        assert results[0]["status"] == "success"
