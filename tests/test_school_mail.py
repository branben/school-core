"""
Tests for the AgentMail notify client (school_mail.py).

Run: python -m pytest tests/test_school_mail.py -v
"""

from unittest.mock import patch

from school_mail import notify_issue_alert, notify_build_failure


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

    @patch("school_mail._resolve_dest_inbox", side_effect=RuntimeError("no inbox"))
    def test_no_inbox_degrades(self, mock_inbox):
        assert notify_build_failure(workflow="CI", run_url="u", commit_sha="s", branch="main", failed_jobs=[]) is False

    @patch("school_mail._resolve_dest_inbox", return_value="inbox-1")
    @patch("school_mail._req", side_effect=RuntimeError("network down"))
    def test_send_failure_degrades(self, mock_req, mock_inbox):
        assert notify_build_failure(workflow="CI", run_url="u", commit_sha="s", branch="main", failed_jobs=[]) is False
