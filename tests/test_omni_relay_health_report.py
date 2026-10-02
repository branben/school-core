"""The relay reports observed model retirements to the fleet probe ledger.

Why: `fleet_config_audit.py` reads configuration and cannot see a model that
died upstream. `fleet_model_probe.py` fills that gap on a 6h schedule -- but a
schedule is a lie about a fact nobody can schedule. A real relay call already
pays for the knowledge: if the upstream says the free period has ended, that
fact is free.

What this must NOT do:

  * Mark a model dead on a transient fault. A 429 or a timeout must never
    poison a healthy model's cached verdict for a full TTL.
  * Let a bad ledger write break a call that would otherwise succeed. This is
    observability bolted onto a production path; a crash here would convert a
    successful inference into a failed one.
  * Write a credential. The detail is already `_redact`d upstream, but the
    ledger is a long-lived file on disk, so it is bounded and sanitized again.
"""

import json
import os

import pytest

from omni_relay import TransportError, _report_model_failure
from omni_relay import _classify_upstream_failure


def _ledger(tmp_path, monkeypatch):
    path = tmp_path / "model-probe.json"
    monkeypatch.setattr(
        "omni_relay._FLEET_LEDGER_PATH", str(path), raising=True
    )
    return path


class TestClassification:
    def test_retirement_notice_is_retired(self):
        assert _classify_upstream_failure(
            "HTTP 404: This model's free period has ended."
        ) == "retired"

    def test_quota_is_transient_not_retired(self):
        # Tonight's live error. Marking this 'retired' would be a false
        # positive on a healthy model.
        assert _classify_upstream_failure(
            'Model openrouter/auto out of credits (per-model billing)'
        ) == "transient"

    def test_rate_limit_is_transient(self):
        assert _classify_upstream_failure(
            "HTTP 429: Rate limit exceeded: free-models-per-day"
        ) == "transient"

    def test_not_found_is_retired(self):
        assert _classify_upstream_failure(
            "Model qwen/qwen3-coder:free not_found"
        ) == "retired"

    def test_transiently_unavailable_is_transient(self):
        assert _classify_upstream_failure(
            "Nous Portal didn't answer after 3 attempts - temporarily unavailable"
        ) == "transient"

    def test_retirement_wins_over_rate_limit_in_same_body(self):
        # A body can carry both; only one of them means the model is gone.
        assert _classify_upstream_failure(
            "429 rate limit. This model's free period has ended."
        ) == "retired"

    def test_unavailable_for_free_is_retired(self):
        # VERBATIM from OmniRoute call_logs, 2026-10-01T17:20:34. Found by
        # querying the provider's own log, not by guessing a phrasing -- this
        # exact retirement shape was classified `unknown` until then.
        assert _classify_upstream_failure(
            "[404]: This model is unavailable for free. "
            "The paid version is available now - use this slug in your request"
        ) == "retired"

    def test_unrecognised_text_is_unknown_not_retired(self):
        assert _classify_upstream_failure("something odd happened") == "unknown"


class TestLedgerReporting:
    def test_retirement_writes_ledger_entry(self, tmp_path, monkeypatch):
        path = _ledger(tmp_path, monkeypatch)
        _report_model_failure(
            "meituan/longcat-2.5-preview:free",
            "HTTP 404: This model's free period has ended.",
        )
        data = json.loads(path.read_text())
        assert data["meituan/longcat-2.5-preview:free"]["verdict"] == "retired"
        assert data["meituan/longcat-2.5-preview:free"]["source"] == "observed"

    def test_transient_does_not_write(self, tmp_path, monkeypatch):
        # The critical guard: a 429 must never mark a healthy model dead.
        path = _ledger(tmp_path, monkeypatch)
        _report_model_failure("poolside/laguna-s-2.1:free", "HTTP 429: rate limit")
        assert not path.exists()

    def test_unknown_does_not_write(self, tmp_path, monkeypatch):
        path = _ledger(tmp_path, monkeypatch)
        _report_model_failure("some/model", "something odd happened")
        assert not path.exists()

    def test_other_entries_are_preserved(self, tmp_path, monkeypatch):
        path = _ledger(tmp_path, monkeypatch)
        path.write_text(json.dumps({
            "a/model": {"verdict": "ok", "checked_at": 1, "source": "probe"}
        }))
        _report_model_failure("b/model", "This model's free period has ended.")
        data = json.loads(path.read_text())
        assert data["a/model"]["verdict"] == "ok"
        assert data["b/model"]["verdict"] == "retired"

    def test_ledger_failure_never_raises(self, tmp_path, monkeypatch):
        # Observability must not convert a good inference into a failed one.
        monkeypatch.setattr(
            "omni_relay._FLEET_LEDGER_PATH",
            "/nonexistent-dir-xyz/cannot-write.json",
            raising=True,
        )
        _report_model_failure(
            "m/model", "This model's free period has ended."
        )  # must not raise

    def test_detail_is_bounded(self, tmp_path, monkeypatch):
        path = _ledger(tmp_path, monkeypatch)
        _report_model_failure("m/model", "free period has ended " + "x" * 5000)
        data = json.loads(path.read_text())
        assert len(data["m/model"]["detail"]) <= 400

    def test_credential_shape_is_scrubbed_before_disk(self, tmp_path, monkeypatch):
        # The ledger is long-lived on disk, so sanitize again even though the
        # upstream detail was already redacted.
        path = _ledger(tmp_path, monkeypatch)
        _report_model_failure(
            "m/model",
            "This model's free period has ended. key=sk-live-ABCDEFGHIJKLMNOP1234",
        )
        raw = path.read_text()
        assert "sk-live-ABCDEFGHIJKLMNOP1234" not in raw


class TestTransportIntegration:
    def test_http_error_reports_retirement(self, tmp_path, monkeypatch):
        """An HTTP 404 carrying a retirement notice reaches the ledger."""
        import io
        import urllib.error

        path = _ledger(tmp_path, monkeypatch)
        monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-key-for-tests")

        body = json.dumps({
            "error": {"message": "This model's free period has ended."}
        }).encode()
        resp = urllib.error.HTTPError(
            "http://x/v1/chat/completions", 404, "Not Found", {},
            io.BytesIO(body),
        )

        from omni_relay import OmniRouteTransport
        t = OmniRouteTransport(
            model="openrouter/ling:free", api_key="sk-test-key-for-tests")

        def raise_http(*a, **k):
            raise resp
        t._open = raise_http

        with pytest.raises(TransportError):
            t("complete", b"hello")

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["openrouter/ling:free"]["verdict"] == "retired"

    def test_quota_error_does_not_report_retirement(self, tmp_path, monkeypatch):
        import io
        import urllib.error

        path = _ledger(tmp_path, monkeypatch)
        monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-key-for-tests")

        body = json.dumps({
            "error": {"message": "out of credits (per-model billing)"}
        }).encode()
        resp = urllib.error.HTTPError(
            "http://x/v1/chat/completions", 402, "Payment Required", {},
            io.BytesIO(body),
        )

        from omni_relay import OmniRouteTransport
        t = OmniRouteTransport(model="openrouter/ling:free")

        def _raise(*a, **k):
            raise resp
        monkeypatch.setattr(t, "_open", _raise)

        with pytest.raises(TransportError):
            t("complete", b"hello")

        # A quota wall is not a retirement. Must NOT be recorded as one.
        assert not path.exists()
