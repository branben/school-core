"""Tests for run-log management and calibration queries."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from director_console.run_log import (
    append_record,
    log_path_for,
    read_records,
)
from director_console.calibration import (
    compute_calibration,
    format_report,
    main as calibration_main,
)
from director_console.ship_safe_adapter import (
    read_ship_safe_verdict,
    _normalize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_run_log_dir(tmp_path, monkeypatch):
    """Redirect RUN_LOG_DIR to a temp path for the test."""
    from director_console import orca_bridge

    monkeypatch.setattr(orca_bridge, "RUN_LOG_DIR", str(tmp_path))
    return tmp_path


def make_record(**overrides):
    """Create a valid run record dict with optional overrides."""
    defaults = {
        "run_id": "test-uuid-1",
        "issue_id": "school-core-123",
        "contract_path": "/tmp/packet.yaml",
        "issued_at": "2026-09-06T20:00:00+00:00",
        "dispatched_at": "2026-09-06T20:01:00+00:00",
        "completed_at": "2026-09-06T20:15:00+00:00",
        "verdict": "PASS",
        "ship_safe_verdict": "PASS",
        "false_refutation": False,
        "settled": True,
        "notes": "",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# log_path_for tests
# ---------------------------------------------------------------------------

def test_log_path_for_uses_year_month():
    """log_path_for returns a path ending in YYYY-MM.jsonl."""
    dt = datetime(2026, 9, 8, tzinfo=timezone.utc)
    path = log_path_for(dt)
    assert path.endswith("2026-09.jsonl")


def test_log_path_for_defaults_to_now():
    """log_path_for with no arg uses current datetime."""
    path = log_path_for()
    now = datetime.now(timezone.utc)
    assert path.endswith(f"{now.strftime('%Y-%m')}.jsonl")


# ---------------------------------------------------------------------------
# append_record tests
# ---------------------------------------------------------------------------

def test_append_record_creates_file(tmp_run_log_dir):
    """append_record creates the JSONL file and writes the record."""
    record = make_record()
    path = append_record(record)

    assert os.path.exists(path)
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["issue_id"] == "school-core-123"


def test_append_record_appends_multiple(tmp_run_log_dir):
    """Multiple append_record calls append to the same file."""
    append_record(make_record(run_id="r1"))
    append_record(make_record(run_id="r2"))

    path = log_path_for()
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run_id"] == "r1"
    assert json.loads(lines[1])["run_id"] == "r2"


# ---------------------------------------------------------------------------
# read_records tests
# ---------------------------------------------------------------------------

def test_read_records_empty_dir(tmp_run_log_dir):
    """read_records returns empty list when no files exist."""
    assert read_records() == []


def test_read_records_filters_by_since(tmp_run_log_dir):
    """read_records respects the since bound."""
    old = make_record(
        run_id="old",
        issued_at="2026-08-01T00:00:00+00:00",
    )
    new = make_record(
        run_id="new",
        issued_at="2026-09-06T00:00:00+00:00",
    )
    append_record(old)
    append_record(new)

    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    records = read_records(since=since)
    assert len(records) == 1
    assert records[0]["run_id"] == "new"


def test_read_records_filters_by_until(tmp_run_log_dir):
    """read_records respects the until bound."""
    old = make_record(
        run_id="old",
        issued_at="2026-08-01T00:00:00+00:00",
    )
    new = make_record(
        run_id="new",
        issued_at="2026-09-06T00:00:00+00:00",
    )
    append_record(old)
    append_record(new)

    until = datetime(2026, 9, 1, tzinfo=timezone.utc)
    records = read_records(until=until)
    assert len(records) == 1
    assert records[0]["run_id"] == "old"


def test_read_records_skips_malformed_lines(tmp_run_log_dir):
    """read_records skips lines that aren't valid JSON."""
    path = log_path_for()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(make_record(run_id="good")) + "\n")
        f.write("not json\n")
        f.write(json.dumps(make_record(run_id="also-good")) + "\n")

    records = read_records()
    assert len(records) == 2
    assert records[0]["run_id"] == "good"
    assert records[1]["run_id"] == "also-good"


# ---------------------------------------------------------------------------
# ship_safe_adapter tests
# ---------------------------------------------------------------------------

def test_normalize_pass_variants():
    """_normalize maps pass-like strings to PASS."""
    assert _normalize("pass") == "PASS"
    assert _normalize("PASSED") == "PASS"
    assert _normalize("ok") == "PASS"
    assert _normalize("clean") == "PASS"
    assert _normalize("safe") == "PASS"


def test_normalize_fail_variants():
    """_normalize maps fail-like strings to FAIL."""
    assert _normalize("fail") == "FAIL"
    assert _normalize("FAILED") == "FAIL"
    assert _normalize("reject") == "FAIL"
    assert _normalize("unsafe") == "FAIL"
    assert _normalize("block") == "FAIL"


def test_normalize_none_and_unknown():
    """_normalize returns None for empty input, preserves unknown."""
    assert _normalize(None) is None
    assert _normalize("") is None
    assert _normalize("INCONCLUSIVE") == "INCONCLUSIVE"


def test_read_ship_safe_verdict_missing_file(tmp_path):
    """read_ship_safe_verdict returns None when file doesn't exist."""
    result = read_ship_safe_verdict(str(tmp_path / "nope.json"))
    assert result is None


def test_read_ship_safe_verdict_matches_pr(tmp_path):
    """read_ship_safe_verdict matches by pr_id."""
    data = [
        {"pr_id": "school-core#100", "verdict": "PASS"},
        {"pr_id": "school-core#200", "verdict": "FAIL"},
    ]
    fpath = tmp_path / "verdicts.json"
    fpath.write_text(json.dumps(data))

    result = read_ship_safe_verdict(str(fpath), pr_id="school-core#100")
    assert result == "PASS"

    result = read_ship_safe_verdict(str(fpath), pr_id="school-core#200")
    assert result == "FAIL"


def test_read_ship_safe_verdict_matches_issue(tmp_path):
    """read_ship_safe_verdict matches by issue_id."""
    data = [
        {"issue_id": "school-core-42", "verdict": "FAIL"},
    ]
    fpath = tmp_path / "verdicts.json"
    fpath.write_text(json.dumps(data))

    result = read_ship_safe_verdict(str(fpath), issue_id="school-core-42")
    assert result == "FAIL"


def test_read_ship_safe_verdict_no_match(tmp_path):
    """read_ship_safe_verdict returns None when no verdict matches."""
    data = [{"pr_id": "school-core#100", "verdict": "PASS"}]
    fpath = tmp_path / "verdicts.json"
    fpath.write_text(json.dumps(data))

    result = read_ship_safe_verdict(str(fpath), pr_id="school-core#999")
    assert result is None


def test_read_ship_safe_verdict_dict_format(tmp_path):
    """read_ship_safe_verdict handles a single-dict format."""
    data = {"verdicts": [{"pr_id": "school-core#50", "verdict": "PASS"}]}
    fpath = tmp_path / "verdicts.json"
    fpath.write_text(json.dumps(data))

    result = read_ship_safe_verdict(str(fpath), pr_id="school-core#50")
    assert result == "PASS"


# ---------------------------------------------------------------------------
# calibration tests
# ---------------------------------------------------------------------------

def test_compute_calibration_empty(tmp_run_log_dir):
    """compute_calibration returns zeros when no records exist."""
    stats = compute_calibration()
    assert stats["total"] == 0
    assert stats["settled"] == 0
    assert stats["false_refutations"] == 0
    assert stats["false_refutation_rate"] == 0.0
    assert stats["ship_safe_agreement_rate"] is None
    assert stats["avg_run_time_min"] is None


def test_compute_calibration_basic_stats(tmp_run_log_dir):
    """compute_calibration aggregates settled, false refutations, agreement."""
    # 4 records: 3 settled, 1 false refutation (Sentinel PASS, ship-safe FAIL)
    append_record(make_record(run_id="r1", settled=True, verdict="PASS", ship_safe_verdict="PASS"))
    append_record(make_record(run_id="r2", settled=True, verdict="PASS", ship_safe_verdict="FAIL", false_refutation=True))
    append_record(make_record(run_id="r3", settled=True, verdict="FAIL", ship_safe_verdict="FAIL"))
    append_record(make_record(run_id="r4", settled=False, verdict="PASS", ship_safe_verdict=None))

    stats = compute_calibration()
    assert stats["total"] == 4
    assert stats["settled"] == 3
    assert stats["settled_rate"] == 75.0
    assert stats["false_refutations"] == 1
    assert stats["false_refutation_rate"] == 25.0
    # ship-safe agreement: 2 out of 3 match (r1 PASS/PASS, r2 PASS/FAIL no, r3 FAIL/FAIL yes)
    assert stats["ship_safe_total"] == 3
    assert stats["ship_safe_agreement"] == 2
    assert stats["ship_safe_agreement_rate"] == pytest.approx(66.67, rel=1e-2)


def test_compute_calibration_avg_run_time(tmp_run_log_dir):
    """compute_calibration computes average run time from dispatched→completed."""
    append_record(make_record(
        run_id="r1",
        dispatched_at="2026-09-06T20:00:00+00:00",
        completed_at="2026-09-06T20:10:00+00:00",  # 10 min
    ))
    append_record(make_record(
        run_id="r2",
        dispatched_at="2026-09-06T20:00:00+00:00",
        completed_at="2026-09-06T20:20:00+00:00",  # 20 min
    ))

    stats = compute_calibration()
    assert stats["avg_run_time_min"] == pytest.approx(15.0)


def test_format_report_contains_key_stats(tmp_run_log_dir):
    """format_report renders a string with total, settled, false refutations."""
    append_record(make_record(run_id="r1"))
    stats = compute_calibration()
    report = format_report(stats)

    assert "Total runs: 1" in report
    assert "Settled: 1/1" in report
    assert "False refutations: 0" in report


def test_calibration_cli_default_30d(tmp_run_log_dir, capsys):
    """calibration_main runs with default --since 30 and prints report."""
    append_record(make_record(run_id="r1"))
    exit_code = calibration_main([])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Director Calibration Report" in captured.out
    assert "Total runs: 1" in captured.out


def test_calibration_cli_json_output(tmp_run_log_dir, capsys):
    """calibration_main --json outputs valid JSON."""
    append_record(make_record(run_id="r1"))
    exit_code = calibration_main(["--json"])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["total"] == 1
    assert data["settled"] == 1


def test_calibration_cli_custom_since(tmp_run_log_dir, capsys):
    """calibration_main --since 365 includes older records."""
    old = make_record(
        run_id="old",
        issued_at=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
    )
    append_record(old)

    # Default 30d should exclude it
    exit_code = calibration_main([])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out) if "--json" in capsys.readouterr().out else None

    # With --since 365 it should be included
    exit_code = calibration_main(["--since", "365", "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["total"] == 1
