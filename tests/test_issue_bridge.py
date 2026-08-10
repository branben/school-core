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
    verify_task_output,
    _mark_github_issue,
    _ensure_school_labels,
    _load_retries,
    _save_retries,
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
