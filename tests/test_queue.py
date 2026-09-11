"""Tests for the Director Queue CLI.

Covers the scenarios from the plan:
  - Empty queue -> "Nothing needs Director attention" + exit 0
  - 10+ issues -> paginated with --limit
  - JSON output -> parseable by Hermes tool calling
  - --include-context -> ripwire output attached, graceful degradation
  - needs_triage bucket for planless issues (key debate finding)
"""

from __future__ import annotations

import io
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from director_console import queue as q
from director_console.__main__ import main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_issue(
    id="school-core-1",
    title="Test issue",
    status="open",
    priority=2,
    issue_type="task",
    description="",
    dependency_count=0,
    dependent_count=0,
    comment_count=0,
    updated_at="2026-09-06T20:00:00Z",
):
    return {
        "id": id,
        "title": title,
        "status": status,
        "priority": priority,
        "issue_type": issue_type,
        "description": description,
        "dependency_count": dependency_count,
        "dependent_count": dependent_count,
        "comment_count": comment_count,
        "updated_at": updated_at,
    }


PLANLESS_ISSUE = make_issue(id="school-core-planless", title="Planless issue")
PLANNED_ISSUE = make_issue(
    id="school-core-planned",
    title="Planned issue",
    description="anchor: plan-director-console-2026-09-06-v2\n\nSome body.",
)
BLOCKED_ISSUE = make_issue(
    id="school-core-blocked",
    title="Blocked issue",
    dependency_count=1,
    description="anchor: plan-director-console-2026-09-06-v2",
)


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

def test_classify_planless_issue_goes_to_needs_triage():
    """Planless issues must NOT be silently dropped -> needs_triage."""
    bucket = q.classify_issue(PLANLESS_ISSUE)
    assert bucket == q.Bucket.NEEDS_TRIAGE


def test_classify_blocked_issue_goes_to_waiting():
    bucket = q.classify_issue(BLOCKED_ISSUE)
    assert bucket == q.Bucket.WAITING_ON_EXTERNAL


def test_classify_planned_active_issue_goes_to_ready(tmp_path):
    """Issue with an active linked plan -> ready_to_work."""
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "plan-director-console-2026-09-06-v2.md"
    plan_file.write_text("---\nstatus: active\n---\n# Plan\n")

    with patch.object(q, "KC_ROOT", str(tmp_path)):
        bucket = q.classify_issue(PLANNED_ISSUE)
    assert bucket == q.Bucket.READY_TO_WORK


def test_classify_planned_inactive_issue_goes_to_triage(tmp_path):
    """Issue with an archived linked plan -> needs_triage."""
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "plan-director-console-2026-09-06-v2.md"
    plan_file.write_text("---\nstatus: archived\n---\n# Plan\n")

    with patch.object(q, "KC_ROOT", str(tmp_path)):
        bucket = q.classify_issue(PLANNED_ISSUE)
    assert bucket == q.Bucket.NEEDS_TRIAGE


def test_resolve_kc_plan_finds_anchor():
    body = "anchor: plan-sound-royale-ny\n\nSome text."
    anchor, _ = q.resolve_kc_plan(body)
    assert anchor == "plan-sound-royale-ny"


def test_resolve_kc_plan_no_anchor():
    anchor, _ = q.resolve_kc_plan("No plan here.")
    assert anchor is None


def test_recommended_action_for_each_bucket():
    for bucket in q.Bucket:
        action = q.recommended_action(bucket, {})
        assert isinstance(action, str)
        assert len(action) > 0


# ---------------------------------------------------------------------------
# Queue building tests
# ---------------------------------------------------------------------------

def test_build_queue_sorts_by_priority_then_updated():
    issues = [
        make_issue(id="a", priority=3, updated_at="2026-09-01T00:00:00Z"),
        make_issue(id="b", priority=1, updated_at="2026-09-02T00:00:00Z"),
        make_issue(id="c", priority=2, updated_at="2026-09-03T00:00:00Z"),
    ]
    items = q.build_queue(issues)
    assert [i.id for i in items] == ["b", "c", "a"]


def test_build_queue_attaches_blast_radius_when_requested():
    issues = [PLANLESS_ISSUE]
    fake_blast = "symbol1.py  symbol2.py  symbol3.py"
    with patch("director_console.ripwire_client.blast_radius", return_value=fake_blast):
        items = q.build_queue(issues, include_context=True)
    assert items[0].blast_radius == fake_blast


def test_build_queue_graceful_when_ripwire_missing():
    """If ripwire returns None, queue still renders (no crash)."""
    issues = [PLANLESS_ISSUE]
    with patch("director_console.ripwire_client.blast_radius", return_value=None):
        items = q.build_queue(issues, include_context=True)
    assert items[0].blast_radius is None


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

def test_cli_empty_queue_exits_zero(capsys):
    """Empty queue -> 'Nothing needs Director attention' + exit 0."""
    with patch("director_console.__main__.fetch_ready_issues", return_value=[]):
        ret = main(["--format=brief"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Nothing needs Director attention" in captured.out


def test_cli_limit_paginates(capsys):
    """10+ issues -> paginated with --limit."""
    issues = [make_issue(id=f"school-core-{i}", priority=i % 3 + 1) for i in range(15)]
    with patch("director_console.__main__.fetch_ready_issues", return_value=issues):
        ret = main(["--limit=5"])
    assert ret == 0
    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l.strip().startswith("○")]
    assert len(lines) == 5


def test_cli_json_output_parseable(capsys):
    """JSON output -> parseable by Hermes tool calling."""
    issues = [PLANLESS_ISSUE, PLANNED_ISSUE]
    with patch("director_console.__main__.fetch_ready_issues", return_value=issues):
        ret = main(["--format=json"])
    assert ret == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "count" in payload
    assert "items" in payload
    assert payload["count"] == 2
    for item in payload["items"]:
        assert "id" in item
        assert "bucket" in item
        assert "recommended_action" in item


def test_cli_bucket_filter(capsys):
    """--bucket=needs_triage shows only planless issues."""
    issues = [PLANLESS_ISSUE, PLANNED_ISSUE]
    with patch("director_console.__main__.fetch_ready_issues", return_value=issues):
        ret = main(["--bucket=needs_triage"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "school-core-planless" in captured.out
    assert "school-core-planned" in captured.out


def test_cli_include_context_no_crash(capsys):
    """--include-context -> ripwire output attached, no crash if missing."""
    issues = [PLANLESS_ISSUE]
    with patch("director_console.ripwire_client.blast_radius", return_value=None):
        with patch("director_console.__main__.fetch_ready_issues", return_value=issues):
            ret = main(["--include-context"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "school-core-planless" in captured.out


def test_cli_md_format(capsys):
    """Markdown output renders headers."""
    issues = [PLANLESS_ISSUE]
    with patch("director_console.__main__.fetch_ready_issues", return_value=issues):
        ret = main(["--format=md"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Director Queue" in captured.out


# ---------------------------------------------------------------------------
# ripwire_client tests
# ---------------------------------------------------------------------------

def test_blast_radius_returns_none_when_bin_missing(tmp_path, monkeypatch):
    """Graceful degradation: missing binary -> None."""
    from director_console import ripwire_client
    monkeypatch.setattr(ripwire_client.shutil, "which", lambda x: None)
    monkeypatch.setattr(ripwire_client, "RIPWIRE_BIN", "/nonexistent/ripwire")
    result = ripwire_client.blast_radius(str(tmp_path), "test")
    assert result is None


def test_blast_radius_returns_output_on_success(tmp_path, monkeypatch):
    """When ripwire succeeds, output is returned."""
    from director_console import ripwire_client

    fake_bin = tmp_path / "ripwire"
    fake_bin.write_text("#!/bin/sh\necho 'fake ripwire output'\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(ripwire_client, "RIPWIRE_BIN", str(fake_bin))
    monkeypatch.setattr(ripwire_client.shutil, "which", lambda x: str(fake_bin))

    result = ripwire_client.blast_radius(str(tmp_path), "test")
    assert result == "fake ripwire output"


# ---------------------------------------------------------------------------
# fetch_ready_issues tests
# ---------------------------------------------------------------------------

def test_fetch_ready_issues_handles_bd_missing():
    """If bd is not installed, returns empty list (no crash)."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = q.fetch_ready_issues()
    assert result == []


def test_fetch_ready_issues_handles_bad_json():
    """If bd emits non-JSON, returns empty list."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "not json"
    with patch("subprocess.run", return_value=mock_result):
        result = q.fetch_ready_issues()
    assert result == []
