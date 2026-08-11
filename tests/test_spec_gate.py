#!/usr/bin/env python3
"""Tests for spec_gate.py (Rank 6 — harness-ready DOD gate).

All offline.  `check_dod()` and `_check_criterion()` are pure
functions; the spec JSON loading is the only I/O and can be
patched or bypassed with spec_path_override and in-memory dicts.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import director
from _helpers import make_passing_review
from scripts.spec_gate import _check_criterion, _check_dod, check_dod, write_spec


# ── _check_criterion (pure function) ────────────────

def test_tests_pass_criterion_passes_when_success():
    passed, reason = _check_criterion({"id": "tests-pass", "required": True}, {"status": "success"})
    assert passed is True
    assert reason == ""


def test_tests_pass_criterion_fails_when_not_success():
    passed, reason = _check_criterion({"id": "tests-pass"}, {"status": "failed"})
    assert passed is False
    assert "status" in reason.lower()


def test_no_critical_findings_passes_when_none():
    passed, reason = _check_criterion(
        {"id": "no-critical-findings"},
        {"review": {"findings": []}},
    )
    assert passed is True


def test_no_critical_findings_fails_on_critical():
    passed, reason = _check_criterion(
        {"id": "no-critical-findings"},
        {"review": {"findings": [{"severity": "CRITICAL", "title": "broken"}]}},
    )
    assert passed is False
    assert "CRITICAL" in reason


def test_response_length_passes_when_long():
    passed, reason = _check_criterion(
        {"id": "response-length"},
        {"response": "x" * 20},
    )
    assert passed is True


def test_response_length_fails_when_short():
    passed, reason = _check_criterion(
        {"id": "response-length"},
        {"response": "short"},
    )
    assert passed is False
    assert "length" in reason.lower()


def test_unknown_criterion_passes_with_warning():
    passed, reason = _check_criterion({"id": "nonexistent-criterion"}, {})
    assert passed is True
    assert reason == ""


# ── _check_dod (all criteria) ──────────────────────

def test_all_pass_no_failures():
    spec = {
        "task_id": "t1",
        "criteria": [
            {"id": "tests-pass", "required": True},
            {"id": "response-length", "required": False},
        ],
    }
    result = {"status": "success", "response": "x" * 20}
    passed, failures = _check_dod(spec, result)
    assert passed is True
    assert failures == []


def test_required_failure_blocks_pass():
    spec = {
        "task_id": "t1",
        "criteria": [
            {"id": "tests-pass", "required": True},
        ],
    }
    result = {"status": "failed"}
    passed, failures = _check_dod(spec, result)
    assert passed is False
    assert len(failures) == 1
    assert failures[0]["criterion_id"] == "tests-pass"


def test_soft_failure_records_but_does_not_hard_block():
    spec = {
        "task_id": "t1",
        "criteria": [
            {"id": "response-length", "required": False},
        ],
    }
    result = {"response": "x"}  # 1 char < 10
    passed, failures = _check_dod(spec, result)
    # A soft criterion records the failure but does not hard-block.
    assert passed is True
    assert len(failures) == 1
    assert failures[0]["required"] is False

# ── check_dod end-to-end ──────────────────────────

def test_check_dod_with_no_spec_passes_by_default():
    result = check_dod("no-spec-bead", {"status": "success"})
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["spec_path"] is None


def test_check_dod_with_existing_spec(tmp_path: Path):
    spec_content = {
        "task_id": "bead-xyz",
        "criteria": [
            {"id": "tests-pass", "description": "All tests pass", "required": True},
        ],
    }
    spec_file = tmp_path / ".hermes" / "specs" / "bead-xyz.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec_content))

    result = check_dod(
        "bead-xyz",
        {"status": "success", "response": "all good"},
        spec_path_override=str(spec_file),
    )
    assert result["passed"] is True


def test_check_dod_spec_failure_sets_dod_gate_failed(tmp_path: Path):
    spec_content = {
        "task_id": "bead-fail",
        "criteria": [
            {"id": "tests-pass", "description": "All tests pass", "required": True},
        ],
    }
    spec_file = tmp_path / ".hermes" / "specs" / "bead-fail.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec_content))

    result = check_dod(
        "bead-fail",
        {"status": "failed", "response": ""},
        spec_path_override=str(spec_file),
    )
    assert result["passed"] is False
    assert len(result["failures"]) == 1


# ── write_spec helper ──────────────────────────────

def test_write_spec_creates_file(tmp_path: Path):
    with patch("scripts.spec_gate.SPEC_DIR", tmp_path / ".hermes" / "specs"):
        p = write_spec("my-bead", [{"id": "tests-pass", "required": True}])
    assert p.exists()
    loaded = json.loads(p.read_text())
    assert loaded["task_id"] == "my-bead"
    assert len(loaded["criteria"]) == 1


# ── director.run_task dod_gate=True integration ───

def test_director_dod_gate_passes_with_success(tmp_path: Path):
    spec_content = {
        "task_id": "dod-pass-bead",
        "criteria": [{"id": "tests-pass", "required": True}],
    }
    spec_file = tmp_path / ".hermes" / "specs" / "dod-pass-bead.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec_content))

    store = MagicMock()
    store.get_score.return_value = 100  # exceeds "hard" gate threshold

    with patch("director._load_spec") as mock_load, \
         patch("director.check_dod") as mock_check, \
         patch("director.call_model", return_value="Mocked response") as mock_model, \
         patch("director._run_two_judge_review", return_value=make_passing_review()):
        mock_load.return_value = spec_content
        mock_check.return_value = {"passed": True, "failures": [], "spec_path": str(spec_file)}
        out = director.run_task(
            prompt="A simple task that passes the DOD",
            domain="python-coding",
            difficulty="hard",
            force_agent="coder",
            store=store,
            dod_gate=True,
        )
    assert "dod_gate" in out
    assert out["dod_gate"]["passed"] is True


def test_director_dod_gate_fails_on_criterion_miss(tmp_path: Path):
    spec_content = {
        "task_id": "dod-fail-bead",
        "criteria": [{"id": "tests-pass", "required": True}],
    }
    spec_file = tmp_path / ".hermes" / "specs" / "dod-fail-bead.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec_content))

    store = MagicMock()
    store.get_score.return_value = 100  # exceeds "hard" gate threshold

    with patch("director._load_spec") as mock_load, \
         patch("director.check_dod") as mock_check, \
         patch("director.call_model", return_value="Mocked response") as mock_model, \
         patch("director._run_two_judge_review", return_value=make_passing_review()):
        mock_load.return_value = spec_content
        mock_check.return_value = {
            "passed": False,
            "failures": [{"criterion_id": "tests-pass", "required": True, "reason": "status is 'failed'"}],
            "spec_path": str(spec_file),
        }
        out = director.run_task(
            prompt="A task that fails the DOD",
            domain="python-coding",
            difficulty="hard",
            force_agent="coder",
            store=store,
            dod_gate=True,
        )
    assert out["accepted"] is False
    assert out["dod_gate"]["passed"] is False


def test_director_dod_gate_false_no_key_in_result():
    """dod_gate=False (default) -> no 'dod_gate' key."""
    store = MagicMock()
    store.get_score.return_value = 100  # exceeds "hard" gate threshold

    with patch("director.call_model", return_value="Mocked response"), \
         patch("director._run_two_judge_review", return_value=make_passing_review()):
        out = director.run_task(
            prompt="Simple task",
            domain="python-coding",
            force_agent="coder",
            store=store,
            dod_gate=False,
        )
    assert out["status"] == "success"
    assert "dod_gate" not in out


# ── U1: session_id forwarding to enrich_prompt ───────

def test_director_forwards_session_id_to_enrich_prompt():
    """run_task must forward session_id so Layer 3 archival fires (U1)."""
    store = MagicMock()
    store.get_score.return_value = 100

    with patch("director.call_model", return_value="Mocked response"), \
         patch("director._run_two_judge_review", return_value=make_passing_review()), \
         patch("director.enrich_prompt", return_value="") as mock_enrich:
        out = director.run_task(
            prompt="Simple task",
            domain="python-coding",
            force_agent="coder",
            store=store,
            session_id="loop-20260811-1230",
        )
    assert out["status"] == "success"
    mock_enrich.assert_called_once()
    assert mock_enrich.call_args.kwargs.get("session_id") == "loop-20260811-1230"


def test_director_session_id_none_by_default():
    """Without an explicit session_id, enrich_prompt gets session_id=None."""
    store = MagicMock()
    store.get_score.return_value = 100

    with patch("director.call_model", return_value="Mocked response"), \
         patch("director._run_two_judge_review", return_value=make_passing_review()), \
         patch("director.enrich_prompt", return_value="") as mock_enrich:
        out = director.run_task(
            prompt="Simple task",
            domain="python-coding",
            force_agent="coder",
            store=store,
        )
    assert out["status"] == "success"
    assert mock_enrich.call_args.kwargs.get("session_id") is None
