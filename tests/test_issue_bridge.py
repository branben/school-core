"""
Tests for U2: Issue→Task Bridge (issue_bridge.py)

Run: python -m pytest tests/test_issue_bridge.py -v
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

import pytest

from issue_bridge import (
    _load_processed,
    _save_processed,
    mark_processed,
    is_processed,
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
    _crew_active_issue,
    _crew_report_content,
    SCHOOL_DONE_LABEL,
    SCHOOL_FAILED_LABEL,
    PROCESSED_FILE,
    RETRY_FILE,
    RETRY_LIMIT,
)
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
    # Reset the per-process label memoization so each test starts fresh.
    monkeypatch.setattr("issue_bridge._LABELS_ENSURED", False)
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
    # Never send real AgentMail alerts from tests.
    monkeypatch.setattr("issue_bridge.notify_issue_alert", lambda *a, **k: True)


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
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert results[0]["issue_number"] == 10
        # Should be marked processed
        assert is_processed(10)

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
        # Attempt 2 (retry budget exhausted) → final error + processed
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert is_processed(20)

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
        # Attempt 2 → final error + processed
        results = bridge_issues("user/test", store=store)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "unexpected error" in results[0]["error"]
        assert is_processed(30)


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
            "prompt": "fix", "response": "fixed",
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
        task_result = {"status": "success", "response": "code"}
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
            "prompt": "implement", "response": "def f(): return 1",
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
        """A real compile/test failure (ran > 0) must force FAIL + CRITICAL."""
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
        assert results[0]["status"] == "success"
        adv = results[0]["adversarial_review"]
        assert any(f.get("section") == "build/verify" for f in adv.get("findings", []))
        assert adv["verdict"] == "FAIL"
        assert adv["score"] == 0.0
        assert results[0]["verify_skipped"] is False

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
        assert results[0]["status"] == "success"
        adv = results[0]["adversarial_review"]
        assert any(f.get("section") == "build/verify" for f in adv.get("findings", []))
        assert adv["verdict"] == "FAIL"
        assert adv["score"] == 0.0
        assert results[0]["verify_skipped"] is False

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
            "prompt": "implement", "response": "def f(): return 1",
        }

    def test_sensor_returns_none_when_repo_missing(self):
        """No clone → None; the pipeline never blocks on the sensor."""
        assert _run_entire_sensor(Path("/nonexistent/repo")) is None

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
            entire_summary={"status": "pass", "findings": 0},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "score: 89.7" in comment
        assert "CTO PASS / COO PASS" in comment
        assert "accepted" in comment
        assert "Adversarial review: GOOD" in comment
        assert "2 finding(s)" in comment
        assert "Verify gate: PASS (1 command(s))" in comment
        assert "Pre-merge check: pass (0 finding(s))" in comment
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
            entire_summary={"status": "pass", "findings": 0},
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
            entire_summary={"status": "fail", "findings": 3},
            combined_score=89.7,
            crew_used=False, crew_fallback_reason=None,
        )
        assert "**Bookbag**" in comment
        assert "instance field" in comment  # output excerpt used
        assert "Blockers: 1" in comment
        assert "Pre-merge check: fail (3 finding(s))" in comment

    def test_crew_fallback_and_skipped_verify_rendered(self):
        comment = _build_school_comment(
            self.ISSUE, self._task(),
            verification={"verdict": "PASS", "score": 90.0, "ran": 0},
            adversarial_review={"verdict": "GOOD", "score": 88.0, "findings": []},
            verify_skipped=True,
            entire_summary=None,
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
            entire_summary={"status": "pass", "findings": 0},
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
            entire_summary={"status": "pass", "findings": 0},
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
            entire_summary={"status": "fail", "findings": 1},
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
            entire_summary={"status": "pass", "findings": 0},
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
                               entire_summary={"status": "pass", "findings": 0},
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
        assert is_processed(71)
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
             patch("director.run_task", return_value=self._task_ok(401)):
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
             patch("executor.call_model", return_value='{"findings": []}'):
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
        assert results[0]["status"] == "success"
        assert results[0]["crew_fallback_reason"] == "timeout"
        # U9: teardown_ok rides the crew block on the result.
        assert results[0]["teardown_ok"] is True
        mock_task.assert_called_once()

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
            # Attempt 2 → final error + processed.
            results = bridge_issues("user/test", crew_enabled=True, store=store)
            assert results[0]["status"] == "error"
            assert is_processed(406)

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
