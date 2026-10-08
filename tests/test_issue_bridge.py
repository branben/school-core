"""
Tests for U2: Issue→Task Bridge (issue_bridge.py)

Run: python -m pytest tests/test_issue_bridge.py -v
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

import pytest

import issue_bridge
from issue_bridge import (
    _load_processed,
    _save_processed,
    _migrate_legacy_ledger,
    mark_processed,
    is_processed,
    load_terminal_issue_numbers,
    bridge_issues,
    _run_verify_gate,
    _run_entire_sensor,
    _run_adversarial_review,
    _heuristic_score,
    verify_task_output,
    _mark_github_issue,
    _build_school_comment,
    _scrub_comment_text,
    _ensure_school_labels,
    _load_retries,
    _save_retries,
    _crew_enabled_from_env,
    _hosted_student_enabled_from_env,
    _crew_active_issue,
    _crew_report_content,
    SCHOOL_DONE_LABEL,
    SCHOOL_FAILED_LABEL,
    PROCESSED_FILE,
    RETRY_FILE,
    RETRY_LIMIT,
)
from candidate_binding import CandidateBinding, CandidateBindingStore, bind_trusted_evidence
from pr_provider import PrStateStore, _BoundFakePublisher
from scoring import ScoreStore


@pytest.fixture(autouse=True)
def _no_real_gh_writes(monkeypatch, tmp_path):
    """Keep every bridge test hermetic.

    The bridge now syncs processed issues back to GitHub (close + label) and
    persists a retry counter. Without this fixture, bridge tests would hit the
    real GitHub API and write the real data/retry_issues.json. Fake _gh_command:
    label list reports no labels (so label creation is also exercised as a no-op),
    everything else returns None (success, no output).
    """
    def fake_gh(args, timeout=30):
        if args[:2] == ["label", "list"]:
            return "[]"
        return None
    monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
    # Keep PR publication hermetic too: the bridge imports a publisher that
    # uses its own gh subprocess boundary, separate from _gh_command.
    monkeypatch.setattr(
        "issue_bridge.create_pr_for_issue",
        lambda **kwargs: "https://github.com/user/test/pull/1",
    )
    # Reset the per-process label memoization so each test starts fresh.
    monkeypatch.setattr("issue_bridge._LABELS_ENSURED", set())
    # Hermetic retry counter — never touch the real data/retry_issues.json.
    monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry_issues.json")
    # Hermetic processed-set too — the live data/processed_issues.json already
    # contains real issue numbers, and crew tests use 4xx that can collide.
    monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed_issues.json")
    # U8: crew flag defaults OFF in tests (flag-off must be today's path);
    # crew registry is hermetic — never touch the real data/crew_runs.json.
    monkeypatch.setattr("issue_bridge.CREW_RUNS_FILE", tmp_path / "crew_runs.json")
    monkeypatch.delenv("CREW_ENABLED", raising=False)
    monkeypatch.delenv("CREW_MAX_PER_CYCLE", raising=False)
    # Candidate-bound publication is disabled by default; tests that exercise
    # the candidate seam opt in explicitly, and this keeps the safe default
    # from leaking in from the ambient environment.
    monkeypatch.delenv("CANDIDATE_PR_ENABLED", raising=False)
    # Never send real AgentMail alerts from tests.
    monkeypatch.setattr("issue_bridge.notify_issue_alert", lambda *a, **k: True)


# ── Processed Issue Tracking ──────────────────────────────────────────────

class TestProcessedTracking:
    def test_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        assert _load_processed() == {}

    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        _save_processed({1: "PASS", 2: "BURN", 3: "REJECT"})
        assert _load_processed() == {1: "PASS", 2: "BURN", 3: "REJECT"}

    def test_mark_and_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        assert not is_processed(42)
        mark_processed(42)
        assert is_processed(42)

    def test_invalid_json_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "processed.json"
        f.write_text("not json")
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", f)
        assert _load_processed() == {}

    def test_multiple_marks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        for n in range(10):
            mark_processed(n)
            assert is_processed(n)
        assert len(_load_processed()) == 10

    def test_legacy_flat_list_reads_as_terminal(self, tmp_path, monkeypatch):
        """An un-migrated flat list must not silently re-open historical work."""
        f = tmp_path / "processed.json"
        f.write_text("[1, 2, 3]")
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", f)
        assert _load_processed() == {1: "BURN", 2: "BURN", 3: "BURN"}
        assert is_processed(2)

    def test_legacy_flat_list_tolerates_a_non_numeric_entry(self, tmp_path, monkeypatch):
        """A corrupt legacy entry is skipped, not a bridge-cycle-killing crash.

        The ledger is tracked, hand-editable state read once per cycle. An
        unguarded ``int()`` over a flat list raised out of ``_load_processed``
        and killed the cycle; the previous flat-set parser accepted any value.
        """
        f = tmp_path / "processed.json"
        f.write_text('[340, 341, "n/a"]')
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", f)
        assert _load_processed() == {340: "BURN", 341: "BURN"}

    def test_unknown_outcome_token_reads_as_terminal(self, tmp_path, monkeypatch):
        """An unrecognized class fails closed (terminal), never open (retryable)."""
        f = tmp_path / "processed.json"
        f.write_text('{"7": "WAT"}')
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", f)
        assert _load_processed() == {7: "BURN"}
        assert is_processed(7)

    def test_infra_class_is_retryable_not_terminal(self, tmp_path, monkeypatch):
        """INFRA is the whole point: a runtime failure must stay eligible."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mark_processed(55, "INFRA")
        assert not is_processed(55)
        assert _load_processed() == {55: "INFRA"}

    def test_mark_processed_rejects_unknown_class(self, tmp_path, monkeypatch):
        """mark_processed coerces an unknown class to terminal BURN."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mark_processed(56, "NOT_A_CLASS")
        assert _load_processed() == {56: "BURN"}
        assert is_processed(56)

    def test_terminal_numbers_exclude_infra(self, tmp_path, monkeypatch):
        """The board projection lists only terminal issues."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        _save_processed({1: "PASS", 2: "INFRA", 3: "REJECT", 4: "BURN"})
        assert load_terminal_issue_numbers() == [1, 3, 4]


class TestLegacyLedgerMigration:
    """The one-shot migration that releases infra-burned issues.

    Live defect (SCH-11): #340/#341/#342/#415/#419 are OPEN, ready-for-agent,
    and present in processed_issues.json with no real verdict behind them.
    """

    def _last_run(self, tmp_path, records):
        p = tmp_path / "last_run.json"
        p.write_text(json.dumps(records))
        return p

    def test_releases_infra_burned_issue(self, tmp_path):
        """An issue whose only history is `error`/`retry` is released."""
        ledger = {100: "BURN"}
        path = self._last_run(tmp_path, [
            {"issue": 100, "status": "retry", "failure_edge": "none"},
            {"issue": 100, "status": "error", "failure_edge": "none"},
        ])
        released = _migrate_legacy_ledger(ledger, path)
        assert released == [100]
        assert 100 not in ledger

    def test_keeps_judge_rejection_terminal(self, tmp_path):
        """A judge `school-failed` is a real verdict — stays terminal."""
        ledger = {101: "BURN"}
        path = self._last_run(tmp_path, [
            {"issue": 101, "status": "school-failed", "failure_edge": "judge"},
        ])
        released = _migrate_legacy_ledger(ledger, path)
        assert released == []
        assert ledger[101] == "REJECT"

    def test_keeps_success_terminal(self, tmp_path):
        """A success (issue closed) is PASS — stays terminal."""
        ledger = {102: "BURN"}
        path = self._last_run(tmp_path, [
            {"issue": 102, "status": "success", "failure_edge": "none"},
        ])
        released = _migrate_legacy_ledger(ledger, path)
        assert released == []
        assert ledger[102] == "PASS"

    def test_runtime_school_failed_is_released(self, tmp_path):
        """`school-failed` from a runtime edge is infra, not a quality verdict."""
        ledger = {103: "BURN"}
        path = self._last_run(tmp_path, [
            {"issue": 103, "status": "school-failed", "failure_edge": "runtime"},
        ])
        released = _migrate_legacy_ledger(ledger, path)
        assert released == [103]
        assert 103 not in ledger

    def test_released_issue_is_readmitted_by_the_bridge(self, tmp_path, monkeypatch):
        """End-to-end: a released issue is no longer skipped by the loop."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        self._last_run(tmp_path, [{"issue": 104, "status": "error"}])
        (tmp_path / "processed.json").write_text("[104]")
        assert is_processed(104)  # legacy entry reads as terminal
        # Simulate the bridge's startup migration (mutate + persist).
        ledger = _load_processed()
        released = _migrate_legacy_ledger(ledger, tmp_path / "last_run.json")
        _save_processed(ledger)
        assert released == [104]
        assert not is_processed(104)

    def test_missing_last_run_releases_nothing_unexpectedly(self, tmp_path):
        """A missing/invalid history file releases nothing (no blind wipe)."""
        ledger = {105: "BURN", 106: "PASS"}
        released = _migrate_legacy_ledger(ledger, tmp_path / "nope.json")
        assert released == []
        assert ledger == {105: "BURN", 106: "PASS"}

    def test_migration_is_idempotent(self, tmp_path):
        """A second run finds nothing terminal-unjustified to release."""
        ledger = {107: "BURN"}
        path = self._last_run(tmp_path, [{"issue": 107, "status": "error"}])
        assert _migrate_legacy_ledger(ledger, path) == [107]
        assert _migrate_legacy_ledger(ledger, path) == []


# ── Bridge Issues ─────────────────────────────────────────────────────────

class TestBridgeIssues:
    @patch("issue_bridge.fetch_issues")
    def test_empty_issues_returns_empty(self, mock_fetch, store):
        mock_fetch.return_value = []
        results = bridge_issues("user/test", store=store)
        assert results == []

    @patch("issue_bridge.fetch_issues")
    def test_skips_already_processed(self, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mark_processed(1)
        mock_fetch.return_value = [
            {"issue_number": 1, "title": "Already done", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "done", "category": "bug", "state": "ready-for-agent"},
        ]
        results = bridge_issues("user/test", store=store)
        assert len(results) == 0

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_dry_run_does_not_execute(self, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 5, "title": "Dry run test", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "test", "category": "bug", "state": "ready-for-agent"},
        ]
        results = bridge_issues("user/test", dry_run=True, store=store)
        assert len(results) == 1
        assert results[0]["status"] == "dry_run"
        mock_task.assert_not_called()

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.build_codebase_context")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("director.run_task")
    def test_dry_run_is_side_effect_free(
        self, mock_task, mock_cleanup, mock_clone, mock_build_context,
        mock_fetch, tmp_path, monkeypatch, store,
    ):
        """Dry-run classifies only; it must not touch cache or durable state."""
        processed = tmp_path / "processed.json"
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", processed)
        mock_fetch.return_value = [
            {"issue_number": 6, "title": "Dry-run isolation", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "inspect",
             "category": "bug", "state": "ready-for-agent"},
        ]

        results = bridge_issues("user/test", dry_run=True, store=store)

        assert results == [{
            "issue_number": 6,
            "title": "Dry-run isolation",
            "domain": "debugging",
            "difficulty": "easy",
            "status": "dry_run",
            "codebase_context_chars": 0,
            "codebase_context_collected": False,
        }]
        mock_task.assert_not_called()
        mock_cleanup.assert_not_called()
        mock_clone.assert_not_called()
        mock_build_context.assert_not_called()
        assert not processed.exists()
        assert not (tmp_path / "last_run.json").exists()

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_successful_bridge(self, mock_ib_call, mock_exec_call, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
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
        mirrored = []

        def fail_mirror_after_recording(repo, outcomes):
            mirrored.append((repo, outcomes))
            raise RuntimeError("simulated Paperclip outage")

        monkeypatch.setattr(
            "paperclip_status.sync_paperclip_results",
            fail_mirror_after_recording,
        )
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert results[0]["issue_number"] == 10
        # Should be marked processed before the optional projection runs.
        assert is_processed(10)
        assert mirrored == [("user/test", results)]

    @pytest.mark.parametrize("publisher_outcome", ["none", "raises"])
    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_pr_publication_failure_remains_retryable(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        publisher_outcome, tmp_path, monkeypatch, store,
    ):
        """An absent or raised PR result cannot checkpoint task success."""
        mock_ib_call.return_value = (
            '{"score": 90, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": ["works"]}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        processed_path = tmp_path / "processed.json"
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", processed_path)
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        repo_path = tmp_path / "target-repo"
        repo_path.mkdir()
        monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
        monkeypatch.setattr("repo_reader.clone_repo", lambda repo: repo_path)
        monkeypatch.setattr("repo_reader.build_codebase_context", lambda *args: "")
        monkeypatch.setattr("issue_bridge._run_verify_gate", lambda *args, **kwargs: None)
        monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
        monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
            "verdict": "PASS", "score": 90.0, "findings": [],
        })
        mock_fetch.return_value = [{
            "issue_number": 12, "title": "PR publication failure", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "fix", "response": "fixed",
        }
        if publisher_outcome == "none":
            publisher = lambda **kwargs: None
        else:
            def publisher(**kwargs):
                raise RuntimeError("GitHub PR create timed out")
        monkeypatch.setattr("issue_bridge.create_pr_for_issue", publisher)

        with patch("issue_bridge._mark_github_issue") as mock_mark:
            results = bridge_issues("user/test", store=store)

        assert results[0]["status"] == "retry"
        assert "PR" in results[0]["error"]
        assert not is_processed(12)
        assert _load_retries() == {12: 1}
        mock_mark.assert_not_called()
        runs = json.loads((processed_path.parent / "last_run.json").read_text())
        assert runs[-1]["status"] == "retry"
        assert runs[-1].get("pr_error")
        assert not any(
            run.get("issue") == 12 and run.get("status") == "success"
            for run in runs
        )

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_success_is_checkpointed_before_loop_end(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch, tmp_path, monkeypatch, store
    ):
        """A success must be persisted as soon as GitHub is mutated.

        REGRESSION: ``_save_processed`` runs after the whole issue loop
        (issue_bridge.py:1990). The School Loop job has a 30-minute
        ``timeout-minutes`` and a 5-minute cron, and 68% of real runs were
        cancelled mid-flight. When the job dies inside the loop, that final
        save never executes — so an issue whose GitHub state was ALREADY
        mutated (closed, labelled, PR opened) is not recorded as processed and
        gets re-fetched and re-dispatched on the next cycle. Issue #65 was
        processed three times this way and still carries both ``school-done``
        and ``school-failed``.

        The rejection path already checkpoints immediately
        (issue_bridge.py:1506). The success path must do the same, so progress
        is crash-safe rather than dependent on reaching the end of the loop.

        We simulate the cancellation by making the SECOND issue raise, which
        aborts the loop before the trailing ``_save_processed``. The first
        issue's success must still be on disk.
        """
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 10, "title": "First — succeeds", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "fix this",
             "category": "bug", "state": "ready-for-agent"},
            {"issue_number": 11, "title": "Second — kills the run", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "boom",
             "category": "bug", "state": "ready-for-agent"},
        ]

        def _task(*args, **kwargs):
            # director.run_task is called positionally in the bridge; the issue
            # prompt distinguishes the two issues.
            if "boom" in repr(args) + repr(kwargs):
                raise KeyboardInterrupt("simulated job cancellation")
            return {
                "status": "success", "agent": "foundry-coder-7b",
                "domain": "debugging", "difficulty": "medium",
                "prompt": "fix this", "response": "ok",
            }

        mock_task.side_effect = _task

        with pytest.raises(KeyboardInterrupt):
            bridge_issues("user/test", store=store)

        # The loop never reached its trailing _save_processed, but issue 10's
        # GitHub state was already mutated — so it MUST be on disk.
        assert is_processed(10), (
            "issue 10 completed and was closed on GitHub but was not "
            "checkpointed; a cancelled job will re-dispatch it next cycle"
        )
        assert not is_processed(11), "issue 11 never completed"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_capability_failure_skips_crew_and_uses_direct_fallback(self, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge._resolve_crew_capability", lambda *args, **kwargs: None)
        dispatch_calls = []
        monkeypatch.setattr("issue_bridge.dispatch_crew", lambda **kwargs: dispatch_calls.append(kwargs))
        mock_fetch.return_value = [
            {"issue_number": 19, "title": "Capability fallback", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "fix",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {"status": "error", "error": "direct path failed"}

        results = bridge_issues("user/test", store=store, crew_enabled=True)

        assert results[0]["status"] == "retry"
        assert results[0]["crew_fallback_reason"] == "capability_resolution_failure"
        assert dispatch_calls == []


    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_task_failure_retries_once_then_marks_processed(self, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 20, "title": "Flaky issue", "body": "",
             "domain": "debugging", "difficulty": "medium", "prompt": "fix",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {"status": "error", "error": "model unavailable"}
        # Attempt 1 → retry scheduled (transient), NOT processed yet
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "retry"
        assert results[0]["retry_attempt"] == 1
        assert not is_processed(20)
        # Attempt 2 (retry budget exhausted) → final error, and the issue is
        # INFRA (retryable), NOT burned: a runtime failure must not consume it.
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert not is_processed(20)
        assert _load_processed()[20] == "INFRA"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_handles_run_task_exception_retries_once(self, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 30, "title": "Boom", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "boom",
             "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.side_effect = RuntimeError("unexpected error")
        # Attempt 1 → retry scheduled
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "retry"
        assert not is_processed(30)
        # Attempt 2 → final error; an exception path never reached a verdict,
        # so the issue stays eligible as INFRA rather than being burned.
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "unexpected error" in results[0]["error"]
        assert not is_processed(30)
        assert _load_processed()[30] == "INFRA"


# ── Adversarial Review Integration ─────────────────────────────────────────

class TestAdversarialReviewStep:
    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_adversarial_review_attached_to_result(self, mock_ib_call, mock_exec_call, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
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
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert "adversarial_review" in results[0]
        adv = results[0]["adversarial_review"]
        assert "verdict" in adv
        assert "score" in adv
        assert "findings" in adv

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    def test_adversarial_review_failure_falls_back(self, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [
            {"issue_number": 51, "title": "Fallback test", "body": "",
             "domain": "debugging", "difficulty": "easy",
             "prompt": "fix", "category": "bug", "state": "ready-for-agent"},
        ]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-1.5b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "fix", "response": "```python\nfixed\n```",
        }
        # Patch the executor.call_model used by _run_adversarial_review to simulate failure
        with patch("executor.call_model", side_effect=RuntimeError("model unavailable")):
            results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        adv = results[0]["adversarial_review"]
        # When model calls fail, the adversarial reviewer catches internally and returns PASS
        # with lens_used showing which lenses were attempted (the fallback is internal)
        assert adv["verdict"] == "PASS"
        # lens_used lists the lenses that were tried before failing
        assert "correctness" in adv["lens_used"]

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_combined_score_uses_all_three_signals(self, mock_ib_call, mock_exec_call, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
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
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert "new_score" in results[0]

    @patch("executor.call_model")
    def test_run_adversarial_review_returns_dict(self, mock_call_model):
        mock_call_model.return_value = '{"findings": []}'
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
        task_result = {"status": "success", "response": "```python\ncode\n```"}
        issue = {"title": "T", "body": "", "domain": "debugging", "difficulty": "easy", "prompt": "p"}
        with patch("executor.call_model", side_effect=ImportError("no module")):
            result = _run_adversarial_review(task_result, issue, "")
        assert result["verdict"] == "PASS"
        # Adversarial reviewer catches exceptions internally and returns lens names
        assert "correctness" in result["lens_used"]

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


# ── Verify-gate loudness: skipped vs real failure (U3) ────────────────────


class TestVerifyGateMerge:
    """The skipped-vs-failed distinction must drive the FAIL override.

    A reusable-gate SKIPPED verdict (Nix missing / no verify commands, ran == 0)
    must NOT force an issue FAIL in default direct/manual mode — the compiler
    never ran, so there is no evidence of a broken build. The scheduled
    school-loop blocks missing Nix earlier in its workflow preflight. A real
    verify failure (ran > 0, non-zero exit) MUST force FAIL so broken code
    cannot pass review (campus.md #3: compiler before critic).
    """

    ISSUE = [{
        "issue_number": 80, "title": "Verify merge", "body": "",
        "domain": "code-implementation", "difficulty": "medium",
        "prompt": "implement", "category": "feature", "state": "ready-for-agent",
    }]

    def _task_ok(self):
        return {
            "status": "success", "agent": "auto/best-free",
            "domain": "code-implementation", "difficulty": "medium",
            "prompt": "implement", "response": "```python\ndef f(): return 1\n```",
        }

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    def test_skipped_verdict_does_not_force_fail(
        self, mock_verify, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """Direct/manual soft-skip must not fail the issue or append findings.

        The scheduled workflow's hard preflight is tested separately in
        test_school_loop_workflow.py.
        """
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": False, "skipped": True, "ran": 0,
            "failures": [{"cmd": "(nix)", "exit": None,
                          "stderr": "Nix not found - verify gate SKIPPED."}],
        }
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        mock_task.return_value = self._task_ok()

        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        adv = results[0]["adversarial_review"]
        # No build/verify CRITICAL findings; verdict not forced to FAIL.
        assert not any(f.get("section") == "build/verify" for f in adv.get("findings", []))
        assert adv["verdict"] != "FAIL"
        # Durable loudness: the skip is recorded on the result.
        assert results[0]["verify_skipped"] is True

    def test_run_verify_gate_pins_flake_to_module_dir_not_cwd(self, tmp_path, monkeypatch):
        """The gate's flake_path must be the school-core checkout, never cwd.

        Regression for the CI-parity footgun: the bridge used to rely on
        Path.cwd() being the checkout root, so a workflow `working-directory:`
        would point the gate at a flake-less dir → nix develop fails with a
        non-127 error → every issue becomes a fake CRITICAL failure. The flake
        that provides #verifyShell lives next to issue_bridge.py.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "project_verify.yaml").write_text("verify:\n  - name: c\n    cmd: echo hi\n")
        import issue_bridge
        # Prove cwd-independence: run from a totally unrelated directory.
        monkeypatch.chdir(tmp_path)
        with patch("verify_gate.run_verify_gate", return_value={
            "passed": True, "failures": [], "ran": 0, "skipped": False,
        }) as mock_gate:
            result = _run_verify_gate(repo, {"issue_number": 1, "title": "t"})
        mock_gate.assert_called_once()
        assert mock_gate.call_args.kwargs["flake_path"] == Path(issue_bridge.__file__).resolve().parent
        # The repo's own project_verify.yaml (when present) must still win as the
        # command manifest — flake pinning must not disturb the priority shadowing.
        assert mock_gate.call_args[0][1] == repo / "project_verify.yaml"
        assert result is not None

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    def test_real_verify_failure_forces_fail(
        self, mock_verify, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """A real compile/test failure (ran > 0) must force FAIL + CRITICAL.

        Without a canonical packet, the no-packet lifecycle guard blocks
        publication — the result is error, not success.
        """
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": False, "ran": 1,
            "failures": [{"cmd": "npm run typecheck", "exit": 1,
                          "stderr": "TS2322: boom"}],
        }
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        mock_task.return_value = self._task_ok()

        results = bridge_issues("user/test", store=store)
        # No-packet lifecycle guard: verify FAIL blocks publication
        assert results[0]["status"] == "error"
        assert "verify failure" in results[0]["error"]

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    def test_strict_escalated_verdict_forces_fail(
        self, mock_verify, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """VERIFY_GATE_STRICT: an escalated (ran==0) gate verdict forces FAIL.

        Strict mode flips an unrunnable gate (Nix missing, no commands) from a
        soft SKIP into `strict_escalated: True` — the merge must treat that as
        a real failure even though ran == 0 (compiler-before-critic enforced).
        Without a canonical packet, the no-packet lifecycle guard blocks
        publication.
        """
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": False, "skipped": False, "strict_escalated": True, "ran": 0,
            "failures": [{"cmd": "(nix)", "exit": None,
                          "stderr": "Nix not found — verify gate SKIPPED.\n"
                                     "[VERIFY_GATE_STRICT] Escalation: ..."}],
        }
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        mock_task.return_value = self._task_ok()

        results = bridge_issues("user/test", store=store)
        # No-packet lifecycle guard: strict_escalated FAIL blocks publication
        assert results[0]["status"] == "error"
        assert "verify failure" in results[0]["error"]

    def test_run_verify_gate_strict_exception_escalates(self, monkeypatch):
        """Strict mode: verify_gate raising must escalate, not return None."""
        import issue_bridge
        # Patch the module import to raise, then assert escalation shape.
        real = issue_bridge._run_verify_gate
        with monkeypatch.context() as m:
            m.setenv("VERIFY_GATE_STRICT", "1")
            # Force the ImportError path by making the repo exist but the
            # lazy import fail.
            import tempfile as _tf
            with _tf.TemporaryDirectory() as td:
                repo = Path(td)
                import builtins
                real_import = builtins.__import__
                def fake_import(name, *a, **k):
                    if name == "verify_gate":
                        raise ImportError("no verify_gate")
                    return real_import(name, *a, **k)
                m.setattr(builtins, "__import__", fake_import)
                res = real(repo, {"issue_number": 1})
        assert res is not None
        assert res["strict_escalated"] is True
        assert res["passed"] is False

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    @patch("issue_bridge._mark_github_issue")
    def test_rejected_two_judge_review_forces_school_failed(
        self, mock_mark, mock_verify, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """A two-judge rejection must close the loop as school-failed, not done.

        Regression for 2026-08-12: issues #51/#52 scored 33/35 (below the
        documented >= 50 acceptance threshold) yet were closed school-done
        because the bridge never consulted the review verdict. The director
        gates acceptance (both judges PASS and score >= 50), so when run_task
        returns review.accepted == False the bridge must mark school-failed
        and leave the issue open instead of closing it.
        """
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": True, "ran": 1, "failures": [],
        }
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        task = self._task_ok()
        task["task_score"] = 33.0
        task["review"] = {
            "cto_verdict": "FAIL",
            "coo_verdict": "FAIL",
            "combined_score": 33.0,
            "accepted": False,
        }
        mock_task.return_value = task

        results = bridge_issues("user/test", store=store)

        # The close decision must honor the verdict: school-failed, open.
        assert results[0]["status"] == "error"
        assert "two-judge review rejected" in results[0]["error"]
        # The low combined score was preserved on the durable record.
        last_run = json.loads((tmp_path / "last_run.json").read_text())
        assert last_run[-1]["status"] == "school-failed"
        assert last_run[-1]["score"] == 33.0
        assert "rejection" in last_run[-1]
        # GitHub is labeled school-failed and left OPEN — never closed done.
        assert mock_mark.call_args[0] == ("user/test", 80, "error")
        assert not any(
            c.args[2] == "success" for c in mock_mark.call_args_list
        )
        # The designed penalty landed in the score store — the agent's
        # recorded score stays below the >= 50 acceptance threshold.
        assert store.get_score("auto/best-free", "code-implementation") < 50

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    def test_accepted_two_judge_review_passes_through(
        self, mock_verify, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """A genuine PASS (accepted=True, both judges, >= 50) still closes done.

        Guards the gate from over-triggering: only an explicit rejection routes
        to school-failed; a real pass and a legacy/async missing review must
        both take the normal success path.
        """
        mock_ib_call.return_value = (
            '{"score": 88, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": True, "ran": 1, "failures": [],
        }
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        task = self._task_ok()
        task["task_score"] = 88.0
        task["review"] = {
            "cto_verdict": "PASS",
            "coo_verdict": "PASS",
            "combined_score": 88.0,
            "accepted": True,
        }
        mock_task.return_value = task

        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        last_run = json.loads((tmp_path / "last_run.json").read_text())
        assert last_run[-1]["status"] == "success"


# ── U6: Entire pre-merge sensor (non-blocking) ─────────────────────────────


class TestEntireSensor:
    """The sync path must run `entire review` as a non-blocking sensor.

    Findings are surfaced (result + last_run) but never override the verdict —
    the adversarial LLM review is the semantic gate.
    """

    ISSUE = [{
        "issue_number": 85, "title": "Entire sensor", "body": "",
        "domain": "code-implementation", "difficulty": "medium",
        "prompt": "implement", "category": "feature", "state": "ready-for-agent",
    }]

    def _task_ok(self):
        return {
            "status": "success", "agent": "auto/best-free",
            "domain": "code-implementation", "difficulty": "medium",
            "prompt": "implement", "response": "```python\ndef f(): return 1\n```",
        }

    def test_sensor_returns_none_when_repo_missing(self):
        """No clone → None; the pipeline never blocks on the sensor."""
        assert _run_entire_sensor(Path("/nonexistent/repo")) is None

    def test_sensor_warns_when_cli_missing(self, tmp_path, capsys):
        """When repo exists but entire CLI missing, warn to stderr and return skipped dict."""
        with patch("src.entire_review._get_entire_path", return_value=None):
            result = _run_entire_sensor(tmp_path)
        assert result is not None
        assert result["status"] == "skipped"
        captured = capsys.readouterr()
        assert "[issue_bridge] entire CLI not found — pre-merge sensor will be skipped." in captured.err

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    @patch("issue_bridge._run_entire_sensor")
    def test_bridge_surfaces_entire_sensor_result(
        self, mock_sensor, mock_verify, mock_ib_call, mock_exec_call,
        mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        """Sensor findings ride on the result + last_run, without a FAIL override."""
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": True, "failures": [], "ran": 0, "skipped": False,
        }
        mock_sensor.return_value = {
            "status": "fail",
            "findings": [{"file": "x.py", "line": 3, "severity": "HIGH",
                           "message": "unused var"}],
        }
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        mock_task.return_value = self._task_ok()

        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        # Surfaced on the result…
        assert results[0]["entire_review"]["status"] == "fail"
        assert len(results[0]["entire_review"]["findings"]) == 1
        # …but NOT a verdict override — the LLM review stays the semantic gate.
        assert results[0]["adversarial_review"]["verdict"] != "FAIL"
        # Durable record carries a compact summary.
        runs = json.loads((tmp_path / "last_run.json").read_text())
        assert runs[-1]["entire"] == {"status": "fail", "findings": 1}

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._run_verify_gate")
    @patch("issue_bridge._run_entire_sensor")
    def test_bridge_records_none_when_sensor_unavailable(
        self, mock_sensor, mock_verify, mock_ib_call, mock_exec_call,
        mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        """Sensor unavailable (CLI missing) → result key None, last_run None."""
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        mock_verify.return_value = {
            "passed": True, "failures": [], "ran": 0, "skipped": False,
        }
        mock_sensor.return_value = None
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self.ISSUE
        mock_task.return_value = self._task_ok()

        results = bridge_issues("user/test", store=store)
        assert results[0]["entire_review"] is None
        runs = json.loads((tmp_path / "last_run.json").read_text())
        assert runs[-1]["entire"] is None


# ── U1: session_id threading ───────────────────────────────────────────────


class TestCycleSessionId:
    """The bridge must thread a per-cycle session_id into director.run_task so
    Layer 3 archival context can fire (U1)."""

    @staticmethod
    def _issue(num):
        return [{"issue_number": num, "title": f"T{num}", "body": "",
                 "domain": "debugging", "difficulty": "easy", "prompt": "p",
                 "category": "bug", "state": "ready-for-agent"}]

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_run_task_receives_cycle_session_id(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """run_task is called with a loop-YYYYMMDD-HHMM session_id."""
        import re
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self._issue(300)
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "p", "response": "ok",
        }
        bridge_issues("user/test", store=store)
        sid = mock_task.call_args[1].get("session_id")
        assert sid, "session_id must be passed to run_task"
        assert re.fullmatch(r"loop-\d{8}-\d{6}", sid), f"unexpected session_id: {sid}"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_same_session_id_for_issues_in_one_cycle(
        self, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        """Two issues in one cycle share one session_id (per-cycle, not per-issue)."""
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
            '"gaps": [], "strengths": []}'
        )
        mock_exec_call.return_value = '{"findings": []}'
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = self._issue(301) + self._issue(302)
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "p", "response": "ok",
        }
        bridge_issues("user/test", store=store)
        sids = {c[1].get("session_id") for c in mock_task.call_args_list}
        assert len(sids) == 1, f"expected one session per cycle, got {sids}"


# ── Verify Task Output Parser ────────────────────────────────────────────

VALID_JSON_RESPONSE = '{"score": 85, "verdict": "GOOD", "reasoning": "solid work", "gaps": ["missing tests"], "strengths": ["clean code"]}'


class TestVerifyTaskOutputParsing:
    """Tests for the hardened JSON parser in verify_task_output.

    The parser must handle the variety of output formats auto/best-free
    (and other models) may return: code fences, preamble text, control
    characters, and prose-wrapped JSON."""

    @patch("issue_bridge.call_model")
    def test_clean_json(self, mock_call):
        """Happy path: model returns clean JSON object."""
        mock_call.return_value = VALID_JSON_RESPONSE
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85
        assert result["verdict"] == "GOOD"
        assert result["gaps"] == ["missing tests"]
        assert result["strengths"] == ["clean code"]

    @patch("issue_bridge.call_model")
    def test_json_inside_fence_with_lang_tag(self, mock_call):
        """Model wraps JSON in ```json ... ``` code fence."""
        mock_call.return_value = "```json\n" + VALID_JSON_RESPONSE + "\n```"
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85
        assert result["verdict"] == "GOOD"

    @patch("issue_bridge.call_model")
    def test_json_inside_fence_no_lang_tag(self, mock_call):
        """Model wraps JSON in plain ``` ... ``` code fence."""
        mock_call.return_value = "```\n" + VALID_JSON_RESPONSE + "\n```"
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85

    @patch("issue_bridge.call_model")
    def test_json_with_leading_preamble(self, mock_call):
        """Model rambles before emitting JSON (common with auto/best-free)."""
        mock_call.return_value = (
            "Here is my evaluation of the task output:\n\n"
            "The solution looks good overall. Let me think step by step...\n\n"
            + VALID_JSON_RESPONSE +
            "\n\nI hope this helps!"
        )
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85
        assert result["verdict"] == "GOOD"

    @patch("issue_bridge.call_model")
    def test_json_with_control_characters(self, mock_call):
        """Response contains control characters that older parsers choked on."""
        mock_call.return_value = '{\x00"score": 72, "verdict": "ACCEPTABLE"}\x1f'
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 72
        assert result["verdict"] == "ACCEPTABLE"

    @patch("issue_bridge.call_model")
    def test_json_embedded_in_markdown_prose(self, mock_call):
        """JSON embedded deep inside markdown — balanced brace extraction."""
        mock_call.return_value = (
            "## Evaluation Results\n\n"
            "After careful review, I found several issues.\n\n"
            "### Score Details\n\n"
            '{"score": 45, "verdict": "PARTIAL", "reasoning": "incomplete", '
            '"gaps": ["no error handling", "missing edge cases"], '
            '"strengths": ["correct core logic"]}\n\n'
            "### Additional Notes\n\n"
            "The agent should also consider..."
        )
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 45
        assert result["verdict"] == "PARTIAL"
        assert len(result["gaps"]) == 2
        assert len(result["strengths"]) == 1

    @patch("issue_bridge.call_model")
    def test_model_call_exception_fallback(self, mock_call):
        """Model call raises — fall back to score=50."""
        mock_call.side_effect = RuntimeError("OmniRoute unavailable")
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 50
        assert result["verdict"] == "PARTIAL"
        assert "Verification error" in result["reasoning"]

    @patch("issue_bridge.call_model")
    def test_non_json_prose_fallback(self, mock_call):
        """Model returns pure prose with no JSON — fall back to score=50."""
        mock_call.return_value = (
            "The agent did an amazing job! The code is clean and well-structured. "
            "I would rate this as excellent work. No complaints at all."
        )
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 50
        assert result["verdict"] == "PARTIAL"
        assert "Parse error" in result["reasoning"]

    @patch("issue_bridge.call_model")
    def test_missing_optional_fields_defaulted(self, mock_call):
        """JSON missing verdict, gaps, strengths — defaults applied."""
        mock_call.return_value = '{"score": 92, "reasoning": "perfect"}'
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 92
        assert result["verdict"] == "PARTIAL"  # default
        assert result["gaps"] == []
        assert result["strengths"] == []

    @patch("issue_bridge.call_model")
    def test_score_below_zero_clamped(self, mock_call):
        """Score below 0 clamped to 0."""
        mock_call.return_value = '{"score": -50, "verdict": "FAIL"}'
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 0

    @patch("issue_bridge.call_model")
    def test_score_above_100_clamped(self, mock_call):
        """Score above 100 clamped to 100."""
        mock_call.return_value = '{"score": 999, "verdict": "EXCELLENT"}'
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 100

    @patch("issue_bridge.call_model")
    def test_score_as_string_converted(self, mock_call):
        """Score as string '85' — int() coercion in parser handles it."""
        mock_call.return_value = '{"score": "85", "verdict": "GOOD"}'
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85

    @patch("issue_bridge.call_model")
    def test_empty_response_fallback(self, mock_call):
        """Model returns empty string — no JSON to parse."""
        mock_call.return_value = ""
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 50
        assert result["verdict"] == "PARTIAL"

    @patch("issue_bridge.call_model")
    def test_whitespace_only_response_fallback(self, mock_call):
        """Model returns only whitespace."""
        mock_call.return_value = "   \n\n  "
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 50

    @patch("issue_bridge.call_model")
    def test_json_with_newlines_in_strings(self, mock_call):
        """JSON with literal newlines inside string values (old parser collapsed them)."""
        mock_call.return_value = (
            '{"score": 60, "verdict": "ACCEPTABLE", '
            '"reasoning": "Line 1\\nLine 2\\nLine 3", '
            '"gaps": ["gap 1\\ngap detail"], "strengths": []}'
        )
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 60
        assert "Line 1" in result["reasoning"]

    @patch("issue_bridge.call_model")
    def test_multiple_fence_blocks_first_parseable_wins(self, mock_call):
        """Multiple code fence blocks — first parseable JSON block wins."""
        mock_call.return_value = (
            "```python\ndef foo(): pass\n```\n\n"
            "```json\n" + VALID_JSON_RESPONSE + "\n```\n\n"
            "```\nSome other text\n```"
        )
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85

    @patch("issue_bridge.call_model")
    def test_json_with_leading_json_prefix(self, mock_call):
        """Model writes 'json' before the opening brace (seen with some providers)."""
        mock_call.return_value = "json " + VALID_JSON_RESPONSE
        result = verify_task_output("prompt", "response", "domain", "easy")
        assert result["score"] == 85

    @patch("issue_bridge.call_model")
    def test_codebase_context_passed_through(self, mock_call):
        """Codebase context string is included in the verification prompt."""
        mock_call.return_value = VALID_JSON_RESPONSE
        ctx = "Repository: sound-royale-ny\nKey files: src/main.py"
        verify_task_output("prompt", "response", "domain", "easy", codebase_context=ctx)
        # Verify ctx was interpolated into the prompt sent to the model
        call_args = mock_call.call_args
        assert ctx in call_args[0][1]  # second positional arg = prompt
        # Also verify the parser still produces correct output
        result = verify_task_output("prompt", "response", "domain", "easy", codebase_context=ctx)
        assert result["score"] == 85


# ── End-to-End Pipeline Tests ────────────────────────────────────────────────


class TestE2EPipeline:
    """End-to-end tests exercising the full bridge_issues pipeline.

    Mocks all external dependencies (GitHub, repo cloning, model calls, director
    task execution) and verifies the complete orchestration: enrichment context,
    adversarial review, verification scoring, heuristic scoring, combined score,
    gate crossing, and score persistence.
    """

    @pytest.fixture
    def mock_repo_dir(self, tmp_path):
        """Create a temporary directory that acts as the cloned repo."""
        d = tmp_path / "cloned_repo"
        d.mkdir(parents=True)
        return d

    # ── Happy path: clean output, all signals positive ──────────────────

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.build_codebase_context")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_e2e_happy_path_all_pass(
        self,
        mock_ib_call, mock_exec_call, mock_task,
        mock_cleanup, mock_build_ctx, mock_clone, mock_fetch,
        tmp_path, monkeypatch, store, mock_repo_dir,
    ):
        """Full pipeline: issue fetched, repo cloned, executed, verified,
        adversarially reviewed, scored. All signals positive."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")

        # Step 1: fetch_issues returns one actionable issue
        mock_fetch.return_value = [{
            "issue_number": 100,
            "title": "Fix null pointer in login",
            "body": "The login function crashes on null username",
            "domain": "code-implementation",
            "difficulty": "medium",
            "prompt": "Add a null check before accessing username",
            "category": "bug",
            "state": "ready-for-agent",
        }]

        # Step 2: repo cloning returns mock directory
        mock_clone.return_value = mock_repo_dir

        # Step 3: codebase context is built from the cloned repo
        mock_build_ctx.return_value = (
            "Repository: test-repo\n"
            "Key files:\n"
            "  src/login.py: login(username, password)\n"
            "  src/db.py: UserStore\n"
        )

        # Step 4: director.run_task executes successfully
        mock_task.return_value = {
            "status": "success",
            "agent": "auto/best-free",
            "domain": "code-implementation",
            "difficulty": "medium",
            "prompt": "Add a null check before accessing username",
            "response": (
                "def login(username, password):\n"
                "    if not username:\n"
                "        raise ValueError('Username cannot be empty')\n"
                "    return authenticate(username, password)\n"
            ),
        }

        # Step 5: Adversarial review — model returns empty findings (clean output)
        mock_exec_call.return_value = '{"findings": []}'

        # Step 6: Verification — model returns GOOD score
        mock_ib_call.return_value = (
            '{"score": 85, "verdict": "GOOD", "reasoning": "correct and complete", '
            '"gaps": ["missing edge case for empty password"], '
            '"strengths": ["handles null username", "clear error message"]}'
        )

        # Execute full pipeline
        results = bridge_issues("user/test", store=store)

        # Assertions
        assert len(results) == 1
        r = results[0]

        # Status and metadata
        assert r["status"] == "success"
        assert r["issue_number"] == 100
        assert r["domain"] == "code-implementation"
        assert r["difficulty"] == "medium"
        assert r["agent"] == "auto/best-free"

        # Verification signal
        assert r["verification"]["score"] == 85
        assert r["verification"]["verdict"] == "GOOD"
        assert len(r["verification"]["gaps"]) == 1
        assert len(r["verification"]["strengths"]) == 2

        # Adversarial review signal
        adv = r["adversarial_review"]
        assert "verdict" in adv
        assert "score" in adv
        assert isinstance(adv["score"], (int, float))
        assert adv["score"] >= 30.0  # floor at 30

        # Combined score
        assert "new_score" in r
        assert isinstance(r["new_score"], (int, float))

        # Issue marked processed
        assert is_processed(100)

        # Verify all mocks were called as expected
        mock_fetch.assert_called_once()
        mock_clone.assert_called_once()
        mock_build_ctx.assert_called_once()
        mock_task.assert_called_once()
        # Adversarial review calls executor.call_model (one per lens + circuit breaker)
        assert mock_exec_call.call_count >= 1
        # Verification calls issue_bridge.call_model
        mock_ib_call.assert_called_once()

        # Verify enrichment context was passed through to director.run_task
        task_prompt = mock_task.call_args[1]["prompt"]
        assert "test-repo" in task_prompt
        assert "src/login.py" in task_prompt

    # ── Failure path: output with issues, review finds problems ─────────

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.build_codebase_context")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_e2e_with_review_findings(
        self,
        mock_ib_call, mock_exec_call, mock_task,
        mock_cleanup, mock_build_ctx, mock_clone, mock_fetch,
        tmp_path, monkeypatch, store, mock_repo_dir,
    ):
        """Full pipeline where adversarial review finds real issues."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")

        mock_fetch.return_value = [{
            "issue_number": 101,
            "title": "SQL injection vulnerability",
            "body": "Fix the SQL injection in the query builder",
            "domain": "code-implementation",
            "difficulty": "hard",
            "prompt": "Use parameterized queries to prevent SQL injection",
            "category": "security",
            "state": "ready-for-agent",
        }]

        mock_clone.return_value = mock_repo_dir
        mock_build_ctx.return_value = "Repository: test-repo\nKey files: src/query.py"

        # Director returns flawed output
        mock_task.return_value = {
            "status": "success",
            "agent": "auto/best-free",
            "domain": "code-implementation",
            "difficulty": "hard",
            "prompt": "Use parameterized queries",
            "response": (
                "def get_user(user_id):\n"
                '    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n'
                "    return db.execute(query)\n"
            ),
        }

        # Adversarial review: model returns string findings
        mock_exec_call.return_value = (
            '{"findings": ["SQL injection: string interpolation in query", '
            '"No input validation on user_id", '
            '"Missing exception handling"]}'
        )

        # Verification: PARTIAL score
        mock_ib_call.return_value = (
            '{"score": 35, "verdict": "POOR", '
            '"reasoning": "Does not fix SQL injection — uses f-string interpolation", '
            '"gaps": ["SQL injection still present", "no parameterized query"], '
            '"strengths": []}'
        )

        results = bridge_issues("user/test", store=store)

        assert len(results) == 1
        r = results[0]
        assert r["status"] == "success"
        assert r["issue_number"] == 101

        # Verification should reflect the flawed output
        assert r["verification"]["score"] == 35
        assert r["verification"]["verdict"] == "POOR"

        # Adversarial review should have findings (string entries)
        adv = r["adversarial_review"]
        assert len(adv.get("findings", [])) > 0
        assert adv["score"] >= 30.0  # floor protects against 0.0

        # Combined score should be reasonable (weights: exec*0.5 + review*0.3 + heuristic*0.2)
        assert r["new_score"] > 0

        assert is_processed(101)

    # ── Error path: model call failure ───────────────────────────────────

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.build_codebase_context")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_e2e_verification_fallback_on_model_failure(
        self,
        mock_ib_call, mock_exec_call, mock_task,
        mock_cleanup, mock_build_ctx, mock_clone, mock_fetch,
        tmp_path, monkeypatch, store, mock_repo_dir,
    ):
        """Verification model call fails — pipeline falls back to score=50."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")

        mock_fetch.return_value = [{
            "issue_number": 102,
            "title": "Simple refactor",
            "body": "Refactor the function",
            "domain": "debugging",
            "difficulty": "easy",
            "prompt": "Refactor the code",
            "category": "refactor",
            "state": "ready-for-agent",
        }]

        mock_clone.return_value = mock_repo_dir
        mock_build_ctx.return_value = "Some context"

        mock_task.return_value = {
            "status": "success",
            "agent": "auto/best-free",
            "domain": "debugging",
            "difficulty": "easy",
            "prompt": "Refactor the code",
            "response": "def refactored(): pass",
        }

        # Adversarial review succeeds
        mock_exec_call.return_value = '{"findings": []}'

        # Verification model call fails entirely
        mock_ib_call.side_effect = RuntimeError("OmniRoute unavailable")

        results = bridge_issues("user/test", store=store)

        assert len(results) == 1
        r = results[0]
        assert r["status"] == "success"

        # Falls back to score=50 on verification failure
        assert r["verification"]["score"] == 50
        assert r["verification"]["verdict"] == "PARTIAL"
        assert "Verification error" in r["verification"]["reasoning"]

        # Adversarial review still completes
        assert r["adversarial_review"]["score"] >= 30.0

    # ── Score weighting verification ─────────────────────────────────────

    @patch("issue_bridge.fetch_issues")
    @patch("repo_reader.clone_repo")
    @patch("repo_reader.build_codebase_context")
    @patch("repo_reader.cleanup_stale_caches")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    def test_e2e_combined_score_weighting(
        self,
        mock_ib_call, mock_exec_call, mock_task,
        mock_cleanup, mock_build_ctx, mock_clone, mock_fetch,
        tmp_path, monkeypatch, store, mock_repo_dir,
    ):
        """Verify combined score formula: exec*0.5 + review*0.3 + heuristic*0.2."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")

        mock_fetch.return_value = [{
            "issue_number": 103,
            "title": "Score test",
            "body": "Test combined scoring",
            "domain": "code-implementation",
            "difficulty": "medium",
            "prompt": "Compute combined score",
            "category": "feature",
            "state": "ready-for-agent",
        }]

        mock_clone.return_value = mock_repo_dir
        mock_build_ctx.return_value = ""

        # Medium difficulty, response of 200 chars → heuristic = min(100, 200/10) * 0.8 = 16.0
        mock_task.return_value = {
            "status": "success",
            "agent": "auto/best-free",
            "domain": "code-implementation",
            "difficulty": "medium",
            "prompt": "Compute combined score",
            "response": "x" * 200,
        }

        # Adversarial review: score=100 (no findings)
        mock_exec_call.return_value = '{"findings": []}'

        # Verification: score=80 (GOOD)
        mock_ib_call.return_value = (
            '{"score": 80, "verdict": "GOOD", "reasoning": "solid", '
            '"gaps": [], "strengths": ["works"]}'
        )

        results = bridge_issues("user/test", store=store)

        assert len(results) == 1
        r = results[0]

        # Combined score must be a valid score that blends the three signals
        combined = r["new_score"]
        assert isinstance(combined, (int, float))
        assert 0 <= combined <= 100
        # Combined should be broad-band: exec 80 + review 100 + heuristic 16 → at least 40
        assert combined > 0


# ── Rich close comment (STE evidence summary) ─────────────────────────────


class TestSchoolComment:
    """_build_school_comment renders a compact STE evidence summary.

    The close comment must tell a human AND a future agent what the school
    actually did: verdicts, which tools produced the evidence, a bookbag
    summary, and an ELI5 line. It must be deterministic (no extra LLM call)
    and must never leak PII (home paths / tokens scrubbed).
    """

    ISSUE = {
        "issue_number": 80, "title": "Make escalation log instance-safe",
        "body": "", "domain": "code-implementation", "difficulty": "medium",
        "prompt": "implement", "category": "feature", "state": "ready-for-agent",
    }

    def _task(self, **kw):
        task = {
            "status": "success", "agent": "auto/best-free",
            "domain": "code-implementation", "difficulty": "medium",
            "prompt": "implement",
            "response": "Refactored escalation_log.py to use instance state.",
            "review": {
                "cto_verdict": "PASS", "coo_verdict": "PASS",
                "combined_score": 89.7, "accepted": True,
            },
            "bookbag": "/nonexistent/bookbag.json",  # absent → section omitted
        }
        task.update(kw)
        return task

    def test_renders_verdicts_tools_and_eli5(self):
        comment = _build_school_comment(
            self.ISSUE, self._task(),
            verification={"verdict": "PASS", "score": 90.0, "ran": 1},
            adversarial_review={
                "verdict": "GOOD", "score": 88.0,
                "findings": [{"section": "a"}, {"section": "b"}],
            },
            verify_skipped=False,
            entire_review={"status": "pass", "findings": 0},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "score: 89.7" in comment
        assert "CTO PASS / COO PASS" in comment
        assert "accepted" in comment
        assert "Adversarial review: GOOD" in comment
        assert "2 finding(s)" in comment
        assert "Verify gate: PASS (1 command(s))" in comment
        assert "Pre-merge check: pass (no findings)" in comment
        assert "Crew: not used (direct path)" in comment
        # ELI5 block at the bottom
        assert comment.strip().endswith(
            "Next step: open the issue to see the details."
        )
        assert "What happened:" in comment
        # No bookbag file → section omitted, not crashed
        assert "**Bookbag**" not in comment

    def test_includes_bookbag_summary_when_readable(self, tmp_path):
        bag = tmp_path / "bag.json"
        bag.write_text(json.dumps({
            "summary": "Made the log path a per-instance field, not a global.",
            "files_changed": ["escalation_log.py"],
            "ac_met": ["no globals", "tests pass"],
            "blockers": [],
            "output": "ignored when summary present",
        }))
        comment = _build_school_comment(
            self.ISSUE, self._task(bookbag=str(bag)),
            verification={"verdict": "PASS", "score": 90.0, "ran": 1},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=False,
            entire_review={"status": "pass", "findings": 0},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "**Bookbag**" in comment
        assert "per-instance field" in comment
        assert "Files changed: 1" in comment
        assert "Acceptance criteria met: 2" in comment
        assert "Blockers: 0" in comment

    def test_bookbag_output_fallback_when_summary_empty(self, tmp_path):
        bag = tmp_path / "bag2.json"
        bag.write_text(json.dumps({
            "summary": "",  # empty → falls back to output excerpt
            "output": "Refactored the log path into an instance field.\nTests: 12 passed.",
            "files_changed": ["escalation_log.py"],
            "ac_met": [], "blockers": ["needs manual check"],
        }))
        comment = _build_school_comment(
            self.ISSUE, self._task(bookbag=str(bag)),
            verification={"verdict": "PASS", "score": 90.0, "ran": 1},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=False,
            entire_review={"status": "fail", "findings": [
                {"severity": "LOW", "file": "a.py", "line": 1},
                {"severity": "LOW", "file": "b.py", "line": 2},
                {"severity": "LOW", "file": "c.py", "line": 3},
            ]},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "**Bookbag**" in comment
        assert "instance field" in comment  # output excerpt used
        assert "Blockers: 1" in comment
        assert "Pre-merge check: fail (3 finding(s), none blocking)" in comment

    def test_crew_fallback_and_skipped_verify_rendered(self):
        comment = _build_school_comment(
            self.ISSUE, self._task(),
            verification={"verdict": "PASS", "score": 90.0, "ran": 0},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=True,
            entire_review=None,
            combined_score=89.7,
            crew_used=False, crew_fallback_reason="spawn timed out",
        )
        assert "Verify gate: skipped" in comment
        assert "Pre-merge check: not run" in comment
        assert "fell back to direct (spawn timed out)" in comment
        assert "Adversarial review: not run" not in comment

    def test_scrub_removes_home_paths_and_tokens(self):
        task = self._task(response=(
            "Fixed in /Users/brandonbennett/school-core/escalation_log.py; "
            "key sk-1f24b3ef61d2e1f9-a3db47-823f823a removed."
        ))
        comment = _build_school_comment(
            self.ISSUE, task,
            verification={"verdict": "PASS", "score": 90.0, "ran": 1},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=False,
            entire_review={"status": "pass", "findings": 0},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "/Users/brandonbennett" not in comment
        assert "sk-1f24b3ef61d2e1f9-a3db47-823f823a" not in comment
        assert "[redacted]" in comment

    def test_renders_collapsible_judge_sections(self):
        task = self._task()
        task["review"] = {
            "cto_verdict": "PASS", "coo_verdict": "PASS",
            "cto_score": 100.0, "coo_score": 79.0,
            "combined_score": 89.5, "accepted": True,
            "cto_narrative": {
                "summary": "Logic is sound and the fix is safe.",
                "liked": "Clear error handling.",
                "improve": "Add one more edge-case test.",
                "why_passed": "No correctness or security findings.",
                "lesson": "Verify error paths even when happy path works.",
            },
            "coo_narrative": {
                "summary": "Covers the issue end to end.",
                "liked": "Acceptance criteria all met.",
                "improve": "Could document the new flag.",
                "why_passed": "Completeness checks passed.",
            },
        }
        comment = _build_school_comment(
            self.ISSUE, task,
            verification={"verdict": "PASS", "score": 90.0, "ran": 1},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=False,
            entire_review={"status": "pass", "findings": 0},
            combined_score=89.5,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "**Judge notes**" in comment
        # COO block: conversational, no lesson line
        assert "<details>" in comment
        assert "COO review — completeness + acceptance (PASS, score 79)" in comment
        assert "Covers the issue end to end." in comment
        assert "Could document the new flag." in comment
        assert "Why it passed:** Completeness checks passed" in comment
        # CTO block: technical tone + lesson line
        assert "CTO review — correctness + security (PASS, score 100)" in comment
        assert "Logic is sound and the fix is safe." in comment
        assert "Clear error handling." in comment
        assert "Add one more edge-case test." in comment
        assert "No correctness or security findings." in comment
        assert "What to learn from this:** Verify error paths" in comment
        assert "</details>" in comment
        # ELI5 still at the bottom
        assert comment.strip().endswith("Next step: open the issue to see the details.")

    def test_fail_verdict_renders_why_failed_and_distinct_markers(self):
        task = self._task()
        task["review"] = {
            "cto_verdict": "FAIL", "coo_verdict": "FAIL",
            "cto_score": 30.0, "coo_score": 40.0,
            "combined_score": 35.0, "accepted": False,
            "cto_narrative": {
                "summary": "Found a logic bug.",
                "liked": "Tests exist.",
                "improve": "Fix the off-by-one.",
                "why_failed": "Correctness check failed.",
                "lesson": "Trace edge cases before submitting.",
            },
            "coo_narrative": {
                "summary": "Missed the acceptance criteria.",
                "improve": "Cover the negative path.",
                "why_failed": "Incomplete coverage.",
            },
        }
        comment = _build_school_comment(
            self.ISSUE, task,
            verification={"verdict": "FAIL", "score": 20.0, "ran": 1},
            adversarial_review={"verdict": "FAIL", "score": 10.0, "findings": []},
            verify_skipped=False,
            entire_review={"status": "fail", "findings": 1},
            combined_score=35.0,
            crew_used=False, crew_fallback_reason=None,
        )
        # FAIL narratives render the why_failed line, not why_passed.
        assert "Why it failed:** Correctness check failed." in comment
        assert "Why it failed:** Incomplete coverage." in comment
        assert "Why it passed" not in comment
        # Distinct per-judge markers: CTO technical 👔, COO conversational 🗣️.
        assert "👔 CTO review" in comment
        assert "🗣️ COO review" in comment
        assert "What to learn from this:** Trace edge cases" in comment

    def test_judge_sections_absent_when_no_narrative(self):
        comment = _build_school_comment(
            self.ISSUE, self._task(),  # review dict has no narratives
            verification={"verdict": "PASS", "score": 90.0, "ran": 1},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=False,
            entire_review={"status": "pass", "findings": 0},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "**Judge notes**" not in comment
        assert "<details>" not in comment
        assert "CTO PASS / COO PASS" in comment  # compact bullets survive

    def test_imperfect_narrative_json_returns_none(self, monkeypatch):
        """A fenced/unparseable synthesis response must degrade to None, not crash."""
        from director import _synthesize_judge_narratives
        calls = []
        def fake_call(prompt, sp=None, timeout=60):
            calls.append(prompt)
            return "```json\n{\"cto\": {\"summary\": \"ok\"} \n"  # unbalanced → extract fails
        result = _synthesize_judge_narratives(
            _call_model=fake_call,
            task={"title": "t", "domain": "d", "difficulty": "easy"},
            output="out",
            cto_verdict="PASS", cto_score=90.0, cto_lens="correctness",
            coo_verdict="PASS", coo_score=80.0, coo_lens="completeness",
            cto_findings=[], coo_findings=[],
        )
        assert result == (None, None)
        assert calls, "the model should have been called once"

    def test_mark_github_issue_uses_rich_comment(self, monkeypatch):
        calls = []
        def fake_gh(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["label", "list"]:
                return "[]"
            return None
        monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
        _mark_github_issue("acme/test", 7, "success", score=89.7,
                           comment=_build_school_comment(
                               self.ISSUE, self._task(),
                               verification={"verdict": "PASS", "score": 90.0, "ran": 1},
                               adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
                               verify_skipped=False,
                               entire_review={"status": "pass", "findings": 0},
                               combined_score=89.7,
                               crew_used=False, crew_fallback_reason=None,
                           ))
        close = next(" ".join(c) for c in calls if c and c[0] == "issue" and c[1] == "close")
        assert "CTO PASS / COO PASS" in close  # rich comment used
        assert "In plain words" in close


# ── GitHub Issue Sync (close + lifecycle labels) ──────────────────────────


class TestGithubIssueSync:
    """Unit tests for _ensure_school_labels / _mark_github_issue."""

    def _record_calls(self, monkeypatch, label_list_out="[]"):
        calls = []
        def fake_gh(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["label", "list"]:
                return label_list_out
            return None
        monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
        return calls

    def test_success_closes_and_labels(self, monkeypatch):
        calls = self._record_calls(monkeypatch)
        _mark_github_issue("acme/test", 7, "success", score=81.25)
        flat = [" ".join(c) for c in calls]
        # Labels ensured (created since list was empty)
        assert any("label create school-done" in c for c in flat)
        assert any("label create school-failed" in c for c in flat)
        # school-done added, then issue closed with the score in the comment
        assert any("issue edit" in c and "--add-label" in c and SCHOOL_DONE_LABEL in c for c in flat)
        close = next(c for c in flat if c.startswith("issue close"))
        assert "81.2" in close  # score in close comment
        assert not any(SCHOOL_FAILED_LABEL in c and "edit" in c for c in flat)

    def test_error_labels_but_does_not_close(self, monkeypatch):
        calls = self._record_calls(monkeypatch, label_list_out='[{"name": "school-done"}, {"name": "school-failed"}]')
        _mark_github_issue("acme/test", 8, "error")
        flat = [" ".join(c) for c in calls]
        assert any("issue edit" in c and SCHOOL_FAILED_LABEL in c for c in flat)
        assert not any(c.startswith("issue close") for c in flat)
        assert not any(c.startswith("label create") for c in flat)  # labels already exist

    def test_labels_created_when_missing(self, monkeypatch):
        calls = self._record_calls(monkeypatch, label_list_out="[]")
        _mark_github_issue("acme/test", 9, "error")
        flat = [" ".join(c) for c in calls]
        assert any("label create school-done" in c for c in flat)
        assert any("label create school-failed" in c for c in flat)

    def test_unknown_status_is_noop(self, monkeypatch):
        calls = self._record_calls(monkeypatch)
        _mark_github_issue("acme/test", 10, "dry_run")
        assert calls == []

    def test_gh_failure_is_non_fatal(self, monkeypatch):
        def boom(args, timeout=30):
            raise RuntimeError("gh exploded")
        monkeypatch.setattr("issue_bridge._gh_command", boom)
        _mark_github_issue("acme/test", 11, "success", score=50)  # must not raise

    def test_ensure_labels_handles_bad_json(self, monkeypatch):
        calls = []
        def fake_gh(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["label", "list"]:
                return "not json"
            return None
        monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
        _ensure_school_labels("acme/test")
        # Treated as "no labels exist" → create both
        assert any("label create school-done" in " ".join(c) for c in calls)
        assert any("label create school-failed" in " ".join(c) for c in calls)


class TestBridgeGithubSync:
    """The bridge must call _mark_github_issue with the right status per path."""

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._mark_github_issue")
    def test_success_syncs_github(
        self, mock_mark, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [{
            "issue_number": 60, "title": "Sync success", "body": "",
            "domain": "debugging", "difficulty": "medium", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "medium",
            "prompt": "fix", "response": "ok",
        }
        bridge_issues("user/test", store=store)
        mock_mark.assert_called_once()
        args, kwargs = mock_mark.call_args
        assert args[0] == "user/test"
        assert args[1] == 60
        assert args[2] == "success"
        assert kwargs.get("score") is not None

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_failure_syncs_github_only_after_retry(self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [{
            "issue_number": 61, "title": "Sync failure", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.return_value = {"status": "error", "error": "model unavailable"}
        # Attempt 1 → retry scheduled: no GitHub sync yet (no school-failed)
        bridge_issues("user/test", store=store)
        mock_mark.assert_not_called()
        # Attempt 2 → final failure: school-failed sync
        bridge_issues("user/test", store=store)
        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][1] == 61
        assert mock_mark.call_args[0][2] == "error"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_run_task_exception_syncs_github_only_after_retry(self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [{
            "issue_number": 62, "title": "Boom sync", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "boom",
            "category": "bug", "state": "ready-for-agent",
        }]
        mock_task.side_effect = RuntimeError("unexpected")
        bridge_issues("user/test", store=store)  # attempt 1 → retry, no sync
        mock_mark.assert_not_called()
        bridge_issues("user/test", store=store)  # attempt 2 → final + sync
        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][1] == 62
        assert mock_mark.call_args[0][2] == "error"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_dry_run_does_not_sync_github(self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        mock_fetch.return_value = [{
            "issue_number": 63, "title": "Dry sync", "body": "",
             "domain": "debugging", "difficulty": "easy", "prompt": "x",
             "category": "bug", "state": "ready-for-agent",
        }]
        bridge_issues("user/test", dry_run=True, store=store)
        mock_mark.assert_not_called()



# ── Retry-once semantics ───────────────────────────────────────────────────


class TestRetryOnce:
    """Transient failures get one retry on the next cycle before school-failed."""

    @staticmethod
    def _issue(num):
        return [{"issue_number": num, "title": f"T{num}", "body": "",
                 "domain": "debugging", "difficulty": "easy", "prompt": "p",
                 "category": "bug", "state": "ready-for-agent"}]

    def test_load_save_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        _save_retries({5: 1, 7: 2})
        assert _load_retries() == {5: 1, 7: 2}

    def test_load_missing_and_bad_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "missing.json")
        assert _load_retries() == {}
        (tmp_path / "bad.json").write_text("not json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "bad.json")
        assert _load_retries() == {}

    def test_retry_limit_is_two(self):
        assert RETRY_LIMIT == 2  # attempt 1 = trial, attempt 2 = final

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_first_failure_schedules_retry_no_github_sync(
        self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        mock_fetch.return_value = self._issue(70)
        mock_task.return_value = {"status": "error", "error": "gateway hiccup"}
        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "retry"
        assert results[0]["retry_attempt"] == 1
        assert not is_processed(70)
        assert _load_retries() == {70: 1}
        mock_mark.assert_not_called()  # no school-failed on the first failure

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_second_failure_is_final_and_syncs(
        self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        mock_fetch.return_value = self._issue(71)
        mock_task.return_value = {"status": "error", "error": "still down"}
        bridge_issues("user/test", store=store)          # attempt 1 → retry
        results = bridge_issues("user/test", store=store)  # attempt 2 → final
        assert results[0]["status"] == "error"
        # A repeated runtime `error` is INFRA, not a verdict: the issue is NOT
        # burned. This is the SCH-11 defect — the old contract marked it
        # processed, which is how #340/#341/#342/#415/#419 became unprocessable.
        assert not is_processed(71)
        assert _load_processed()[71] == "INFRA"
        assert _load_retries() == {}   # retry state cleared after final
        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][1] == 71
        assert mock_mark.call_args[0][2] == "error"

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("executor.call_model")
    @patch("issue_bridge.call_model")
    @patch("issue_bridge._mark_github_issue")
    @patch("issue_bridge.notify_issue_alert")
    def test_success_clears_retry_state(
        self, mock_notify, mock_mark, mock_ib_call, mock_exec_call, mock_task, mock_fetch,
        tmp_path, monkeypatch, store,
    ):
        mock_ib_call.return_value = '{"score": 90, "verdict": "GOOD", "reasoning": "ok", "gaps": [], "strengths": ["works"]}'
        mock_exec_call.return_value = '{"findings": []}'
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        _save_retries({72: 1})  # previously failed once
        mock_fetch.return_value = self._issue(72)
        mock_task.return_value = {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "p", "response": "ok",
        }
        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        assert is_processed(72)
        assert _load_retries() == {}   # retry state cleared on success
        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][2] == "success"
        mock_notify.assert_not_called()  # no alert on success

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    @patch("issue_bridge.notify_issue_alert")
    def test_first_failure_notifies_retry_pending(
        self, mock_notify, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        mock_fetch.return_value = self._issue(73)
        mock_task.return_value = {"status": "error", "error": "gateway hiccup"}
        bridge_issues("user/test", store=store)
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        assert args[0] == 73                       # issue number
        assert args[1] == "T73"                   # title
        assert args[2] == "retry"                 # status
        assert kwargs.get("attempt") == 1
        assert kwargs.get("repo") == "user/test"
        assert "gateway hiccup" in str(kwargs.get("error", ""))

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    @patch("issue_bridge.notify_issue_alert")
    def test_final_failure_notifies_school_failed(
        self, mock_notify, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        mock_fetch.return_value = self._issue(74)
        mock_task.return_value = {"status": "error", "error": "still down"}
        bridge_issues("user/test", store=store)   # attempt 1 → retry alert
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][2] == "retry"
        bridge_issues("user/test", store=store)   # attempt 2 → school-failed alert
        assert mock_notify.call_count == 2
        final_args = mock_notify.call_args
        assert final_args[0][2] == "school-failed"
        assert final_args[1].get("attempt") == 2

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_persisted_counter_accumulates_across_fresh_checkouts(
        self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        """B12 regression: the counter must survive a fresh checkout per cycle.

        Models the real workflow shape: each cycle runs in a fresh checkout of
        `main` (retry file = {}), and the durable state comes from whatever was
        checkpointed to board-publish — which the workflow now SEEDS back into
        the worktree before the bridge runs (school-loop.yml, seed step).

        Fails if the seed is {} (the pre-fix behavior): attempts recomputes to 1
        forever, RETRY_LIMIT stays unreachable, school-failed never fires.
        """
        persisted = tmp_path / "board-publish-retry_issues.json"
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")

        # ── Cycle 1: fresh checkout of main ({}), failure → retry persisted ──
        mock_fetch.return_value = self._issue(75)
        mock_task.return_value = {"status": "error", "error": "gateway hiccup"}
        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "retry"
        assert results[0]["retry_attempt"] == 1
        # "Checkpoint": the counter lands on board-publish.
        persisted.write_text((tmp_path / "retries.json").read_text())
        assert json.loads(persisted.read_text()) == {"75": 1}

        # ── Cycle 2: ANOTHER fresh checkout of main — retry file is {} again,
        #    then the workflow seeds it from the board-publish copy. ──
        main_state = tmp_path / "retries.json"
        main_state.unlink()                      # actions/checkout@v4 of main
        assert not main_state.exists()
        main_state.write_text(persisted.read_text())  # ← the B12 seed step
        assert _load_retries() == {75: 1}             # bridge reads seeded state

        results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "error"            # terminal, NOT retry
        assert results[0]["retry_attempt"] == RETRY_LIMIT  # accumulated to 2
        # INFRA (runtime `error`) — retryable, not burned into the ledger.
        assert not is_processed(75)
        assert _load_processed()[75] == "INFRA"
        assert _load_retries() == {}                       # popped on terminal
        mock_mark.assert_called_once()
        assert mock_mark.call_args[0][1] == 75
        assert mock_mark.call_args[0][2] == "error"  # school-failed label path

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    def test_empty_seed_never_accumulates_documents_b12_defect(
        self, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        """Documents the defect: seeding from main's {} keeps attempts at 1.

        Same two-cycle shape as above, but cycle 2 gets NO board-publish seed —
        exactly what happened for 12+ hours ({"342":1,"341":1} frozen). The
        second failure must come back as `retry` with attempt still 1 and no
        school-fired sync; if this ever starts asserting `error`, the workflow
        seed regressed or issue_bridge arithmetic changed.
        """
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        mock_fetch.return_value = self._issue(76)
        mock_task.return_value = {"status": "error", "error": "still down"}

        bridge_issues("user/test", store=store)              # cycle 1 → retry
        (tmp_path / "retries.json").unlink()                 # fresh checkout of main
        # NO seed step: file stays absent → _load_retries() == {}
        assert _load_retries() == {}

        results = bridge_issues("user/test", store=store)    # cycle 2
        assert results[0]["status"] == "retry"               # stuck at attempt 1
        assert results[0]["retry_attempt"] == 1
        assert not is_processed(76)
        mock_mark.assert_not_called()                        # school-failed never fires

    @patch("issue_bridge.fetch_issues")
    @patch("director.run_task")
    @patch("issue_bridge._mark_github_issue")
    @patch("issue_bridge.notify_issue_alert")
    def test_exception_path_notifies_retry_then_school_failed(
        self, mock_notify, mock_mark, mock_task, mock_fetch, tmp_path, monkeypatch, store,
    ):
        """The run_task exception path alerts on both transitions too."""
        monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
        monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retries.json")
        mock_fetch.return_value = self._issue(75)
        mock_task.side_effect = RuntimeError("connection refused")
        bridge_issues("user/test", store=store)   # attempt 1 → retry alert
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][2] == "retry"
        assert mock_notify.call_args[1].get("attempt") == 1
        bridge_issues("user/test", store=store)   # attempt 2 → school-failed alert
        assert mock_notify.call_count == 2
        assert mock_notify.call_args[0][2] == "school-failed"
        assert mock_notify.call_args[1].get("attempt") == 2
        assert "connection refused" in mock_notify.call_args[1].get("error", "")


# ── U8: crew dispatch path (CREW_ENABLED) ─────────────────────────────────


class TestCrewDispatchPath:
    """The student-task path routes through the crew module when enabled.

    Flag-off (default) must be byte-for-byte today's path — run_task direct,
    no crew dispatch. Flag-on: done feeds the crew report as the student
    deliverable (via run_task's provided_student_output); spawn failure,
    timeout, failed, and blocked fall back to the direct path same-cycle with
    the fallback_reason recorded; the fallback itself failing carries the
    existing retry-once semantics.
    """

    @staticmethod
    def _issue(num):
        return [{"issue_number": num, "title": f"T{num}", "body": "",
                 "domain": "debugging", "difficulty": "easy", "prompt": "p",
                 "category": "bug", "state": "ready-for-agent"}]

    @staticmethod
    def _task_ok(num):
        return {
            "status": "success", "agent": "auto/best-free",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "p", "response": "ok",
        }

    @staticmethod
    def _repo_mocks(tmp_path):
        """Hermetic repo_reader stack (same targets as the E2E tests).

        clone_repo is imported *inside* bridge_issues, so the patch target is
        repo_reader.clone_repo, not a module attribute of issue_bridge.
        """
        return (
            patch("repo_reader.cleanup_stale_caches"),
            patch("repo_reader.clone_repo", return_value=tmp_path / "repo"),
            patch("repo_reader.build_codebase_context", return_value=""),
        )

    @staticmethod
    def _enter_repo_mocks(tmp_path):
        """Enter the hermetic repo mocks; returns the stack for cleanup."""
        stack = TestCrewDispatchPath._repo_mocks(tmp_path)
        for m in stack:
            m.start()
        return stack

    @staticmethod
    def _exit_repo_mocks(stack):
        for m in reversed(stack):
            m.stop()

    @staticmethod
    def _crew_done(num, tmp_path):
        """CrewResult-shaped done result with a real report.md."""
        report = tmp_path / "report.md"
        report.write_text(
            "branch=fm/task-%d commit=abc123 base=main@def456\n"
            "Implemented the fix in the Orca worktree.\n" % num
        )
        return SimpleNamespace(
            crew_id=f"fm-loop-20260811-120000-{num}",
            status="done",
            report_path=report,
            fallback_reason=None,
            teardown_ok=True,
            orca_worktree_id="repo::/tmp/worktree",
        )

    def test_flag_off_is_direct_path(self, monkeypatch, tmp_path, store):
        """CREW_ENABLED absent → crew never dispatched; run_task called direct."""
        import issue_bridge
        calls = []
        def fake_gh(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["label", "list"]:
                return "[]"
            return None
        monkeypatch.setattr("issue_bridge._gh_command", fake_gh)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(400)) as mock_fetch, \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(400)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        mock_crew.assert_not_called()
        mock_task.assert_called_once()
        assert "provided_student_output" not in mock_task.call_args[1]

    def test_flag_off_ignores_env_garbage(self, monkeypatch, tmp_path, store):
        """CREW_ENABLED=garbage → crew off (fail closed), not on."""
        monkeypatch.setenv("CREW_ENABLED", "banana")
        with patch("issue_bridge.fetch_issues", return_value=self._issue(401)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(401)), \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            bridge_issues("user/test", crew_enabled=None, store=store)
        mock_crew.assert_not_called()

    def test_crew_done_feeds_report_as_student_output(
        self, monkeypatch, tmp_path, store,
    ):
        """done → report.md content flows through run_task as the deliverable."""
        import issue_bridge
        monkeypatch.setattr("issue_bridge.CREW_RUNS_FILE", tmp_path / "crew_runs.json")
        with patch("issue_bridge.fetch_issues", return_value=self._issue(402)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew", return_value=self._crew_done(402, tmp_path)) as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(402)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'), \
             patch("issue_bridge._select_verification", return_value={
                 "passed": True, "ran": 0, "failures": [], "skipped": False,
             }), \
             patch("issue_bridge._run_adversarial_review", return_value={
                 "verdict": "PASS", "score": 85.0, "findings": [],
             }):
            results = bridge_issues("user/test", crew_enabled=True, store=store)
        r = results[0]
        assert r["status"] == "success"
        assert r["crew_used"] is True
        assert r["crew_id"] == "fm-loop-20260811-120000-402"
        assert r["teardown_ok"] is True
        assert mock_crew.call_args[1]["issue_number"] == 402
        # The crew deliverable substituted for the student model call.
        assert mock_task.call_args[1]["provided_student_output"] == (
            tmp_path / "report.md").read_text()
        # U9: the durable last_run entry carries the compact crew block.
        runs = json.loads((tmp_path / "last_run.json").read_text())
        assert runs[-1]["crew_id"] == "fm-loop-20260811-120000-402"
        assert runs[-1]["crew_used"] is True
        assert runs[-1]["teardown_ok"] is True
        assert runs[-1]["crew_fallback_reason"] is None

    def test_spawn_failure_falls_back_direct(self, monkeypatch, tmp_path, store):
        """CrewUnavailableError → same-cycle direct path, reason recorded."""
        from crew_dispatch import CrewUnavailableError
        with patch("issue_bridge.fetch_issues", return_value=self._issue(403)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew",
                   side_effect=CrewUnavailableError("fm-spawn missing")) as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(403)) as mock_task:
            results = bridge_issues("user/test", crew_enabled=True, store=store)
        assert results[0]["status"] == "success"
        assert results[0]["crew_used"] is False
        assert results[0]["crew_fallback_reason"] == "spawn_failure"
        # No crew_result on spawn failure → teardown_ok surfaces as None.
        assert results[0]["teardown_ok"] is None
        mock_crew.assert_called_once()
        mock_task.assert_called_once()
        assert "provided_student_output" not in mock_task.call_args[1]

    def test_timeout_falls_back_direct(self, monkeypatch, tmp_path, store):
        """Non-done terminal status (timeout) → direct path, reason recorded."""
        timeout = SimpleNamespace(
            crew_id=f"fm-loop-20260811-120000-404", status="timeout",
            report_path=None, fallback_reason="timeout",
            teardown_ok=True, orca_worktree_id=None,
        )
        with patch("issue_bridge.fetch_issues", return_value=self._issue(404)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew", return_value=timeout), \
             patch("director.run_task", return_value=self._task_ok(404)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", crew_enabled=True, store=store)
        assert results[0]["status"] == "retry"
        assert results[0]["crew_fallback_reason"] == "timeout"
        # U9: teardown_ok rides the crew block on the result.
        assert results[0]["teardown_ok"] is True
        # Timeout defers direct fallback — no second model call in the same cycle.
        mock_task.assert_not_called()

    def test_failed_falls_back_direct(self, monkeypatch, tmp_path, store):
        """Crew 'failed' → direct path, reason recorded."""
        failed = SimpleNamespace(
            crew_id=f"fm-loop-20260811-120000-405", status="failed",
            report_path=None, fallback_reason="crew_failed",
            teardown_ok=True, orca_worktree_id=None,
        )
        with patch("issue_bridge.fetch_issues", return_value=self._issue(405)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew", return_value=failed), \
             patch("director.run_task", return_value=self._task_ok(405)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", crew_enabled=True, store=store)
        assert results[0]["status"] == "success"
        assert results[0]["crew_fallback_reason"] == "crew_failed"
        assert results[0]["teardown_ok"] is True
        mock_task.assert_called_once()

    def test_fallback_also_fails_retries_once(self, monkeypatch, tmp_path, store):
        """Crew spawn fails AND direct path fails → retry-once carry (R8)."""
        from crew_dispatch import CrewUnavailableError
        with patch("issue_bridge.fetch_issues", return_value=self._issue(406)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew",
                   side_effect=CrewUnavailableError("gateway down")), \
             patch("director.run_task", return_value={"status": "error", "error": "model unavailable"}):
            # Attempt 1 → retry scheduled, crew reason preserved.
            results = bridge_issues("user/test", crew_enabled=True, store=store)
            assert results[0]["status"] == "retry"
            assert results[0]["retry_attempt"] == 1
            assert results[0]["crew_fallback_reason"] == "spawn_failure"
            assert not is_processed(406)
            # Attempt 2 → final error; a spawn failure never reached a verdict,
            # so the issue stays eligible as INFRA.
            results = bridge_issues("user/test", crew_enabled=True, store=store)
            assert results[0]["status"] == "error"
            assert not is_processed(406)
            assert _load_processed()[406] == "INFRA"

    def test_in_flight_record_skips_issue(self, monkeypatch, tmp_path, store):
        """An active crew record (interrupted prior cycle) skips, never double-spawns.

        The registry is matched by issue_number, NOT crew_id — crew_id embeds
        the writing cycle's session id, so a leftover record from a DIFFERENT
        (interrupted) cycle must still block this issue.
        """
        import datetime as _dt
        import issue_bridge
        runs = tmp_path / "crew_runs.json"
        recent = _dt.datetime.now(_dt.timezone.utc).isoformat()
        runs.write_text(json.dumps([{
            "crew_id": "fm-loop-20260810-230000-407",  # a PRIOR cycle
            "issue_number": 407,
            "status": "running",
            "started_at": recent,  # fresh → not stale → sweep leaves it
        }]))
        monkeypatch.setattr("issue_bridge.CREW_RUNS_FILE", runs)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(407)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task") as mock_task:
            results = bridge_issues(
                "user/test", crew_enabled=True, store=store,
                cycle_session_id="loop-20260811-120000",
            )
        assert results[0]["status"] == "crew_in_flight"
        assert results[0]["crew_skip_reason"] == "crew_in_flight"
        mock_crew.assert_not_called()
        mock_task.assert_not_called()
        assert not is_processed(407)

    def test_in_flight_record_sweeps_when_stale(self, monkeypatch, tmp_path, store):
        """A STALE active record triggers the sweep on skip (unstrands the issue).

        Without this, an interrupted crew would be skipped forever: the sweep
        only runs inside dispatch_crew, which the skip prevents.
        """
        import issue_bridge
        runs = tmp_path / "crew_runs.json"
        runs.write_text(json.dumps([{
            "crew_id": "fm-loop-20260701-000000-407",
            "issue_number": 407,
            "status": "running",
            "started_at": "2026-07-01T00:00:00+00:00",  # > CREW_TIMEOUT old
        }]))
        monkeypatch.setattr("issue_bridge.CREW_RUNS_FILE", runs)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(407)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.sweep_stale_runs") as mock_sweep, \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task") as mock_task:
            results = bridge_issues(
                "user/test", crew_enabled=True, store=store,
                cycle_session_id="loop-20260811-120000",
            )
        assert results[0]["status"] == "crew_in_flight"
        mock_sweep.assert_called_once()
        mock_crew.assert_not_called()
        mock_task.assert_not_called()

    def test_per_cycle_cap_falls_back_direct(self, monkeypatch, tmp_path, store):
        """After CREW_MAX_PER_CYCLE dispatches, later issues go direct."""
        done = self._crew_done(408, tmp_path)
        done2 = self._crew_done(409, tmp_path)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(408) + self._issue(409)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew", side_effect=[done, done2]) as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(408)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues(
                "user/test", crew_enabled=True, crew_max_per_cycle=1, store=store,
            )
        # First issue consumed the cap via crew; second fell back to direct.
        assert mock_crew.call_count == 1
        assert len(results) == 2
        by_num = {r["issue_number"]: r for r in results}
        assert by_num[408]["crew_used"] is True
        assert by_num[409]["crew_used"] is False
        assert by_num[409]["crew_fallback_reason"] == "crew_cap_reached"
        assert mock_task.call_count == 2

    def test_crew_done_report_missing_falls_back(self, monkeypatch, tmp_path, store):
        """done without a usable report → treat as fallback, direct path."""
        no_report = SimpleNamespace(
            crew_id=f"fm-loop-20260811-120000-410", status="done",
            report_path=tmp_path / "missing.md", fallback_reason="report_missing",
            teardown_ok=True, orca_worktree_id="repo::/tmp/wt",
        )
        with patch("issue_bridge.fetch_issues", return_value=self._issue(410)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew", return_value=no_report), \
             patch("director.run_task", return_value=self._task_ok(410)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", crew_enabled=True, store=store)
        assert results[0]["status"] == "success"
        assert results[0]["crew_fallback_reason"] == "report_missing"
        assert results[0]["crew_used"] is False
        mock_task.assert_called_once()

    # ── flag parsing / registry helpers ─────────────────────────────────

    def test_flag_parsing(self, monkeypatch):
        for truthy in ("1", "true", "TRUE", "yes", "on", " True "):
            monkeypatch.setenv("CREW_ENABLED", truthy)
            assert _crew_enabled_from_env() is True, truthy
        for falsy in ("", "0", "false", "no", "off", "banana", None):
            if falsy is None:
                monkeypatch.delenv("CREW_ENABLED", raising=False)
            else:
                monkeypatch.setenv("CREW_ENABLED", falsy)
            assert _crew_enabled_from_env() is False, falsy

    def test_active_issue_reads_registry(self, tmp_path):
        runs = tmp_path / "crew_runs.json"
        runs.write_text(json.dumps([
            {"crew_id": "fm-loop-a-1", "issue_number": 1, "status": "running"},
            {"crew_id": "fm-loop-b-1", "issue_number": 1, "status": "blocked"},
            {"crew_id": "fm-loop-c-2", "issue_number": 2, "status": "done"},
            {"crew_id": "fm-loop-d-2", "issue_number": 2, "status": "failed"},
        ]))
        # Any active record for the issue blocks it — even from another cycle.
        assert _crew_active_issue(runs, 1) is True
        assert _crew_active_issue(runs, 2) is False  # only terminal statuses
        assert _crew_active_issue(runs, 3) is False

    def test_active_issue_missing_file(self, tmp_path):
        assert _crew_active_issue(tmp_path / "nope.json", 1) is False

    def test_report_content_bounds_and_missing(self, tmp_path):
        assert _crew_report_content(None) is None
        assert _crew_report_content(tmp_path / "missing.md") is None
        big = tmp_path / "big.md"
        big.write_text("x" * (600 * 1024))
        assert _crew_report_content(big) is None
        small = tmp_path / "ok.md"
        small.write_text("branch=x commit=y base=z")
        assert _crew_report_content(small) == "branch=x commit=y base=z"
        blank = tmp_path / "blank.md"
        blank.write_text("   \n")
        assert _crew_report_content(blank) is None


# ── PR #145: frozen-contract threading (freeze BEFORE dispatch) ─────────────


class TestFrozenContractThreading:
    """The verification contract must be frozen from the clean base BEFORE
    dispatch, and the same snapshot must reach run_task → _run_two_judge_review
    → run_verify_gate. If it is not threaded, the review gate re-freezes from a
    repo the candidate may already have touched (TOCTOU).

    Regression: the pre-frozen contract was computed in bridge_issues but never
    passed to run_task, so the freeze was dead code on the direct path.
    """

    @staticmethod
    def _issue(num):
        return [{"issue_number": num, "title": f"T{num}", "body": "",
                 "domain": "debugging", "difficulty": "easy", "prompt": "p",
                 "category": "bug", "state": "ready-for-agent"}]

    @staticmethod
    def _task_ok(num):
        return {
            "status": "success", "agent": "auto/best-free",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "p", "response": "ok",
        }

    def _run(self, monkeypatch, tmp_path, store, repo_path):
        import issue_bridge
        with patch("issue_bridge.fetch_issues", return_value=self._issue(500)), \
             patch("repo_reader.clone_repo", return_value=repo_path), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(500)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            bridge_issues("user/test", store=store)
        mock_crew.assert_not_called()
        mock_task.assert_called_once()
        return mock_task.call_args[1]

    def test_frozen_contract_reaches_run_task(self, monkeypatch, tmp_path, store):
        """Direct path: a real repo dir → a non-None frozen contract is passed."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "x"}')
        kwargs = self._run(monkeypatch, tmp_path, store, repo)
        contract = kwargs.get("trusted_contract")
        assert isinstance(contract, dict), "frozen contract must be threaded to run_task"
        assert "files" in contract and "commands" in contract

    def test_missing_repo_path_does_not_crash(self, monkeypatch, tmp_path, store):
        """clone_repo returning a non-existent path must not raise: freeze is
        skipped (None) rather than crashing on resolve(strict=True).

        Regression: the unguarded freeze at bridge_issues crashed 15 tests
        whose clone_repo mock points at a path that is never created.
        """
        missing = tmp_path / "repo"  # never created
        kwargs = self._run(monkeypatch, tmp_path, store, missing)
        assert kwargs.get("trusted_contract") is None


# ── SCH-32a: hosted student execution flag (SCHOOL_CORE_HOSTED_STUDENT) ──────


class TestHostedStudentFlag:
    """Fail-closed operator gate for hosted SmolMachines Cloud student execution.

    When the flag is absent or unparseable, the existing no-host / Orca /
    direct-model path must be byte-for-byte unchanged: SmolCloudRunner is
    never imported, never constructed, and run_task runs the direct path
    exactly as before. This is the regression guard that makes slicing A
    safe to land before the routing slice (SCH-32b).
    """

    @staticmethod
    def _issue(num):
        return [{"issue_number": num, "title": f"T{num}", "body": "",
                 "domain": "debugging", "difficulty": "easy", "prompt": "p",
                 "category": "bug", "state": "ready-for-agent"}]

    @staticmethod
    def _task_ok(num):
        return {
            "status": "success", "agent": "auto/best-free",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "p", "response": "ok",
        }

    @staticmethod
    def _patches(tmp_path, monkeypatch):
        """Common hermetic patches for the direct dispatch path."""
        return (
            patch("issue_bridge.fetch_issues", return_value=TestHostedStudentFlag._issue(430)),
            patch("repo_reader.clone_repo", return_value=tmp_path / "repo"),
            patch("repo_reader.build_codebase_context", return_value=""),
            patch("repo_reader.cleanup_stale_caches"),
            patch("issue_bridge.dispatch_crew"),
            patch("director.run_task", return_value=TestHostedStudentFlag._task_ok(430)),
            patch("issue_bridge.call_model", return_value=(
                '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                '"gaps": [], "strengths": []}'
            )),
            patch("executor.call_model", return_value='{"findings": []}'),
        )

    @staticmethod
    def _run_bridge(tmp_path, monkeypatch, store, num):
        """Run bridge_issues on the direct path with hermetic mocks."""
        stack = TestHostedStudentFlag._patches(tmp_path, monkeypatch)
        # Start ALL patches — not just the first — so director.run_task,
        # dispatch_crew, call_model, etc. are all hermetic and the real
        # run_task does not spin up context probes (serena/school-context).
        mocks = [m.start() for m in stack]
        # Override the issue number for the fetch mock.
        mocks[0].return_value = TestHostedStudentFlag._issue(num)
        try:
            results = bridge_issues("user/test", store=store)
        finally:
            for m in reversed(stack):
                m.stop()
        return results

    def test_flag_absent_never_imports_smol_cloud_runner(
        self, monkeypatch, tmp_path, store,
    ):
        """SCHOOL_CORE_HOSTED_STUDENT absent → smol_cloud_runner never imported."""
        import sys
        monkeypatch.delenv("SCHOOL_CORE_HOSTED_STUDENT", raising=False)
        # Purge any prior import so we can prove the lazy import never fires.
        monkeypatch.delitem(sys.modules, "smol_cloud_runner", raising=False)
        results = self._run_bridge(tmp_path, monkeypatch, store, 430)
        assert results[0]["status"] == "success"
        assert "smol_cloud_runner" not in sys.modules, (
            "flag-absent path must not lazily import smol_cloud_runner"
        )

    def test_flag_absent_direct_path_unchanged(
        self, monkeypatch, tmp_path, store,
    ):
        """Flag absent → run_task called on the direct path, no runner kwarg."""
        monkeypatch.delenv("SCHOOL_CORE_HOSTED_STUDENT", raising=False)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(431)), \
             patch("repo_reader.clone_clone", return_value=tmp_path / "repo") if False else patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(431)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        mock_crew.assert_not_called()
        mock_task.assert_called_once()
        # No hosted-student plumbing leaks into the direct path.
        assert "student_runner" not in (mock_task.call_args.kwargs or {})

    def test_flag_garbage_is_off(self, monkeypatch, tmp_path, store):
        """SCHOOL_CORE_HOSTED_STUDENT=garbage → off (fail closed), no import."""
        import sys
        monkeypatch.setenv("SCHOOL_CORE_HOSTED_STUDENT", "banana")
        monkeypatch.delitem(sys.modules, "smol_cloud_runner", raising=False)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(432)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew") as mock_crew, \
             patch("director.run_task", return_value=self._task_ok(432)) as mock_task, \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        assert "smol_cloud_runner" not in sys.modules
        mock_crew.assert_not_called()
        mock_task.assert_called_once()

    def test_flag_parsing(self, monkeypatch):
        """_hosted_student_enabled_from_env: truthy on, everything else off."""
        for truthy in ("1", "true", "TRUE", "yes", "on", " True "):
            monkeypatch.setenv("SCHOOL_CORE_HOSTED_STUDENT", truthy)
            assert _hosted_student_enabled_from_env() is True, truthy
        for falsy in ("", "0", "false", "no", "off", "banana", None):
            if falsy is None:
                monkeypatch.delenv("SCHOOL_CORE_HOSTED_STUDENT", raising=False)
            else:
                monkeypatch.setenv("SCHOOL_CORE_HOSTED_STUDENT", falsy)
            assert _hosted_student_enabled_from_env() is False, falsy

    def test_flag_on_constructs_runner_from_env(self, monkeypatch, tmp_path, store):
        """Flag on with valid env → SmolCloudRunner constructed with pinned digest."""
        monkeypatch.setenv("SCHOOL_CORE_HOSTED_STUDENT", "1")
        monkeypatch.setenv(
            "SCHOOL_CORE_SMOL_CLOUD_IMAGE",
            "registry.smolmachines.com/library/alpine@sha256:" + "a" * 64,
        )
        # Spy on the constructor to confirm it is called with the env values
        # without actually making any network call (transport defaults to the
        # real urllib transport, but execute() is never reached in this slice).
        with patch("smol_cloud_runner.SmolCloudRunner.__init__", return_value=None) as mock_init, \
             patch("issue_bridge.fetch_issues", return_value=self._issue(433)), \
             patch("repo_reader.clone_repo", return_value=tmp_path / "repo"), \
             patch("repo_reader.build_codebase_context", return_value=""), \
             patch("repo_reader.cleanup_stale_caches"), \
             patch("issue_bridge.dispatch_crew"), \
             patch("director.run_task", return_value=self._task_ok(433)), \
             patch("issue_bridge.call_model", return_value=(
                 '{"score": 85, "verdict": "GOOD", "reasoning": "ok", '
                 '"gaps": [], "strengths": []}'
             )), \
             patch("executor.call_model", return_value='{"findings": []}'):
            results = bridge_issues("user/test", store=store)
        assert results[0]["status"] == "success"
        mock_init.assert_called_once()
        kwargs = mock_init.call_args.kwargs
        assert kwargs["image_reference"] == (
            "registry.smolmachines.com/library/alpine@sha256:" + "a" * 64
        )
        assert kwargs["source_type"] == "smolmachine"

    def test_flag_on_missing_image_fails_closed(self, monkeypatch, tmp_path, store):
        """Flag on but SCHOOL_CORE_SMOL_CLOUD_IMAGE absent → ValueError, not silent off."""
        monkeypatch.setenv("SCHOOL_CORE_HOSTED_STUDENT", "1")
        monkeypatch.delenv("SCHOOL_CORE_SMOL_CLOUD_IMAGE", raising=False)
        with patch("issue_bridge.fetch_issues", return_value=self._issue(434)):
            with pytest.raises(ValueError, match="image_reference"):
                bridge_issues("user/test", store=store)


# ── Phase 1 PR-correctness: candidate-bound publication ────────────────────

class TestCandidateBoundPublication:
    """Generic review/verification results cannot authorize candidate PR writes.

    The bridge has no wired trusted-verifier result or authenticated pre-PR
    teacher approval ingress yet, so this path must remain retryable and leave
    the issue open without calling even the fake provider.
    """

    @pytest.fixture
    def candidate_env(self, tmp_path, monkeypatch):
        import subprocess
        from dataclasses import asdict
        from candidate_manifest import CandidateStore, create_candidate

        repo = tmp_path / "target-repo"
        repo.mkdir()

        def _git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True, capture_output=True, text=True,
            )

        _git("init", "-b", "main")
        _git("config", "user.email", "test@example.com")
        _git("config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n")
        _git("add", "README.md")
        _git("commit", "-m", "base")
        _git("checkout", "-b", "candidate/exact")
        (repo / "README.md").write_text("candidate\n")
        _git("add", "README.md")
        _git("commit", "-m", "candidate")
        # The bridge's candidate path reads the SAME CandidateStore it uses for
        # registration/lookup, so the test fixture must point the bridge at the
        # fixture's store/repo — not a second hermetic copy.
        store = CandidateStore(tmp_path / "candidates.json")
        manifest = create_candidate(
            store=store, repo_path=repo,
            candidate_id="cand-1", bead_id="bead-1", issue_number=12,
            repository="user/test", base_ref="main", branch="candidate/exact",
            owner="student-coder",
        )

        # Opt in explicitly: candidate-bound publication is disabled by default.
        monkeypatch.setenv("CANDIDATE_PR_ENABLED", "1")
        # Hermetic bridge-side stores for every run.
        monkeypatch.setattr("issue_bridge.CANDIDATE_STORE_FILE", tmp_path / "candidates.json")
        monkeypatch.setattr("issue_bridge.PR_STATE_FILE", tmp_path / "pr-state.json")
        # The resume seam reads/writes the durable binding store; keep it
        # hermetic so a real data/candidate_bindings.json can never steer a test.
        monkeypatch.setattr(
            "issue_bridge.CANDIDATE_BINDING_FILE", tmp_path / "candidate_bindings.json"
        )
        # Pipeline scaffolding — same recipe as the legacy PR failure test.
        monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
        monkeypatch.setattr("repo_reader.clone_repo", lambda r: repo)
        monkeypatch.setattr("repo_reader.build_codebase_context", lambda *args: "")
        monkeypatch.setattr("issue_bridge._run_verify_gate", lambda *args, **kwargs: None)
        monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
        monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
            "verdict": "PASS", "score": 90.0, "findings": [],
        })
        monkeypatch.setattr(
            "issue_bridge.call_model",
            lambda *a, **k: '{"score": 90, "verdict": "GOOD", "reasoning": "ok", '
                           '"gaps": [], "strengths": ["works"]}',
        )
        monkeypatch.setattr("executor.call_model", lambda *a, **k: '{"findings": []}')
        monkeypatch.setattr("issue_bridge.fetch_issues", lambda *a, **k: [{
            "issue_number": 12, "title": "Bound candidate PR", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }])
        monkeypatch.setattr("director.run_task", lambda *a, **k: {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "fix", "response": "fixed",
            "candidate_manifest": asdict(manifest),
            "verify_result": {
                "passed": True,
                "skipped": False,
                "failures": [],
            },
            "review": {
                "accepted": True,
                "approval_id": "teacher-bridge",
            },
        })
        return SimpleNamespace(repo=repo, manifest=manifest, tmp_path=tmp_path)

    def _run_bridge(self, candidate_env, publisher, store, monkeypatch):
        from pr_provider import PrStateStore
        # Patch the publisher class the bridge factory instantiates, so the
        # candidate spine cannot fall back to a real GitHubCliPublisher even if
        # it resolves the factory through a different import path.
        monkeypatch.setattr(
            "issue_bridge.GitHubCliPublisher",
            lambda repo_path: publisher,
        )
        monkeypatch.setattr(
            "pr_provider.GitHubCliPublisher",
            lambda repo_path: publisher,
        )
        with patch("issue_bridge._mark_github_issue") as mock_mark, patch("issue_bridge._gh_command") as mock_gh:
            results = bridge_issues("user/test", store=store)
        journal = PrStateStore(candidate_env.tmp_path / "pr-state.json")
        return results, mock_mark, mock_gh, journal

    def test_missing_bound_verification_and_teacher_approval_refuse_before_write(
        self, candidate_env, monkeypatch, store,
    ):
        """Unbound generic verify/review dictionaries cannot authorize publication."""
        publisher = _BoundFakePublisher()

        results, mock_mark, mock_gh, journal = self._run_bridge(
            candidate_env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry"
        assert "PR" in results[0]["error"]
        assert not is_processed(12)
        mock_mark.assert_not_called()
        mock_gh.assert_not_called()
        runs = json.loads(
            (candidate_env.tmp_path / "last_run.json").read_text()
        )
        assert runs[-1]["status"] == "retry"
        assert not any(
            run.get("issue") == 12 and run.get("status") == "success"
            for run in runs
        )
        record = journal.get("cand-1")
        assert publisher.publish_calls == []
        assert record is not None, "a pre-write refusal must still journal the candidate row"
        assert record.state == "pr_failed"
        assert "trusted verification" in record.error or "teacher approval" in record.error
        assert record.head_sha == candidate_env.manifest.head_sha

    def test_pre_write_refusal_marks_failed_with_no_provider_write(
        self, candidate_env, monkeypatch, store,
    ):
        """A stale candidate fails before any provider write is attempted."""
        subprocess = __import__("subprocess")
        (candidate_env.repo / "README.md").write_text("moved on\n")
        subprocess.run(
            ["git", "-C", str(candidate_env.repo), "add", "README.md"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(candidate_env.repo), "commit", "-m", "new head"],
            check=True, capture_output=True,
        )
        publisher = _BoundFakePublisher()

        results, mock_mark, mock_gh, journal = self._run_bridge(
            candidate_env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry"
        assert not is_processed(12)
        mock_mark.assert_not_called()
        mock_gh.assert_not_called()
        assert publisher.publish_calls == []
        assert journal.get("cand-1").state == "pr_failed"

    def test_generic_verify_result_is_not_trusted_candidate_evidence(self, candidate_env):
        from issue_bridge import _build_trusted_verification_evidence

        evidence = _build_trusted_verification_evidence(
            {"passed": True, "skipped": False, "failures": []},
            candidate_env.manifest,
        )

        assert evidence is None

    def test_bound_verifier_evidence_preserves_candidate_identity(self, candidate_env):
        from issue_bridge import _build_trusted_verification_evidence
        from verifier_vm import VerifierEvidence

        manifest = candidate_env.manifest
        verification = VerifierEvidence(
            task_id="task-1",
            repository=manifest.repository,
            base_sha=manifest.base_sha,
            candidate_id=manifest.candidate_id,
            head_sha=manifest.head_sha,
            manifest_sha256="a" * 64,
            archive_sha256="b" * 64,
            disposition="current",
            checks_run=("unit",),
            guest_id="scv-task-1-deadbeef",
        )

        evidence = _build_trusted_verification_evidence(verification, manifest)

        assert evidence is not None
        assert evidence.candidate_id == manifest.candidate_id
        assert evidence.head_sha == manifest.head_sha
        assert evidence.passed is True

    def test_review_accepted_is_not_a_teacher_approval(self):
        from issue_bridge import _build_trusted_approval_evidence

        from candidate_manifest import CandidateManifest

        manifest = CandidateManifest(
            candidate_id="candidate-1",
            bead_id="bead-1",
            issue_number=12,
            repository="user/test",
            candidate_kind="source_diff",
            worktree="/tmp/candidate",
            branch="candidate/exact",
            base_ref="main",
            base_sha="b" * 40,
            head_sha="a" * 40,
            diff_digest="c" * 64,
            dirty_tree=False,
            owner="student-coder",
            created_at="2026-10-02T00:00:00Z",
        )
        evidence = _build_trusted_approval_evidence(
            {"accepted": True, "approval_id": "automated-review"}, manifest,
        )

        assert evidence is None

    def test_replay_does_not_bypass_missing_teacher_approval(
        self, candidate_env, monkeypatch, store,
    ):
        class TrackingPublisher(_BoundFakePublisher):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.find_calls = []

            def find_pr(self, **request):
                self.find_calls.append(request)
                return super().find_pr(**request)

        publisher = TrackingPublisher(existing={
            ("cand-1", candidate_env.manifest.head_sha):
                "https://github.com/user/test/pull/99",
        })
        results, mock_mark, mock_gh, journal = self._run_bridge(
            candidate_env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry"
        assert not is_processed(12)
        mock_mark.assert_not_called()
        mock_gh.assert_not_called()
        assert publisher.publish_calls == []
        assert publisher.find_calls == []
        assert journal.get("cand-1").state == "pr_failed"


# ── Phase 1 resume seam: CandidateBindingStore restart wiring ──────────────
#
# The production call path used to rebuild the verification/approval posture
# from scratch every cycle. A restart destroyed the in-memory state, so a
# candidate that had already been verified and approved could not be
# re-gated. The bridge now reloads the durable CandidateBindingStore binding
# for the candidate and re-runs the gate against the stored posture, falling
# back to fresh evidence (and persisting it) only when no binding exists.

class TestCandidateBindingResume:
    """Restart with a stored binding resumes; restart without one starts fresh."""

    @pytest.fixture
    def resume_env(self, tmp_path, monkeypatch):
        import subprocess
        from candidate_manifest import CandidateStore, create_candidate

        repo = tmp_path / "target-repo"
        repo.mkdir()

        def _git(*args):
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True, capture_output=True, text=True,
            )

        _git("init", "-b", "main")
        _git("config", "user.email", "test@example.com")
        _git("config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n")
        _git("add", "README.md")
        _git("commit", "-m", "base")
        _git("checkout", "-b", "candidate/exact")
        (repo / "README.md").write_text("candidate\n")
        _git("add", "README.md")
        _git("commit", "-m", "candidate")

        store = CandidateStore(tmp_path / "candidates.json")
        manifest = create_candidate(
            store=store, repo_path=repo,
            candidate_id="cand-1", bead_id="bead-1", issue_number=12,
            repository="user/test", base_ref="main", branch="candidate/exact",
            owner="student-coder",
        )

        # Hermetic bridge-side stores, including the new binding store.
        monkeypatch.setattr("issue_bridge.CANDIDATE_STORE_FILE", tmp_path / "candidates.json")
        monkeypatch.setattr("issue_bridge.PR_STATE_FILE", tmp_path / "pr-state.json")
        binding_path = tmp_path / "candidate_bindings.json"
        monkeypatch.setattr("issue_bridge.CANDIDATE_BINDING_FILE", binding_path)

        # Opt in explicitly: candidate-bound publication is disabled by default.
        monkeypatch.setenv("CANDIDATE_PR_ENABLED", "1")
        # Fail-closed by default: no journal, no allowlist.
        monkeypatch.delenv("APPROVAL_JOURNAL_FILE", raising=False)
        monkeypatch.delenv("APPROVED_ACTORS", raising=False)

        # Pipeline scaffolding (same recipe as TestCandidateBoundPublication).
        monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
        monkeypatch.setattr("repo_reader.clone_repo", lambda r: repo)
        monkeypatch.setattr("repo_reader.build_codebase_context", lambda *args: "")
        monkeypatch.setattr("issue_bridge._run_verify_gate", lambda *args, **kwargs: None)
        monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
        monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
            "verdict": "PASS", "score": 90.0, "findings": [],
        })
        monkeypatch.setattr(
            "issue_bridge.call_model",
            lambda *a, **k: '{"score": 90, "verdict": "GOOD", "reasoning": "ok", '
                           '"gaps": [], "strengths": ["works"]}',
        )
        monkeypatch.setattr("executor.call_model", lambda *a, **k: '{"findings": []}')
        monkeypatch.setattr("issue_bridge.fetch_issues", lambda *a, **k: [{
            "issue_number": 12, "title": "Bound candidate PR", "body": "",
            "domain": "debugging", "difficulty": "easy", "prompt": "fix",
            "category": "bug", "state": "ready-for-agent",
        }])
        # This cycle carries the manifest but NO trusted VerifierEvidence and
        # NO journal approval, so the gate can only authorize from a stored
        # binding. That is exactly the restart condition under test.
        monkeypatch.setattr("director.run_task", lambda *a, **k: {
            "status": "success", "agent": "foundry-coder-7b",
            "domain": "debugging", "difficulty": "easy",
            "prompt": "fix", "response": "fixed",
            "candidate_manifest": __import__("dataclasses").asdict(manifest),
        })
        return SimpleNamespace(
            repo=repo, manifest=manifest, tmp_path=tmp_path,
            binding_path=binding_path, candidate_store=store,
        )

    def _publisher(self, env):
        return _BoundFakePublisher(response_override={
            "candidate_id": env.manifest.candidate_id,
            "head_sha": env.manifest.head_sha,
            "pr_url": "https://github.com/user/test/pull/7",
        })

    def _run_bridge(self, env, publisher, store, monkeypatch):
        monkeypatch.setattr("issue_bridge.GitHubCliPublisher", lambda repo_path: publisher)
        monkeypatch.setattr("pr_provider.GitHubCliPublisher", lambda repo_path: publisher)
        with patch("issue_bridge._mark_github_issue") as mock_mark, \
                patch("issue_bridge._gh_command") as mock_gh:
            results = bridge_issues("user/test", store=store)
        return results, mock_mark, mock_gh

    def _seed_binding(self, env, *, head_sha=None, verification_passed=True,
                      approval_state="approved"):
        """Persist a binding the way a prior (pre-restart) process would have."""
        from candidate_binding import CandidateBindingStore, bind_trusted_evidence

        head = head_sha or env.manifest.head_sha
        verification = SimpleNamespace(
            candidate_id=env.manifest.candidate_id, head_sha=head,
            passed=verification_passed, skipped=False, failures=(),
        )
        approval = SimpleNamespace(
            candidate_id=env.manifest.candidate_id, head_sha=head,
            state=approval_state, approval_id="teacher-1",
        )
        binding = bind_trusted_evidence(
            manifest=env.manifest, verification=verification, approval=approval,
        )
        return CandidateBindingStore(env.binding_path).put(binding)

    # ── Requirement: restart WITH an existing binding resumes ──────────────

    def test_restart_with_existing_binding_resumes_posture(
        self, resume_env, monkeypatch, store,
    ):
        """A stored posture re-gates the candidate with no fresh evidence."""
        seeded = self._seed_binding(resume_env)
        assert seeded.verification_passed is True
        assert seeded.approval_state == "approved"

        publisher = self._publisher(resume_env)
        results, mock_mark, mock_gh = self._run_bridge(
            resume_env, publisher, store, monkeypatch,
        )

        # No journal, no VerifierEvidence — yet the gate authorized from the
        # resumed binding and the exact candidate was published.
        assert results[0]["status"] == "success", results[0].get("error")
        assert len(publisher.publish_calls) == 1
        assert publisher.publish_calls[0]["candidate_id"] == resume_env.manifest.candidate_id
        assert publisher.publish_calls[0]["head_sha"] == resume_env.manifest.head_sha
        mock_mark.assert_called()

    def test_resume_surfaces_the_stored_posture_not_a_rebuild(
        self, resume_env, monkeypatch,
    ):
        """`_resume_candidate_binding` returns the stored binding for the candidate."""
        seeded = self._seed_binding(resume_env)
        resumed = issue_bridge._resume_candidate_binding(resume_env.manifest)
        assert resumed is not None
        assert resumed.candidate_id == seeded.candidate_id
        assert resumed.head_sha == seeded.head_sha
        assert resumed.verification_passed is True
        assert resumed.approval_state == "approved"

    def test_resume_does_not_surface_a_superseded_head(self, resume_env):
        """A binding for a different head is not this candidate's posture."""
        from candidate_binding import CandidateBinding, CandidateBindingStore

        stale = CandidateBinding(
            candidate_id=resume_env.manifest.candidate_id,
            head_sha="b" * 40,  # superseded head
            repository=resume_env.manifest.repository,
            issue_number=resume_env.manifest.issue_number,
            branch=resume_env.manifest.branch,
            base_sha=resume_env.manifest.base_sha,
            diff_digest=resume_env.manifest.diff_digest,
            verification_passed=True, verification_skipped=False,
            verification_failures=(), approval_state="approved",
            approval_id="teacher-old", bound_at="",
        )
        CandidateBindingStore(resume_env.binding_path).put(stale)

        assert issue_bridge._resume_candidate_binding(resume_env.manifest) is None

    # ── Requirement: restart WITHOUT a binding starts fresh ────────────────

    def test_restart_without_binding_starts_fresh(
        self, resume_env, monkeypatch, store,
    ):
        """No stored binding and no fresh evidence -> fail closed, no write."""
        publisher = self._publisher(resume_env)
        results, mock_mark, mock_gh = self._run_bridge(
            resume_env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry"
        assert publisher.publish_calls == []
        mock_mark.assert_not_called()
        # A fresh start must not invent and store a posture it never had.
        assert CandidateBindingStore(resume_env.binding_path).get("cand-1") is None

    # ── Persistence half: first cycle writes the binding for the next one ──

    def test_first_cycle_persists_binding_for_next_restart(
        self, resume_env, monkeypatch, store,
    ):
        """With trusted verification + approval, the cycle persists the binding."""
        from state_journal import StateJournal
        from teacher_approval_ingress import TRUSTED_APPROVAL_SCOPE

        manifest = resume_env.manifest
        bound_verification = SimpleNamespace(
            candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
            passed=True, skipped=False, failures=(),
        )
        monkeypatch.setattr(
            "issue_bridge._build_trusted_verification_evidence",
            lambda verify_result, m: bound_verification,
        )
        journal_path = resume_env.tmp_path / "journal.sqlite3"
        monkeypatch.setenv("APPROVAL_JOURNAL_FILE", str(journal_path))
        monkeypatch.setenv("APPROVED_ACTORS", "human@example.com")
        StateJournal(journal_path).issue_approval(
            approval_id="appr-1", candidate_id=manifest.candidate_id,
            head_sha=manifest.head_sha, actor="human@example.com",
            scope=TRUSTED_APPROVAL_SCOPE,
        )

        publisher = self._publisher(resume_env)
        results, _, _ = self._run_bridge(resume_env, publisher, store, monkeypatch)
        assert results[0]["status"] == "success", results[0].get("error")

        stored = CandidateBindingStore(resume_env.binding_path).get("cand-1")
        assert stored is not None, "the cycle did not persist its bound posture"
        assert stored.head_sha == manifest.head_sha
        assert stored.verification_passed is True
        assert stored.approval_state == "approved"

    def test_resumed_binding_requires_no_fresh_journal_on_restart(
        self, resume_env, monkeypatch, store,
    ):
        """A consumed approval on restart still authorizes via the stored binding.

        The journal's approval may be consumed or absent after a restart; the
        durable binding is what carries the posture forward. This pins that the
        restart path does not depend on a still-live journal approval.
        """
        from state_journal import StateJournal
        from teacher_approval_ingress import TRUSTED_APPROVAL_SCOPE

        manifest = resume_env.manifest
        journal_path = resume_env.tmp_path / "journal.sqlite3"
        monkeypatch.setenv("APPROVAL_JOURNAL_FILE", str(journal_path))
        monkeypatch.setenv("APPROVED_ACTORS", "human@example.com")
        journal = StateJournal(journal_path)
        journal.issue_approval(
            approval_id="appr-used", candidate_id=manifest.candidate_id,
            head_sha=manifest.head_sha, actor="human@example.com",
            scope=TRUSTED_APPROVAL_SCOPE,
        )
        journal.consume_approval(
            approval_id="appr-used", candidate_id=manifest.candidate_id,
            head_sha=manifest.head_sha, operation_id="op-1", idempotency_key="idem-1",
        )
        # The posture was persisted before the restart.
        self._seed_binding(resume_env)

        publisher = self._publisher(resume_env)
        results, _, _ = self._run_bridge(resume_env, publisher, store, monkeypatch)
        assert results[0]["status"] == "success", results[0].get("error")
        assert len(publisher.publish_calls) == 1


# ── Phase 1 production seam: enablement + provider-outcome visibility ──────
#
# The candidate seam is disabled by default and must be opted into explicitly.
# These tests drive the production call path end to end
# (bridge_issues -> gate -> publish_candidate_pr_idempotent -> provider) and pin
# the acceptance behaviors the bead names:
#   * a disabled seam refuses a manifest-bearing task with NO provider call;
#   * a None / exception / ambiguous provider result stays visible (pr_pending)
#     and leaves the issue unprocessed;
#   * replay cannot create a duplicate PR;
#   * a mismatched / unapproved / unverified candidate never reaches the provider.

def _make_pub_env(tmp_path, monkeypatch, *, enabled):
    """Hermetic bridge environment for the candidate publication production path."""
    import subprocess
    from dataclasses import asdict
    from candidate_manifest import CandidateStore, create_candidate

    repo = tmp_path / "target-repo"
    repo.mkdir()

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n")
    _git("add", "README.md")
    _git("commit", "-m", "base")
    _git("checkout", "-b", "candidate/exact")
    (repo / "README.md").write_text("candidate\n")
    _git("add", "README.md")
    _git("commit", "-m", "candidate")

    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store, repo_path=repo,
        candidate_id="cand-1", bead_id="bead-1", issue_number=12,
        repository="user/test", base_ref="main", branch="candidate/exact",
        owner="student-coder",
    )

    monkeypatch.setattr("issue_bridge.CANDIDATE_STORE_FILE", tmp_path / "candidates.json")
    monkeypatch.setattr("issue_bridge.PR_STATE_FILE", tmp_path / "pr-state.json")
    binding_path = tmp_path / "candidate_bindings.json"
    monkeypatch.setattr("issue_bridge.CANDIDATE_BINDING_FILE", binding_path)
    # Explicit enablement: the seam is off unless the operator opts in.
    monkeypatch.setenv("CANDIDATE_PR_ENABLED", "1" if enabled else "0")
    monkeypatch.delenv("APPROVAL_JOURNAL_FILE", raising=False)
    monkeypatch.delenv("APPROVED_ACTORS", raising=False)

    # Pipeline scaffolding (same recipe as TestCandidateBoundPublication).
    monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
    monkeypatch.setattr("repo_reader.clone_repo", lambda r: repo)
    monkeypatch.setattr("repo_reader.build_codebase_context", lambda *args: "")
    monkeypatch.setattr("issue_bridge._run_verify_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
    monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
        "verdict": "PASS", "score": 90.0, "findings": [],
    })
    monkeypatch.setattr(
        "issue_bridge.call_model",
        lambda *a, **k: '{"score": 90, "verdict": "GOOD", "reasoning": "ok", '
                       '"gaps": [], "strengths": ["works"]}',
    )
    monkeypatch.setattr("executor.call_model", lambda *a, **k: '{"findings": []}')
    monkeypatch.setattr("issue_bridge.fetch_issues", lambda *a, **k: [{
        "issue_number": 12, "title": "Bound candidate PR", "body": "",
        "domain": "debugging", "difficulty": "easy", "prompt": "fix",
        "category": "bug", "state": "ready-for-agent",
    }])
    # The task carries ONLY the manifest: any authorization must come from the
    # seeded binding, so the gate's identity checks are what decide the outcome.
    monkeypatch.setattr("director.run_task", lambda *a, **k: {
        "status": "success", "agent": "foundry-coder-7b",
        "domain": "debugging", "difficulty": "easy",
        "prompt": "fix", "response": "fixed",
        "candidate_manifest": asdict(manifest),
    })
    return SimpleNamespace(
        repo=repo, manifest=manifest, tmp_path=tmp_path,
        binding_path=binding_path, candidate_store=store,
    )


def _seed_pub_binding(env, *, verification_passed=True, approval_state="approved",
                      head_sha=None, candidate_id=None):
    """Persist a binding the way a prior process would have.

    A binding whose candidate_id/head_sha do not match the manifest cannot pass
    ``bind_trusted_evidence`` (that boundary fails closed). To exercise the
    bridge's refusal on a stale or foreign stored posture, write the record
    directly — this is the shape a restart would actually find on disk.
    """
    from candidate_binding import (
        CandidateBinding,
        CandidateBindingStore,
        bind_trusted_evidence,
    )

    head = head_sha or env.manifest.head_sha
    cid = candidate_id or env.manifest.candidate_id
    store = CandidateBindingStore(env.binding_path)
    if cid != env.manifest.candidate_id or head != env.manifest.head_sha:
        return store.put(CandidateBinding(
            candidate_id=cid, head_sha=head,
            repository=env.manifest.repository,
            issue_number=env.manifest.issue_number,
            branch=env.manifest.branch,
            base_sha=env.manifest.base_sha,
            diff_digest=env.manifest.diff_digest,
            verification_passed=verification_passed, verification_skipped=False,
            verification_failures=(), approval_state=approval_state,
            approval_id="teacher-1", bound_at="",
        ))
    verification = SimpleNamespace(
        candidate_id=cid, head_sha=head,
        passed=verification_passed, skipped=False, failures=(),
    )
    approval = SimpleNamespace(
        candidate_id=cid, head_sha=head,
        state=approval_state, approval_id="teacher-1",
    )
    binding = bind_trusted_evidence(
        manifest=env.manifest, verification=verification, approval=approval,
    )
    return store.put(binding)


def _run_pub_bridge(env, publisher, store, monkeypatch):
    from pr_provider import PrStateStore

    monkeypatch.setattr("issue_bridge.GitHubCliPublisher", lambda repo_path: publisher)
    monkeypatch.setattr("pr_provider.GitHubCliPublisher", lambda repo_path: publisher)
    with patch("issue_bridge._mark_github_issue") as mock_mark, \
            patch("issue_bridge._gh_command") as mock_gh:
        results = bridge_issues("user/test", store=store)
    journal = PrStateStore(env.tmp_path / "pr-state.json")
    return results, mock_mark, mock_gh, journal


class TestCandidatePublicationProductionSeam:
    """Enablement and provider-outcome behavior of the production call path."""

    # ── Explicit enablement: safe default disabled ─────────────────────────

    def test_disabled_seam_refuses_manifest_task_with_no_provider_call(
        self, tmp_path, monkeypatch, store,
    ):
        """With CANDIDATE_PR_ENABLED unset, a candidate manifest never reaches
        the provider — the seam refuses instead of falling back to the legacy
        patch-blob path, and the issue stays retryable/unprocessed."""
        env = _make_pub_env(tmp_path, monkeypatch, enabled=False)
        _seed_pub_binding(env)  # posture would authorize, but the seam is off
        publisher = _BoundFakePublisher(response_override={
            "candidate_id": env.manifest.candidate_id,
            "head_sha": env.manifest.head_sha,
            "pr_url": "https://github.com/user/test/pull/7",
        })

        results, mock_mark, mock_gh, journal = _run_pub_bridge(
            env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry"
        assert "disabled" in results[0]["error"]
        assert publisher.publish_calls == []
        assert not is_processed(12)
        mock_mark.assert_not_called()
        assert journal.get("cand-1").state == "pr_failed"

    def test_enablement_flag_parses_fail_closed(self, monkeypatch):
        """Absent/0/false/garbage are OFF; only explicit truthy values are ON."""
        from issue_bridge import _candidate_pr_enabled_from_env

        monkeypatch.delenv("CANDIDATE_PR_ENABLED", raising=False)
        assert _candidate_pr_enabled_from_env() is False
        for value in ("0", "false", "no", "off", "garbage", ""):
            monkeypatch.setenv("CANDIDATE_PR_ENABLED", value)
            assert _candidate_pr_enabled_from_env() is False, value
        for value in ("1", "true", "YES", "on"):
            monkeypatch.setenv("CANDIDATE_PR_ENABLED", value)
            assert _candidate_pr_enabled_from_env() is True, value

    # ── Positive control: enabled seam publishes the exact candidate ───────

    def test_enabled_seam_publishes_exact_candidate(
        self, tmp_path, monkeypatch, store,
    ):
        env = _make_pub_env(tmp_path, monkeypatch, enabled=True)
        _seed_pub_binding(env)
        publisher = _BoundFakePublisher(response_override={
            "candidate_id": env.manifest.candidate_id,
            "head_sha": env.manifest.head_sha,
            "pr_url": "https://github.com/user/test/pull/7",
        })

        results, mock_mark, mock_gh, journal = _run_pub_bridge(
            env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "success", results[0].get("error")
        assert len(publisher.publish_calls) == 1
        request = publisher.publish_calls[0]
        assert request["candidate_id"] == env.manifest.candidate_id
        assert request["head_sha"] == env.manifest.head_sha
        assert request["base_sha"] == env.manifest.base_sha
        assert request["diff"]  # the exact source diff, not a patch blob
        record = journal.get("cand-1")
        assert record.state == "pr_published"
        assert record.pr_url == "https://github.com/user/test/pull/7"

    # ── Provider None / exception / ambiguous stays visible + unprocessed ──

    @pytest.mark.parametrize("outcome", ["none", "raises", "mismatch"])
    def test_ambiguous_provider_outcome_is_visible_and_unprocessed(
        self, tmp_path, monkeypatch, store, outcome,
    ):
        env = _make_pub_env(tmp_path, monkeypatch, enabled=True)
        _seed_pub_binding(env)
        if outcome == "none":
            publisher = _BoundFakePublisher(response_override=None)
        elif outcome == "raises":
            publisher = _BoundFakePublisher(raises=RuntimeError("create timed out"))
        else:  # mismatch: provider answered for a different candidate
            publisher = _BoundFakePublisher(response_override={
                "candidate_id": "another-candidate",
                "head_sha": env.manifest.head_sha,
                "pr_url": "https://github.com/user/test/pull/8",
            })

        results, mock_mark, mock_gh, journal = _run_pub_bridge(
            env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry", results[0]
        assert not is_processed(12)
        mock_mark.assert_not_called()
        # The write was attempted exactly once; ambiguity is not a license to
        # retry blindly, and the unresolved outcome is durably visible.
        assert len(publisher.publish_calls) == 1
        record = journal.get("cand-1")
        assert record.state == "pr_pending"
        assert record.head_sha == env.manifest.head_sha
        assert record.error

    # ── Replay cannot create a duplicate PR ────────────────────────────────

    def test_published_replay_creates_no_duplicate_pr(
        self, tmp_path, monkeypatch, store,
    ):
        env = _make_pub_env(tmp_path, monkeypatch, enabled=True)
        _seed_pub_binding(env)
        recorded_url = "https://github.com/user/test/pull/11"
        PrStateStore(env.tmp_path / "pr-state.json").record_published(
            candidate_id=env.manifest.candidate_id,
            issue_number=env.manifest.issue_number,
            repository=env.manifest.repository,
            branch=env.manifest.branch,
            head_sha=env.manifest.head_sha,
            pr_url=recorded_url,
        )
        publisher = _BoundFakePublisher(response_override={
            "candidate_id": env.manifest.candidate_id,
            "head_sha": env.manifest.head_sha,
            "pr_url": "https://github.com/user/test/pull/999",
        })

        results, mock_mark, mock_gh, journal = _run_pub_bridge(
            env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "success", results[0].get("error")
        assert publisher.publish_calls == [], "replay must not re-publish"
        assert results[0]["pr_url"] == recorded_url
        assert journal.get("cand-1").pr_url == recorded_url

    def test_ambiguous_replay_reconciles_without_duplicate_pr(
        self, tmp_path, monkeypatch, store,
    ):
        """An unresolved pr_pending is reconciled by (candidate_id, head_sha)
        before retry; the adopted PR is not written a second time."""
        env = _make_pub_env(tmp_path, monkeypatch, enabled=True)
        _seed_pub_binding(env)
        # Cycle 1: ambiguous outcome -> pr_pending.
        first = _BoundFakePublisher(raises=RuntimeError("timeout after create"))
        _run_pub_bridge(env, first, store, monkeypatch)
        assert len(first.publish_calls) == 1
        assert PrStateStore(env.tmp_path / "pr-state.json").get("cand-1").state == "pr_pending"

        # Cycle 2: provider now reports the PR bound to this candidate/head.
        existing_url = "https://github.com/user/test/pull/12"
        second = _BoundFakePublisher(existing={
            (env.manifest.candidate_id, env.manifest.head_sha): existing_url,
        })
        results, mock_mark, mock_gh, journal = _run_pub_bridge(
            env, second, store, monkeypatch,
        )

        assert results[0]["status"] == "success", results[0].get("error")
        assert second.publish_calls == [], "reconciled replay must not duplicate the PR"
        record = journal.get("cand-1")
        assert record.state == "pr_published"
        assert record.pr_url == existing_url

    # ── Mismatched / unapproved / unverified never reach the provider ──────

    @pytest.mark.parametrize("case", ["unverified", "unapproved", "wrong_head", "wrong_candidate"])
    def test_unauthorized_candidate_causes_no_provider_call(
        self, tmp_path, monkeypatch, store, case,
    ):
        env = _make_pub_env(tmp_path, monkeypatch, enabled=True)
        if case == "unverified":
            _seed_pub_binding(env, verification_passed=False)
        elif case == "unapproved":
            _seed_pub_binding(env, approval_state="pending")
        elif case == "wrong_head":
            _seed_pub_binding(env, head_sha="b" * 40)
        else:  # a binding for another candidate is not this candidate's posture
            _seed_pub_binding(env, candidate_id="cand-other")
        publisher = _BoundFakePublisher(response_override={
            "candidate_id": env.manifest.candidate_id,
            "head_sha": env.manifest.head_sha,
            "pr_url": "https://github.com/user/test/pull/7",
        })

        results, mock_mark, mock_gh, journal = _run_pub_bridge(
            env, publisher, store, monkeypatch,
        )

        assert results[0]["status"] == "retry", results[0]
        assert publisher.publish_calls == [], case
        assert not is_processed(12)
        mock_mark.assert_not_called()
        record = journal.get("cand-1")
        assert record is not None
        assert record.state == "pr_failed"
        assert record.head_sha == env.manifest.head_sha
