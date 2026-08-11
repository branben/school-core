"""
Tests for the AgentMail notify client (school_mail.py).

Run: python -m pytest tests/test_school_mail.py -v
"""

from unittest.mock import patch

from school_mail import (
    notify_issue_alert,
    notify_build_failure,
    notify_pipeline_alert,
    notify_verdict,
    RESPONSE_FOOTER,
)


class TestNotifyVerdict:
    """notify_verdict builds a readable card with an ELI5 line + footer."""

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_card_has_eli5_and_footer(self, mock_req, mock_inbox):
        ok = notify_verdict("bead-1", True, "PASS", "PASS", repo="acme/repo")
        assert ok is True
        text = mock_req.call_args[0][2]["text"]
        # ELI5 line present even with no summary passed
        assert "What happened:" in text
        assert "passed both teacher reviews" in text
        # Response footer with the reply commands on their own lines
        assert "/approve" in text and "/reject" in text and "/fix <note>" in text
        assert "Reply with one of:" in text

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_summary_wins_over_eli5_default(self, mock_req, mock_inbox):
        notify_verdict("bead-1", False, "PASS", "FAIL", summary="CTO wants a regression test")
        text = mock_req.call_args[0][2]["text"]
        assert "What happened: CTO wants a regression test" in text


class TestNotifyIssueAlert:
    """notify_issue_alert builds the right AgentMail payload and never raises."""

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_school_failed_message(self, mock_req, mock_inbox):
        ok = notify_issue_alert(
            45, "Broken thing", "school-failed",
            error="model unavailable", repo="acme/repo", attempt=2,
        )
        assert ok is True
        mock_req.assert_called_once()
        method, path, body = mock_req.call_args[0]
        assert method == "POST"
        assert "/inboxes/inbox-1/messages/send" in path
        assert body["to"] == ["inbox-1"]
        assert "SCHOOL-FAILED" in body["subject"]
        assert "model unavailable" in body["text"]
        assert "acme/repo/issues/45" in body["text"]
        assert "Needs human review" in body["text"] or "needs human review" in body["text"]

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_retry_message(self, mock_req, mock_inbox):
        ok = notify_issue_alert(
            45, "Flaky", "retry",
            error="A2A connection failed: Connection refused",
            repo="acme/repo", attempt=1,
        )
        assert ok is True
        method, path, body = mock_req.call_args[0]
        assert "RETRY" in body["subject"]
        assert "attempt 1/2" in body["text"]
        assert "Connection refused" in body["text"]
        assert "retried automatically" in body["text"]

    @patch("school_mail._resolve_dest_inbox", side_effect=RuntimeError("no inbox"))
    def test_no_inbox_degrades(self, mock_inbox):
        assert notify_issue_alert(1, "x", "retry") is False

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req", side_effect=RuntimeError("network down"))
    def test_send_failure_degrades(self, mock_req, mock_inbox):
        assert notify_issue_alert(1, "x", "school-failed") is False

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_error_is_truncated(self, mock_req, mock_inbox):
        notify_issue_alert(1, "x", "retry", error="e" * 2000)
        text = mock_req.call_args[0][2]["text"]
        err_line = next(l for l in text.splitlines() if l.startswith("Error:"))
        assert len(err_line) <= 510  # 500 cap + "Error: " prefix

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_retry_explains_no_action_needed(self, mock_req, mock_inbox):
        notify_issue_alert(45, "Flaky", "retry", error="boom", repo="acme/repo", attempt=1)
        text = mock_req.call_args[0][2]["text"]
        assert "What happened:" in text
        assert "No action needed" in text
        assert "retried automatically" in text

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_school_failed_explains_next_step(self, mock_req, mock_inbox):
        notify_issue_alert(45, "Broken", "school-failed", repo="acme/repo", attempt=2)
        text = mock_req.call_args[0][2]["text"]
        assert "What happened:" in text
        assert "Next step:" in text
        assert "Needs human review" in text


class TestNotifyPipelineAlert:
    """Pipeline blockers are distinct from issue failures."""

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_blocked_card_names_component_and_next_step(self, mock_req, mock_inbox):
        ok = notify_pipeline_alert(
            component="school-core-mac runner",
            reason="runner offline",
            repo="acme/repo",
            run_url="https://github.com/acme/repo/actions/runs/7",
        )
        assert ok is True
        _, path, body = mock_req.call_args[0]
        assert "/inboxes/inbox-1/messages/send" in path
        assert "PIPELINE BLOCKED" in body["subject"]
        assert "What happened:" in body["text"]
        assert "school-core-mac runner" in body["text"]
        assert "runner offline" in body["text"]
        assert "Next step:" in body["text"]
        assert "actions/runs/7" in body["text"]

    @patch("school_mail._resolve_dest_inbox", side_effect=RuntimeError("no inbox"))
    def test_blocked_card_degrades_without_inbox(self, mock_inbox):
        assert notify_pipeline_alert("runner", "offline") is False


class TestNotifyBuildFailure:
    """notify_build_failure builds the CI-failure alert and never raises."""

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_basic_message(self, mock_req, mock_inbox):
        ok = notify_build_failure(
            workflow="CI", run_url="https://github.com/acme/repo/actions/runs/1",
            commit_sha="abc123def456", branch="main",
            failed_jobs=["pytest (py3.9/3.11/3.12)"], repo="acme/repo",
        )
        assert ok is True
        method, path, body = mock_req.call_args[0]
        assert "/inboxes/inbox-1/messages/send" in path
        assert "CI FAILED" in body["subject"]
        assert "abc123def" in body["text"]
        assert "pytest (py3.9/3.11/3.12)" in body["text"]
        assert "actions/runs/1" in body["text"]
        # No integration job → no infra hint
        assert "integration job failed" not in body["text"]

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_integration_failure_adds_infra_hint(self, mock_req, mock_inbox):
        notify_build_failure(
            workflow="CI", run_url="u", commit_sha="s", branch="main",
            failed_jobs=["integration (live Orca + OmniRoute)"], repo="acme/repo",
        )
        text = mock_req.call_args[0][2]["text"]
        assert "integration job failed" in text
        assert "gateway" in text

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_empty_jobs_lists_unknown(self, mock_req, mock_inbox):
        notify_build_failure(workflow="CI", run_url="u", commit_sha="s", branch="main", failed_jobs=[])
        text = mock_req.call_args[0][2]["text"]
        assert "unknown" in text

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req")
    def test_card_has_eli5_and_next_step(self, mock_req, mock_inbox):
        notify_build_failure(
            workflow="CI", run_url="u", commit_sha="abc123", branch="main",
            failed_jobs=["pytest"], repo="acme/repo",
        )
        text = mock_req.call_args[0][2]["text"]
        assert "What happened:" in text
        assert "Next step:" in text
        assert "Run: u" in text

    @patch("school_mail._resolve_dest_inbox", side_effect=RuntimeError("no inbox"))
    def test_no_inbox_degrades(self, mock_inbox):
        assert notify_build_failure(workflow="CI", run_url="u", commit_sha="s", branch="main", failed_jobs=[]) is False

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req", side_effect=RuntimeError("network down"))
    def test_send_failure_degrades(self, mock_req, mock_inbox):
        assert notify_build_failure(workflow="CI", run_url="u", commit_sha="s", branch="main", failed_jobs=[]) is False
