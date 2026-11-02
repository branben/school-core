"""Tests for scripts/record_deviation.py (0% coverage)."""

import json
import sys
from pathlib import Path

import pytest

from scripts.record_deviation import main


class TestRecordDeviation:
    def test_required_args_missing(self):
        with pytest.raises(SystemExit):
            main()

    def test_logs_deviation_and_prints_json(self, tmp_path, monkeypatch, capsys):
        entries = []
        captured = {}

        class FakeLog:
            def record_deviation(self, **kwargs):
                entries.append(kwargs)
                return {"id": "dev-1", "issue": kwargs["issue"]}

        monkeypatch.setattr("scripts.record_deviation.get_log", lambda: FakeLog())
        monkeypatch.setenv("HOME", str(tmp_path))

        sys.argv = [
            "record_deviation.py",
            "--issue", "school-core-123",
            "--agent", "student-coder",
            "--summary", "summary text",
            "--plan-expected", "plan text",
            "--code-revealed", "code text",
            "--decision", "decision text",
            "--revisit", "revisit text",
        ]
        rc = main()
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["id"] == "dev-1"
        assert entries[0]["issue"] == "school-core-123"

    def test_log_path_override(self, tmp_path, monkeypatch, capsys):
        log_path = tmp_path / "custom.json"

        class FakeLog:
            def __init__(self, path=None):
                self.path = path

            def record_deviation(self, **kwargs):
                return {"id": "dev-2", "path": str(self.path)}

        monkeypatch.setattr("scripts.record_deviation.ActivityLog", FakeLog)
        monkeypatch.setattr("scripts.record_deviation.get_log", lambda: None)

        sys.argv = [
            "record_deviation.py",
            "--issue", "school-core-456",
            "--agent", "student-coder",
            "--summary", "s",
            "--plan-expected", "p",
            "--code-revealed", "c",
            "--decision", "d",
            "--revisit", "r",
            "--log-path", str(log_path),
        ]
        rc = main()
        assert rc == 0

    def test_oserror_returns_one(self, monkeypatch, capsys):
        monkeypatch.setattr("scripts.record_deviation.get_log", lambda: (_ for _ in ()).throw(OSError("disk full")))
        sys.argv = [
            "record_deviation.py",
            "--issue", "school-core-789",
            "--agent", "student-coder",
            "--summary", "s",
            "--plan-expected", "p",
            "--code-revealed", "c",
            "--decision", "d",
            "--revisit", "r",
        ]
        rc = main()
        assert rc == 1
        assert "record_deviation" in capsys.readouterr().err
