"""
Tests for the append-only last_run.json logger (Task 1 of Durable Board plan).

Run: python -m pytest tests/test_last_run.py -v
"""

import json
from pathlib import Path

import pytest

from issue_bridge import record_run


class TestRecordRun:
    def test_last_run_appends(self, tmp_path):
        """record_run appends entries to a JSON list, adding timestamps."""
        p = tmp_path / "last_run.json"
        record_run(p, {"issue": 12, "status": "done", "agent": "x", "score": 80})
        record_run(p, {"issue": 13, "status": "blocked", "agent": "y", "score": 0})

        runs = json.loads(p.read_text())
        assert len(runs) == 2
        assert runs[-1]["issue"] == 13
        # Both entries got a server-side timestamp
        assert "timestamp" in runs[0]
        assert "timestamp" in runs[1]

    def test_writes_atomically(self, tmp_path):
        """record_run writes to a temp file then renames (atomic write)."""
        p = tmp_path / "atomic_run.json"
        record_run(p, {"issue": 1, "status": "done", "agent": "a", "score": 90})
        # No .tmp files should remain after write
        assert not list(tmp_path.glob("*.tmp"))
        runs = json.loads(p.read_text())
        assert len(runs) == 1

    def test_does_not_overwrite_existing_timestamp(self, tmp_path):
        """If the entry already has a timestamp, it is preserved."""
        p = tmp_path / "preserve_ts.json"
        record_run(p, {"issue": 5, "status": "done", "agent": "x",
                       "score": 75, "timestamp": "2026-01-01T00:00:00"})
        runs = json.loads(p.read_text())
        assert runs[0]["timestamp"] == "2026-01-01T00:00:00"

    def test_creates_missing_file(self, tmp_path):
        """record_run creates a new file if it doesn't exist."""
        p = tmp_path / "new" / "nested" / "last_run.json"
        record_run(p, {"issue": 99, "status": "dry_run", "agent": "z", "score": None})
        assert p.exists()
        runs = json.loads(p.read_text())
        assert len(runs) == 1
        assert runs[0]["issue"] == 99
        assert "timestamp" in runs[0]

    def test_preserves_existing_entries(self, tmp_path):
        """Appending to an existing file keeps previous entries."""
        p = tmp_path / "cumulative.json"
        for i in range(3):
            record_run(p, {"issue": i, "status": "done", "agent": "x", "score": 100})
        runs = json.loads(p.read_text())
        assert len(runs) == 3
        assert [r["issue"] for r in runs] == [0, 1, 2]
