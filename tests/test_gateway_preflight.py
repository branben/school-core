"""Gateway preflight — fail fast instead of retry-looping against a dead gateway.

WHY THIS EXISTS
---------------
62 of the last 63 School Loop runs were cancelled at ~31 minutes by
``timeout-minutes: 30``. Inspecting a cancelled run (32290180356) showed the
execute job spent 30 minutes processing exactly ONE issue, which failed with::

    [director] A2A fallback failed: A2A connection failed: [Errno 61] Connection refused
    send failed: HTTP Error 403: Forbidden
    [issue_bridge] #340: crew admission denied (retry_pressure) — direct path

``executor.py`` points BOTH transports at the same process::

    OMNIROUTE_BASE = "http://localhost:20128/v1"
    A2A_BASE       = "http://localhost:20128/a2a"

So when that one gateway is down, every model call and every A2A escalation
fails, ``_a2a_poll`` burns its 120s deadline per attempt, retry pressure
accumulates, the crew denies admission, and the job grinds until GitHub kills
it. The ``gate`` job checks that the *runner* is online but never checks the
*gateway the runner depends on*.

Fail-fast is strictly better here: a dead gateway should end the cycle in
seconds as a loud, alerting failure rather than 30 minutes of silent retrying
that reports as the ambiguous ``cancelled``.
"""

import urllib.error

import pytest

from gateway_preflight import GatewayDown, check_gateway, require_gateway


class TestCheckGateway:
    def test_returns_true_when_gateway_answers(self, monkeypatch):
        monkeypatch.setattr(
            "gateway_preflight._probe", lambda url, timeout: (200, "ok")
        )
        ok, detail = check_gateway()
        assert ok is True
        assert "200" in detail

    def test_connection_refused_is_down(self, monkeypatch):
        def _refused(url, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

        monkeypatch.setattr("gateway_preflight._probe", _refused)
        ok, detail = check_gateway()
        assert ok is False
        assert "refused" in detail.lower()

    def test_auth_failure_is_down_not_up(self, monkeypatch):
        """A 401/403 means the gateway cannot serve us — treat as down.

        The cancelled run showed `send failed: HTTP Error 403: Forbidden`
        alongside the refused connections. A reachable-but-unauthorized
        gateway produces the same 30-minute grind as an absent one, so it must
        not be reported as healthy.
        """
        monkeypatch.setattr(
            "gateway_preflight._probe", lambda url, timeout: (403, "Forbidden")
        )
        ok, detail = check_gateway()
        assert ok is False
        assert "403" in detail

    def test_server_error_is_down(self, monkeypatch):
        monkeypatch.setattr(
            "gateway_preflight._probe", lambda url, timeout: (503, "unavailable")
        )
        ok, detail = check_gateway()
        assert ok is False

    def test_probe_timeout_is_bounded_and_down(self, monkeypatch):
        """The probe must not inherit the 120s call timeout.

        A preflight that itself hangs defeats the purpose.
        """
        seen = {}

        def _record(url, timeout):
            seen["timeout"] = timeout
            raise TimeoutError("timed out")

        monkeypatch.setattr("gateway_preflight._probe", _record)
        ok, _ = check_gateway(timeout=5)
        assert ok is False
        assert seen["timeout"] == 5, "probe must honour the short preflight timeout"


class TestRequireGateway:
    def test_raises_gateway_down_when_unreachable(self, monkeypatch):
        monkeypatch.setattr(
            "gateway_preflight.check_gateway",
            lambda **kw: (False, "Connection refused"),
        )
        with pytest.raises(GatewayDown) as exc:
            require_gateway()
        # The message must name the gateway and the reason so a CI log line is
        # actionable without opening the code.
        assert "20128" in str(exc.value)
        assert "refused" in str(exc.value).lower()

    def test_passes_through_when_healthy(self, monkeypatch):
        monkeypatch.setattr(
            "gateway_preflight.check_gateway", lambda **kw: (True, "HTTP 200")
        )
        require_gateway()  # must not raise
