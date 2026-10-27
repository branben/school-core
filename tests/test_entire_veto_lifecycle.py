"""Lifecycle regression tests for the pre-merge `entire` CRITICAL veto.

When `entire review` reports a CRITICAL finding, the bridge already injects it
into ``adversarial_review`` and sets ``adversarial_review["verdict"] = "FAIL"``
(issue_bridge.py:2757-2772). The lifecycle guard at issue_bridge.py:2896 is what
makes that verdict bite: it blocks grading, scoring, and publication. Before
that guard existed the verdict was a dead letter and ``create_pr_for_issue``
was still called.

These tests pin the intended contract: a CRITICAL pre-merge-sensor finding
must block grading, scoring, and publication — the same lifecycle the
verify-failure guard at issue_bridge.py:2827 already enforces.

Measured 2026-10-22: RED — the veto fires but the PR is still created.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import issue_bridge
from issue_bridge import bridge_issues
from review_packet import ReviewPacket


@pytest.fixture(autouse=True)
def _hermetic_bridge(monkeypatch, tmp_path):
    """Keep every bridge test hermetic."""
    def fake_gh(args, timeout=30):
        if args[:2] == ["label", "list"]:
            return "[]"
        return None
    monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
    monkeypatch.setattr("issue_bridge.create_pr_for_issue", lambda **kwargs: "https://github.com/user/test/pull/1")
    monkeypatch.setattr("issue_bridge._LABELS_ENSURED", set())
    monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry_issues.json")
    monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed_issues.json")
    monkeypatch.setattr("issue_bridge.CREW_RUNS_FILE", tmp_path / "crew_runs.json")
    monkeypatch.delenv("CREW_ENABLED", raising=False)
    monkeypatch.delenv("CREW_MAX_PER_CYCLE", raising=False)
    monkeypatch.delenv("CANDIDATE_PR_ENABLED", raising=False)
    monkeypatch.delenv("ENTIRE_HIGH_STRICT", raising=False)
    monkeypatch.setattr("issue_bridge.notify_issue_alert", lambda *a, **k: True)
    monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
    monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
        "verdict": "PASS", "score": 90.0, "findings": [],
    })


def _make_task_result_with_packet(accepted=True):
    """Create a task result with an authoritative canonical review packet."""
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


def _make_entire_critical():
    """A `entire review` result carrying one CRITICAL finding."""
    return {
        "status": "fail",
        "findings": [
            {
                "severity": "CRITICAL",
                "file": "src/app/main.py",
                "line": 42,
                "message": "unhandled exception path",
            }
        ],
    }


def _make_entire_high():
    """A `entire review` result carrying only a HIGH finding (not blocking)."""
    return {
        "status": "fail",
        "findings": [
            {
                "severity": "HIGH",
                "file": "src/app/main.py",
                "line": 42,
                "message": "style concern",
            }
        ],
    }


def _issue(num=30):
    return {
        "issue_number": num, "title": "Entire critical veto", "body": "",
        "domain": "debugging", "difficulty": "easy", "prompt": "fix",
        "category": "bug", "state": "ready-for-agent",
    }


class TestEntireCriticalVetoLifecycle:
    """A CRITICAL pre-merge sensor finding must skip grading, scoring, publication."""

    @patch("issue_bridge.fetch_issues")
    @patch("src.core.director.run_task")
    @patch("src.crew.executor.call_model")
    @patch("issue_bridge.call_model")
    def test_entire_critical_skips_pr_creation(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """A CRITICAL entire finding must NOT reach create_pr_for_issue."""
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [_issue(30)]
        mock_task.return_value = _make_task_result_with_packet(accepted=True)

        monkeypatch.setattr(
            "issue_bridge._run_entire_sensor", lambda *args: _make_entire_critical(),
        )

        pr_calls = []
        def track_pr(**kwargs):
            pr_calls.append(kwargs)
            return "https://github.com/user/test/pull/1"
        monkeypatch.setattr("issue_bridge.create_pr_for_issue", track_pr)

        results = bridge_issues("user/test", store=store)

        assert len(pr_calls) == 0, (
            "PR creation must not be called after a CRITICAL entire finding"
        )
        assert results[0]["status"] == "error"
        assert "critical pre-merge finding" in results[0]["error"].lower()
        assert "src/app/main.py:42" in results[0]["error"]

    @patch("issue_bridge.fetch_issues")
    @patch("src.core.director.run_task")
    @patch("src.crew.executor.call_model")
    @patch("issue_bridge.call_model")
    def test_entire_critical_records_zero_score(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """A CRITICAL entire finding must score 0, not a positive quality score."""
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [_issue(31)]
        mock_task.return_value = _make_task_result_with_packet(accepted=True)

        monkeypatch.setattr(
            "issue_bridge._run_entire_sensor", lambda *args: _make_entire_critical(),
        )

        eval_calls = []
        def track_eval(task_result, score, store=None):
            eval_calls.append(score)
            return {"old_score": 0, "new_score": score, "gate_crossed": False}
        monkeypatch.setattr("src.core.director.evaluate_and_update", track_eval)

        bridge_issues("user/test", store=store)

        assert eval_calls == [0.0], (
            f"Score must be 0 after a CRITICAL entire finding, got {eval_calls}"
        )

    @patch("issue_bridge.fetch_issues")
    @patch("src.core.director.run_task")
    @patch("src.crew.executor.call_model")
    @patch("issue_bridge.call_model")
    def test_entire_high_blocks_publication(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """A HIGH entire finding blocks publication.

        A finding the reviewer chose to emit is a veto (CRITICAL or HIGH).
        HIGH used to stay advisory unless ENTIRE_HIGH_STRICT=1, which left the
        sensor inert on real output because the parser defaulted unlabelled
        findings to HIGH.
        """
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        mock_fetch.return_value = [_issue(32)]
        mock_task.return_value = _make_task_result_with_packet(accepted=True)

        monkeypatch.setattr(
            "issue_bridge._run_entire_sensor", lambda *args: _make_entire_high(),
        )

        pr_calls = []
        def track_pr(**kwargs):
            pr_calls.append(kwargs)
            return "https://github.com/user/test/pull/1"
        monkeypatch.setattr("issue_bridge.create_pr_for_issue", track_pr)

        results = bridge_issues("user/test", store=store)

        assert len(pr_calls) == 0, "HIGH must block publication"
        assert results[0]["status"] == "error"
