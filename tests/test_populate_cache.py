"""
Tests for populate_board_cache.py (Task 5 of Durable Board plan).

Run: python -m pytest tests/test_populate_cache.py -v
"""

import json
import subprocess
from pathlib import Path

import pytest

import populate_board_cache


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_gh_output() -> str:
    """Simulate ``gh issue list --json ...`` output for two open issues."""
    return json.dumps(
        [
            {
                "number": 42,
                "title": "Fix authentication race condition",
                "labels": [{"name": "bug"}],
                "body": "Users report sporadic 401 errors.",
            },
            {
                "number": 99,
                "title": "Add end-to-end test suite",
                "labels": [{"name": "enhancement"}],
                "body": "We need integration tests covering the full login flow.",
            },
        ]
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestPopulateCache:
    """Tests for populate_board_cache.populate_cache()."""

    def test_writes_issue_number_keyed_dicts(
        self, tmp_path: Path, monkeypatch, fake_gh_output: str
    ):
        """When gh returns two issues, the output JSON contains two dicts
        each having an 'issue_number' key."""
        monkeypatch.setattr(populate_board_cache, "CACHE_PATH", tmp_path / "issues_cache.json")

        def mock_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, fake_gh_output, "")

        monkeypatch.setattr(subprocess, "run", mock_run)

        result = populate_board_cache.populate_cache(repo="test/repo")

        assert len(result) == 2
        for item in result:
            assert "issue_number" in item

        written = json.loads((tmp_path / "issues_cache.json").read_text())
        assert written == result
        assert written[0]["issue_number"] == 42
        assert written[1]["issue_number"] == 99
        assert written[0]["title"] == "Fix authentication race condition"
        assert written[1]["title"] == "Add end-to-end test suite"
        # Default fields
        assert written[0]["domain"] == "_default"
        assert written[0]["difficulty"] == "medium"
        assert written[0]["state"] == "open"

    def test_handles_empty_gh_output_gracefully(
        self, tmp_path: Path, monkeypatch
    ):
        """When gh returns empty stdout, write an empty list."""
        monkeypatch.setattr(populate_board_cache, "CACHE_PATH", tmp_path / "issues_cache.json")

        def mock_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", mock_run)

        result = populate_board_cache.populate_cache(repo="test/repo")
        assert result == []

        written = json.loads((tmp_path / "issues_cache.json").read_text())
        assert written == []

    def test_handles_missing_gh_gracefully(
        self, tmp_path: Path, monkeypatch
    ):
        """When gh is not installed (FileNotFoundError), write an empty list."""
        monkeypatch.setattr(populate_board_cache, "CACHE_PATH", tmp_path / "issues_cache.json")

        def mock_run(cmd, *args, **kwargs):
            raise FileNotFoundError("No such file or directory: 'gh'")

        monkeypatch.setattr(subprocess, "run", mock_run)

        result = populate_board_cache.populate_cache(repo="test/repo")
        assert result == []

        written = json.loads((tmp_path / "issues_cache.json").read_text())
        assert written == []

    def test_handles_nonzero_exit_gracefully(
        self, tmp_path: Path, monkeypatch
    ):
        """When gh exits non-zero (e.g. auth failure), write an empty list."""
        monkeypatch.setattr(populate_board_cache, "CACHE_PATH", tmp_path / "issues_cache.json")

        def mock_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "gh: not authenticated")

        monkeypatch.setattr(subprocess, "run", mock_run)

        result = populate_board_cache.populate_cache(repo="test/repo")
        assert result == []

        written = json.loads((tmp_path / "issues_cache.json").read_text())
        assert written == []

    def test_atomic_write_no_tmp_left_behind(
        self, tmp_path: Path, monkeypatch, fake_gh_output: str
    ):
        """After a successful write, no .tmp file remains."""
        monkeypatch.setattr(populate_board_cache, "CACHE_PATH", tmp_path / "issues_cache.json")

        def mock_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, fake_gh_output, "")

        monkeypatch.setattr(subprocess, "run", mock_run)

        populate_board_cache.populate_cache(repo="test/repo")

        assert (tmp_path / "issues_cache.json").exists()
        # No temp leftovers
        assert not list(tmp_path.glob("*.tmp"))
