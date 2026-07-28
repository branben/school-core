#!/usr/bin/env python3
"""Tests for principal_doubt.py (Rank 3 — Doubt-Driven Development routing cycle).

All tests run OFFLINE: run_doubt_cycle defaults to a deterministic local
analyzer (no LLM / OmniRoute), and the conductor wiring test mocks run_leaf.
"""

from unittest.mock import patch, MagicMock

from principal_doubt import run_doubt_cycle
import conductor


# ── run_doubt_cycle unit tests ─────────────────────────────────────────────

def test_doubt_offline_no_findings():
    """Default offline analyzer returns no findings; not reconciled."""
    log = run_doubt_cycle(
        claim="Routing task to coder via gate hard",
        extract={"task": "x", "role": "coder", "gate": "hard"},
    )
    assert log["findings"] == []
    assert log["reconciled"] is False
    assert log["override_reason"] is None
    assert log["cycles"] == 1
    assert "claim" in log and "extract" in log


def test_doubt_with_findings_down_shifts_gate():
    """A doubt_fn returning a finding down-shifts the gate and reconciles."""

    def doubter(claim, extract):
        # Flag any gate harder than "easy" as over-aggressive.
        if extract.get("gate") in ("medium", "hard", "diploma"):
            return ["Gate too hard for this role's readiness"]
        return []

    log = run_doubt_cycle(
        claim="Routing task to coder via gate hard",
        extract={"task": "x", "role": "coder", "gate": "hard"},
        doubt_fn=doubter,
    )
    assert log["findings"] == ["Gate too hard for this role's readiness"]
    assert log["reconciled"] is True
    assert log["extract"]["gate"] == "medium"  # hard -> one tier down
    assert "gate medium" in log["claim"]


def test_doubt_override_reason_recorded():
    """A human override skips the cycle and records the reason."""
    log = run_doubt_cycle(
        claim="Routing as-is",
        extract={"gate": "hard"},
        override_reason="user explicitly wants this gate",
    )
    assert log["override_reason"] == "user explicitly wants this gate"
    assert log["findings"] == []
    assert log["cycles"] == 0
    assert log["reconciled"] is False


# ── conductor._principal_dispatch wiring tests ──────────────────────────────

def test_dispatch_without_doubt_no_log():
    """doubt_enabled=False → no doubt_log key, run_leaf called once."""
    fake_result = {"status": "success", "agent": "coder", "domain": "python-coding"}
    with patch.object(conductor, "run_leaf", return_value=fake_result) as m:
        out = conductor._principal_dispatch(
            task="t", role="coder", domain="python-coding",
            difficulty="easy", store=MagicMock(), repo="__global__",
            doubt_enabled=False,
        )
    assert "doubt_log" not in out
    m.assert_called_once()


def test_dispatch_with_doubt_attaches_log():
    """doubt_enabled=True → doubt_log present, run_leaf called once."""
    fake_result = {"status": "success", "agent": "coder", "domain": "python-coding"}
    with patch.object(conductor, "run_leaf", return_value=fake_result) as m:
        out = conductor._principal_dispatch(
            task="t", role="coder", domain="python-coding",
            difficulty="easy", store=MagicMock(), repo="__global__",
            doubt_enabled=True,
        )
    assert "doubt_log" in out
    assert out["doubt_log"]["cycles"] == 1
    m.assert_called_once()
    # doubt_log is attached to the dispatch result as required by the plan.
    assert set(out["doubt_log"].keys()) >= {
        "claim", "extract", "findings", "reconciled", "override_reason", "cycles"
    }


def test_dispatch_doubt_reconciles_gate_passed_to_leaf():
    """If doubt down-shifts the gate, the reconciled gate reaches run_leaf."""

    def doubter(claim, extract):
        if extract.get("gate") == "hard":
            return ["over-aggressive"]
        return []

    fake_result = {"status": "success", "agent": "coder"}
    with patch.object(conductor, "run_leaf", return_value=fake_result) as m:
        out = conductor._principal_dispatch(
            task="t", role="coder", domain="python-coding",
            difficulty="hard", store=MagicMock(), repo="__global__",
            doubt_enabled=True, doubt_fn=doubter,
        )
    assert out["doubt_log"]["reconciled"] is True
    # run_leaf must have been called with the down-shifted gate ("medium").
    _, kwargs = m.call_args
    assert kwargs["difficulty"] == "medium"
