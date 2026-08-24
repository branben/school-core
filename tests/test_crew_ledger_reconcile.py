"""N10 guard tests — producer/consumer reconciliation (bead school-core-1u9 / B7).

Pins the boundary for the blindness that dominated the 2026-08-20 session: a
near-empty consumer ledger (6 records) was read as evidence that the producer had
never worked, while the producer's own directory held 108 status files, 54 of them
terminal, across 6 real issues.

The most important tests here are NOT the happy path. They are:
  * ``test_empty_producer_never_reconciles`` — two empty sets agreeing is the
    original blindness reimplemented as a guard;
  * ``test_reports_the_ids_not_just_a_count`` — a count is a number, the ids are
    actionable work;
  * ``test_in_flight_crew_is_not_a_violation`` — the invariant must not
    false-alarm on crews still running, or it gets switched off.
"""

from __future__ import annotations

import json

import pytest

from crew_ledger_reconcile import reconcile


def _state(tmp_path, **crews: str):
    """Write status files. Value is the file body."""
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    for crew_id, body in crews.items():
        (d / f"{crew_id}.status").write_text(body, encoding="utf-8")
    return d


def _ledger(tmp_path, *crew_ids: str):
    f = tmp_path / "crew_runs.json"
    f.write_text(json.dumps([{"crew_id": c} for c in crew_ids]), encoding="utf-8")
    return f


DONE = "working: started\ndone: branch=fm/x commit=abc1234 base=def5678\n"
FAILED = "working: started\nfailed: could not build\n"
WORKING = "working: still going\n"


class TestTheInvariant:
    def test_every_terminal_status_recorded_reconciles(self, tmp_path):
        state = _state(tmp_path, c1=DONE, c2=FAILED)
        ledger = _ledger(tmp_path, "c1", "c2")
        r = reconcile(state, ledger)
        assert r.ok is True, r.findings
        assert r.terminal_count == 2

    def test_unrecorded_terminal_crew_is_a_violation(self, tmp_path):
        """THE REGRESSION: 48 completed crews had no ledger record."""
        state = _state(tmp_path, c1=DONE, c2=DONE, c3=DONE)
        ledger = _ledger(tmp_path, "c1")
        r = reconcile(state, ledger)
        assert r.ok is False
        assert r.unreconciled == ["c2", "c3"]

    def test_reports_the_ids_not_just_a_count(self, tmp_path):
        """A count is a number; the ids are work."""
        state = _state(tmp_path, c1=DONE, c2=DONE)
        ledger = _ledger(tmp_path)
        r = reconcile(state, ledger)
        blob = "\n".join(r.findings)
        assert "c1" in blob and "c2" in blob, (
            "findings report only a count — nobody can act on that"
        )

    def test_in_flight_crew_is_not_a_violation(self, tmp_path):
        """A `working:` crew legitimately has no terminal record yet.

        This is why the invariant is terminal-verb-scoped rather than a
        producer/consumer ratio: a ratio would false-alarm on every live crew,
        and a guard that cries wolf gets switched off.
        """
        state = _state(tmp_path, c1=DONE, c2=WORKING)
        ledger = _ledger(tmp_path, "c1")
        r = reconcile(state, ledger)
        assert r.ok is True, (
            f"an in-flight crew was reported as unreconciled: {r.findings}"
        )
        assert r.terminal_count == 1

    def test_extra_ledger_records_are_not_a_violation(self, tmp_path):
        """The invariant is one-directional. A record without a surviving status
        file (rotated away, older run) is not evidence of loss."""
        state = _state(tmp_path, c1=DONE)
        ledger = _ledger(tmp_path, "c1", "c-ancient", "c-older")
        assert reconcile(state, ledger).ok is True


class TestNeverReportsCleanFromNothing:
    """The load-bearing guards. Getting these wrong rebuilds the original bug."""

    def test_empty_producer_never_reconciles(self, tmp_path):
        """0 vs 0 is not agreement — it is a failure to observe.

        This is the exact shape of the original blindness: a check that looks
        green precisely because it learned nothing.
        """
        state = _state(tmp_path)          # exists, no status files
        ledger = _ledger(tmp_path)        # exists, no records
        r = reconcile(state, ledger)
        assert r.ok is None, (
            "reported a verdict from two empty sets — this is the original "
            "blindness reimplemented as a guard"
        )
        assert r.ok is not True

    def test_missing_producer_dir_is_unknown_not_pass(self, tmp_path):
        r = reconcile(tmp_path / "no-such-dir", _ledger(tmp_path, "c1"))
        assert r.ok is None
        assert any("state dir" in f for f in r.findings)

    def test_missing_ledger_is_unknown_not_pass(self, tmp_path):
        """Terminal crews exist but there is nothing to compare against."""
        state = _state(tmp_path, c1=DONE)
        r = reconcile(state, tmp_path / "no-ledger.json")
        assert r.ok is None
        assert r.terminal_count == 1, (
            "the producer count must survive even when the ledger cannot be read"
        )

    def test_corrupt_ledger_is_unknown_not_pass(self, tmp_path):
        state = _state(tmp_path, c1=DONE)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert reconcile(state, bad).ok is None


class TestAuditNeverBreaksTheCaller:
    def test_summary_is_always_a_string(self, tmp_path):
        for report in (
            reconcile(_state(tmp_path, c1=DONE), _ledger(tmp_path, "c1")),
            reconcile(_state(tmp_path), _ledger(tmp_path)),
            reconcile(tmp_path / "nope", tmp_path / "nope.json"),
        ):
            assert isinstance(report.summary(), str) and report.summary()

    def test_unreadable_status_file_does_not_crash(self, tmp_path):
        state = _state(tmp_path, c1=DONE)
        (state / "c2.status").write_bytes(b"\xff\xfe binary garbage")
        r = reconcile(state, _ledger(tmp_path, "c1"))
        assert r.ok in (True, False)  # a verdict, not an exception


class TestCliIsAdvisory:
    def test_cli_always_exits_zero(self, monkeypatch, tmp_path, capsys):
        """A bookkeeping loss must not stop the cycle.

        Deliberately unlike gateway_preflight.py (exit 1): a dead gateway means
        no useful work is possible, an unreconciled ledger means the work
        happened and wasn't recorded.
        """
        import crew_ledger_reconcile as mod

        monkeypatch.setattr(mod, "STATE_DIR", _state(tmp_path, c1=DONE, c2=DONE))
        monkeypatch.setattr(mod, "CREW_RUNS_FILE", _ledger(tmp_path))
        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "MISSING" in out

    def test_cli_says_unknown_is_not_a_pass(self, monkeypatch, tmp_path, capsys):
        import crew_ledger_reconcile as mod

        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "gone")
        monkeypatch.setattr(mod, "CREW_RUNS_FILE", tmp_path / "gone.json")
        assert mod.main() == 0
        assert "not a pass" in capsys.readouterr().out.lower()
