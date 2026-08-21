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

from gateway_preflight import (
    GatewayDown,
    check_gateway,
    check_gateway_with_warmup,
    require_gateway,
)


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


class TestWarmupRetry:
    """Cold-start tolerance without taxing the happy path.

    Measured on the live gateway (2026-08-20): probe1 12.31s, probe2 0.030s,
    probe3 0.033s. A cron firing during that first compile loses its whole
    preflight budget and kills a cycle that would have worked seconds later.
    The fix must distinguish "cold and compiling" from "dead" rather than
    widening the budget, because every widening slows the dead case — the one
    case this module exists to catch fast.
    """

    def test_cold_then_warm_is_healthy(self, monkeypatch):
        """First probe times out (compiling), second answers -> healthy."""
        calls = []

        def _cold_then_warm(url, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return 200, "ok"

        monkeypatch.setattr("gateway_preflight._probe", _cold_then_warm)
        slept = []
        ok, detail = check_gateway_with_warmup(sleep_fn=slept.append)

        assert ok is True, "a healthy-but-cold gateway must not be reported down"
        assert len(calls) == 2, "must retry exactly once, not loop"
        assert slept == [10], "must pause once between probes"
        # The detail has to preserve the first failure or the log hides the race.
        assert "warmup retry" in detail
        assert "timed out" in detail

    def test_refused_twice_attempts_only_one_probe(self, monkeypatch):
        """ECONNREFUSED is not a cold start: fail fast, never sleep.

        A compiling gateway has already accepted the socket; a refused
        connection means there is no listener. Retrying it only burns the
        warmup sleep for a guaranteed second failure.
        """
        calls = []

        def _refused(url, timeout):
            calls.append(timeout)
            raise urllib.error.URLError(
                ConnectionRefusedError(61, "Connection refused")
            )

        monkeypatch.setattr("gateway_preflight._probe", _refused)
        slept = []
        ok, detail = check_gateway_with_warmup(sleep_fn=slept.append)

        assert ok is False
        assert len(calls) == 1, "refused must NOT be retried"
        assert slept == [], "refused must not pay the warmup sleep"
        assert "refused" in detail.lower()

    def test_healthy_first_probe_never_sleeps(self, monkeypatch):
        """The green path must stay ~0.03s: one probe, no sleep."""
        calls = []

        def _healthy(url, timeout):
            calls.append(timeout)
            return 200, "ok"

        monkeypatch.setattr("gateway_preflight._probe", _healthy)
        slept = []
        ok, detail = check_gateway_with_warmup(sleep_fn=slept.append)

        assert ok is True
        assert len(calls) == 1, "a healthy gateway must not pay a retry probe"
        assert slept == [], "a healthy gateway must not pay the warmup sleep"
        assert detail == "HTTP 200", "green detail must stay clean"

    def test_sleep_is_injectable_and_timeout_per_probe(self, monkeypatch):
        """The suite must never really sleep, and each probe keeps the short budget.

        Guards the same invariant as test_probe_timeout_is_bounded_and_down: a
        retry must repeat the probe, NOT sum the budget into one long hang.
        """
        calls = []

        def _always_timeout(url, timeout):
            calls.append(timeout)
            raise TimeoutError("timed out")

        monkeypatch.setattr("gateway_preflight._probe", _always_timeout)

        def _boom(_seconds):  # real time.sleep would make the suite slow
            raise AssertionError("sleep_fn was not injected")

        monkeypatch.setattr("gateway_preflight.time.sleep", _boom)

        slept = []
        ok, detail = check_gateway_with_warmup(
            timeout=5, warmup_sleep=0.25, sleep_fn=slept.append
        )

        assert ok is False
        assert calls == [5, 5], "each probe gets the full short timeout, not the sum"
        assert slept == [0.25], "warmup_sleep must be honoured via the injected fn"
        assert "warmup retry also failed" in detail
