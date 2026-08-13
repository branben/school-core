"""Behavioral tests for the shared AgentMail transport."""

from __future__ import annotations

from email.message import Message
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

import pytest

import agentmail_client


class _Response:
    def __init__(self, payload: bytes = b"{}") -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.payload


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        url="https://api.agentmail.to/v0/inboxes",
        code=code,
        msg=f"status {code}",
        hdrs=headers,
        fp=None,
    )


class TestReqRetryPolicy:
    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer secret"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_429_honors_retry_after_then_succeeds(
        self, mock_urlopen, mock_sleep, mock_headers
    ):
        mock_urlopen.side_effect = [_http_error(429, "3"), _Response(b'{"ok": true}')]

        result = agentmail_client.req("GET", "/inboxes", _tries=2)

        assert result == {"ok": True}
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(3)

    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer secret"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_large_retry_after_is_capped(
        self, mock_urlopen, mock_sleep, mock_headers
    ):
        mock_urlopen.side_effect = [_http_error(429, "9999"), _Response(b'{"ok": true}')]

        result = agentmail_client.req("GET", "/inboxes", _tries=2)

        assert result == {"ok": True}
        mock_sleep.assert_called_once_with(60)

    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer secret"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_5xx_uses_capped_exponential_backoff(
        self, mock_urlopen, mock_sleep, mock_headers
    ):
        mock_urlopen.side_effect = [
            _http_error(503),
            _http_error(502),
            _Response(b'{"ok": true}'),
        ]

        result = agentmail_client.req("GET", "/inboxes", _tries=3)

        assert result == {"ok": True}
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_args_list == [call(5), call(10)]

    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer secret"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_non_retryable_4xx_fails_without_retry(
        self, mock_urlopen, mock_sleep, mock_headers
    ):
        mock_urlopen.side_effect = _http_error(401)

        with pytest.raises(HTTPError) as exc_info:
            agentmail_client.req("GET", "/inboxes", _tries=4)

        assert exc_info.value.code == 401
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer secret"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_final_transient_failure_stops_at_attempt_budget(
        self, mock_urlopen, mock_sleep, mock_headers
    ):
        mock_urlopen.side_effect = _http_error(429)

        with pytest.raises(HTTPError) as exc_info:
            agentmail_client.req("GET", "/inboxes", _tries=3)

        assert exc_info.value.code == 429
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_args_list == [call(5), call(10)]

    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer secret"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_url_error_is_retried_then_raised(
        self, mock_urlopen, mock_sleep, mock_headers
    ):
        error = URLError("gateway unavailable")
        mock_urlopen.side_effect = error

        with pytest.raises(URLError):
            agentmail_client.req("GET", "/inboxes", _tries=2)

        assert mock_urlopen.call_count == 2
        assert mock_sleep.call_args_list == [call(5)]

    @patch("agentmail_client._headers", return_value={"Authorization": "Bearer very-secret-token"})
    @patch("agentmail_client.time.sleep")
    @patch("agentmail_client.urllib.request.urlopen")
    def test_retry_log_does_not_include_authorization_value(
        self, mock_urlopen, mock_sleep, mock_headers, caplog
    ):
        mock_urlopen.side_effect = [_http_error(503), _Response(b"{}")]

        with caplog.at_level("WARNING", logger="agentmail_client"):
            agentmail_client.req("GET", "/inboxes", _tries=2)

        assert "very-secret-token" not in caplog.text
        assert "Bearer" not in caplog.text
