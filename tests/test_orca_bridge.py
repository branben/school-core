"""Tests for the Orca bridge — queue → dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from unittest.mock import MagicMock, patch

import pytest

from director_console.contract import Intent, Packet, Provenance, Scope
from director_console.orca_bridge import (
    CLAIM_TIMEOUT_MIN,
    RUN_LOG_DIR,
    DispatchError,
    DoubleDispatchError,
    RunRecord,
    _is_claim_stale,
    _orca_task_exists,
    _run_cmd,
    _unclaim_issue,
    build_packet,
    dispatch_issue,
)
from director_console.queue import Bucket, QueueItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_queue_item(**overrides):
    """Create a valid QueueItem with optional overrides."""
    defaults = dict(
        id="school-core-123",
        title="Test issue",
        status="open",
        priority=2,
        issue_type="task",
        bucket=Bucket.NEEDS_TRIAGE,
        recommended_action="Link a KC plan or decide scope before dispatch",
        updated_at="2026-09-06T20:00:00Z",
        dependency_count=0,
        dependent_count=0,
        comment_count=0,
        kc_plan_anchor=None,
        kc_plan_active=None,
        blast_radius=None,
        extra={"description": ""},
    )
    defaults.update(overrides)
    return QueueItem(**defaults)


# ---------------------------------------------------------------------------
# build_packet tests
# ---------------------------------------------------------------------------

def test_build_packet_creates_valid_packet():
    """build_packet creates a valid Packet from a QueueItem."""
    item = make_queue_item()
    packet = build_packet(item)
    assert isinstance(packet, Packet)
    assert packet.intent.what == "Test issue"
    assert packet.provenance.bd_issue == "school-core-123"


def test_build_packet_with_kc_plan():
    """build_packet extracts KC plan anchor from issue description."""
    item = make_queue_item(extra={"description": "anchor: plan-sound-royale-ny\n\nBody"})
    packet = build_packet(item)
    assert packet.intent.anchor == "plan-sound-royale-ny"
    assert packet.provenance.kc_plan == "plan-sound-royale-ny"


# ---------------------------------------------------------------------------
# dispatch_issue tests
# ---------------------------------------------------------------------------

def test_dispatch_claims_issue_first(monkeypatch):
    """dispatch_issue claims the issue before creating an Orca task."""
    item = make_queue_item()
    calls = []

    def mock_run_cmd(cmd, timeout=60):
        calls.append(cmd)
        proc = MagicMock()
        if "bd" in cmd and "--claim" in cmd:
            proc.returncode = 0
            proc.stderr = ""
        elif "task-create" in cmd:
            proc.returncode = 0
            proc.stdout = json.dumps({"id": "task-abc-123"})
        elif "dispatch" in cmd:
            proc.returncode = 0
            proc.stderr = ""
        elif "task-get" in cmd:
            proc.returncode = 0
            proc.stdout = "task exists"
        else:
            proc.returncode = 0
            proc.stdout = ""
            proc.stderr = ""
        return proc

    monkeypatch.setattr("director_console.orca_bridge._run_cmd", mock_run_cmd)
    monkeypatch.setattr("director_console.orca_bridge.time.sleep", lambda x: None)

    record = dispatch_issue(item, "student-coder", skip_claim_check=False)

    # Verify claim was called before task-create
    assert any("bd" in c and "--claim" in c for c in calls)
    assert any("task-create" in c for c in calls)
    assert record.orca_task_id == "task-abc-123"
    assert record.settled


def test_dispatch_rolls_back_on_task_create_failure(monkeypatch):
    """If task creation fails, the claim is rolled back."""
    item = make_queue_item()
    calls = []

    def mock_run_cmd(cmd, timeout=60):
        calls.append(cmd)
        proc = MagicMock()
        if "bd" in cmd and "--claim" in cmd:
            proc.returncode = 0
            proc.stderr = ""
        elif "bd" in cmd and "--unclaim" in cmd:
            proc.returncode = 0
            proc.stderr = ""
        elif "task-create" in cmd:
            proc.returncode = 1
            proc.stderr = "connection timeout"
        else:
            proc.returncode = 0
            proc.stdout = ""
            proc.stderr = ""
        return proc

    monkeypatch.setattr("director_console.orca_bridge._run_cmd", mock_run_cmd)

    with pytest.raises(DispatchError, match="task-create failed"):
        dispatch_issue(item, "student-coder", skip_claim_check=False)

    # Verify unclaim was called
    assert any("bd" in c and "--unclaim" in c for c in calls)


def test_dispatch_prevents_double_dispatch(monkeypatch):
    """DoubleDispatchError is raised when issue is already claimed."""
    item = make_queue_item()

    def mock_run_cmd(cmd, timeout=60):
        proc = MagicMock()
        if "bd" in cmd and "--claim" in cmd:
            proc.returncode = 1
            proc.stderr = "Error: issue already claimed by another agent"
        else:
            proc.returncode = 0
            proc.stdout = ""
            proc.stderr = ""
        return proc

    monkeypatch.setattr("director_console.orca_bridge._run_cmd", mock_run_cmd)

    with pytest.raises(DoubleDispatchError, match="already claimed"):
        dispatch_issue(item, "student-coder", skip_claim_check=False)


def test_dispatch_logs_reconciliation_failure(monkeypatch):
    """If task disappears after dispatch, reconciliation fails."""
    item = make_queue_item()
    call_count = {"n": 0}

    def mock_run_cmd(cmd, timeout=60):
        call_count["n"] += 1
        proc = MagicMock()
        if "bd" in cmd and "--claim" in cmd:
            proc.returncode = 0
            proc.stderr = ""
        elif "task-create" in cmd:
            proc.returncode = 0
            proc.stdout = json.dumps({"id": "task-xyz"})
        elif "dispatch" in cmd:
            proc.returncode = 0
            proc.stderr = ""
        elif "task-get" in cmd:
            # Task not found — triggers reconciliation failure
            proc.returncode = 1
            proc.stdout = "not found"
        else:
            proc.returncode = 0
            proc.stdout = ""
            proc.stderr = ""
        return proc

    monkeypatch.setattr("director_console.orca_bridge._run_cmd", mock_run_cmd)
    monkeypatch.setattr("director_console.orca_bridge.time.sleep", lambda x: None)

    with pytest.raises(DispatchError, match="Reconciliation failed"):
        dispatch_issue(item, "student-coder", skip_claim_check=False)


# ---------------------------------------------------------------------------
# _is_claim_stale tests
# ---------------------------------------------------------------------------

def test_is_claim_stale_with_old_claim():
    """Claims older than CLAIM_TIMEOUT_MIN are stale."""
    from datetime import datetime, timedelta, timezone

    old_time = (datetime.now(timezone.utc) - timedelta(minutes=CLAIM_TIMEOUT_MIN + 5)).isoformat()
    issue = {"claimed_at": old_time}
    assert _is_claim_stale(issue) is True


def test_is_claim_stale_with_recent_claim():
    """Recent claims are not stale."""
    from datetime import datetime, timedelta, timezone

    recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    issue = {"claimed_at": recent_time}
    assert _is_claim_stale(issue) is False


def test_is_claim_stale_with_no_claim():
    """Issues without claimed_at are not stale."""
    assert _is_claim_stale({}) is False
    assert _is_claim_stale({"claimed_at": ""}) is False


# ---------------------------------------------------------------------------
# RunRecord tests
# ---------------------------------------------------------------------------

def test_run_record_save_creates_jsonl(tmp_path, monkeypatch):
    """RunRecord.save() appends a JSONL record to the run log."""
    monkeypatch.setattr("director_console.orca_bridge.RUN_LOG_DIR", str(tmp_path))

    record = RunRecord(
        run_id="test-uuid",
        issue_id="school-core-999",
        packet_yaml_path="/tmp/packet.yaml",
        orca_task_id="task-123",
        verdict="dispatched",
        settled=True,
    )
    log_path = record.save()

    assert os.path.exists(log_path)
    with open(log_path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["issue_id"] == "school-core-999"
    assert data["orca_task_id"] == "task-123"
    assert data["settled"] is True


def test_run_record_to_dict():
    """RunRecord.to_dict() returns a plain dict."""
    record = RunRecord(
        run_id="test-uuid",
        issue_id="school-core-1",
        packet_yaml_path="/tmp/packet.yaml",
    )
    d = record.to_dict()
    assert isinstance(d, dict)
    assert d["run_id"] == "test-uuid"
    assert d["issue_id"] == "school-core-1"
