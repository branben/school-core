"""Tests for scripts/sanitize_ledger.py (0% coverage)."""

import json
from pathlib import Path

import pytest

from scripts.sanitize_ledger import sanitize


class TestSanitize:
    def test_missing_file_returns_zero(self, tmp_path):
        target = tmp_path / "missing.json"
        assert sanitize(target) == 0

    def test_malformed_json_returns_zero(self, tmp_path):
        target = tmp_path / "bad.json"
        target.write_text("not json")
        assert sanitize(target) == 0

    def test_non_list_json_returns_zero(self, tmp_path):
        target = tmp_path / "dict.json"
        target.write_text('{"a": 1}')
        assert sanitize(target) == 0

    def test_removes_active_rows(self, tmp_path):
        target = tmp_path / "runs.json"
        rows = [
            {"id": 1, "status": "running"},
            {"id": 2, "status": "done"},
            {"id": 3, "status": "blocked"},
            {"id": 4, "status": "success"},
        ]
        target.write_text(json.dumps(rows))
        removed = sanitize(target)
        assert removed == 2
        clean = json.loads(target.read_text())
        assert [r["id"] for r in clean] == [2, 4]

    def test_keeps_inactive_rows(self, tmp_path):
        target = tmp_path / "runs.json"
        rows = [
            {"id": 1, "status": "success"},
            {"id": 2, "status": "failed"},
            {"id": 3, "status": "cancelled"},
        ]
        target.write_text(json.dumps(rows))
        removed = sanitize(target)
        assert removed == 0
        clean = json.loads(target.read_text())
        assert len(clean) == 3

    def test_no_active_rows_returns_zero(self, tmp_path):
        target = tmp_path / "runs.json"
        target.write_text('[]')
        assert sanitize(target) == 0

    def test_empty_list_returns_zero(self, tmp_path):
        target = tmp_path / "runs.json"
        target.write_text('[]')
        assert sanitize(target) == 0

    def test_writes_compact_json(self, tmp_path):
        target = tmp_path / "runs.json"
        target.write_text('[{"status":"running"}]')
        sanitize(target)
        text = target.read_text()
        assert json.loads(text) == []
        assert text.endswith("\n")

    def test_prints_removal_message(self, tmp_path, capsys):
        target = tmp_path / "runs.json"
        target.write_text('[{"status":"running"}]')
        sanitize(target)
        captured = capsys.readouterr()
        assert "removed 1 active row(s)" in captured.out

    def test_no_message_when_nothing_removed(self, tmp_path, capsys):
        target = tmp_path / "runs.json"
        target.write_text('[{"status":"done"}]')
        sanitize(target)
        captured = capsys.readouterr()
        assert captured.out == ""
