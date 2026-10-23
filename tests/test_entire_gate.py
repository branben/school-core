"""Tests for the Entire gate: CRITICAL/HIGH entire findings veto acceptance."""
import os
import pytest
from conductor import _persist_acceptance


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("ENTIRE_HIGH_STRICT", raising=False)


@pytest.fixture
def _mock_bookbag(monkeypatch):
    """Mock read_bookbag + locked_update_bookbag so _persist_acceptance is testable."""
    def _setup(bag):
        monkeypatch.setattr("conductor.read_bookbag", lambda bead, repo: bag)
        monkeypatch.setattr("conductor.locked_update_bookbag",
                            lambda bead, repo, lock_timeout=10.0, **kw: {**bag, **kw})
    return _setup


def test_critical_entire_finding_veto(_clean_env, _mock_bookbag):
    """CRITICAL entire finding → accepted=False even if both judges PASS."""
    _mock_bookbag({
        "cto_verdict": "PASS", "coo_verdict": "PASS",
        "cto_score": 80, "coo_score": 80,
        "cto_findings": [], "coo_findings": [],
        "entire_findings": [{"severity": "CRITICAL", "msg": "x"}],
        "entire_status": "fail"})
    assert _persist_acceptance("bead-1", "PASS", "PASS") is False


def test_high_entire_finding_vetoes(_clean_env, _mock_bookbag):
    """HIGH entire finding → accepted=False.

    A finding the reviewer chose to emit is blocking (CRITICAL or HIGH);
    MEDIUM/LOW stay advisory. HIGH used to be a warning unless
    ENTIRE_HIGH_STRICT=1, which left the sensor inert on real output because
    the parser defaulted unlabelled findings to HIGH.
    """
    _mock_bookbag({
        "cto_verdict": "PASS", "coo_verdict": "PASS",
        "cto_score": 80, "coo_score": 80,
        "cto_findings": [], "coo_findings": [],
        "entire_findings": [{"severity": "HIGH", "msg": "x"}],
        "entire_status": "fail"})
    assert _persist_acceptance("bead-2", "PASS", "PASS") is False


def test_medium_entire_finding_stays_advisory(_clean_env, _mock_bookbag):
    """MEDIUM entire finding → accepted=True (advisory, not a veto)."""
    _mock_bookbag({
        "cto_verdict": "PASS", "coo_verdict": "PASS",
        "cto_score": 80, "coo_score": 80,
        "cto_findings": [], "coo_findings": [],
        "entire_findings": [{"severity": "MEDIUM", "msg": "x"}],
        "entire_status": "warn"})
    assert _persist_acceptance("bead-2b", "PASS", "PASS") is True


def test_skipped_entire_does_not_veto(_clean_env, _mock_bookbag):
    """Entire CLI missing (no entire_findings) → no veto."""
    _mock_bookbag({
        "cto_verdict": "PASS", "coo_verdict": "PASS",
        "cto_score": 80, "coo_score": 80,
        "cto_findings": [], "coo_findings": []})
    assert _persist_acceptance("bead-4", "PASS", "PASS") is True


def test_error_entire_does_not_veto(_clean_env, _mock_bookbag):
    """Entire crashed (empty findings) → no veto."""
    _mock_bookbag({
        "cto_verdict": "PASS", "coo_verdict": "PASS",
        "cto_score": 80, "coo_score": 80,
        "cto_findings": [], "coo_findings": [],
        "entire_findings": [], "entire_status": "error"})
    assert _persist_acceptance("bead-5", "PASS", "PASS") is True
