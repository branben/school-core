"""Tests for src/entire_review.py — the `entire review` pre-merge shim.

Covers the R12 path fallback (worktree shells without ~/.local/bin on PATH),
the severity-line parser, and the graceful skip when the CLI is missing.
"""

import os
import stat
from pathlib import Path
from unittest import mock

from src.entire_review import (
    _get_entire_path,
    _parse_entire_output,
    run_entire_review,
)


def test_get_entire_path_uses_which_first():
    """PATH wins when `entire` is already on PATH."""
    with mock.patch("shutil.which", return_value="/usr/local/bin/entire"):
        assert _get_entire_path() == "/usr/local/bin/entire"


def test_get_entire_path_falls_back_to_home_local_bin(tmp_path, monkeypatch):
    """Orca worktree shells miss ~/.local/bin on PATH — fall back to it (R12)."""
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    entire = bin_dir / "entire"
    entire.write_text("#!/bin/sh\n")
    entire.chmod(entire.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with mock.patch("shutil.which", return_value=None):
        found = _get_entire_path()
    assert found == str(entire)


def test_get_entire_path_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with mock.patch("shutil.which", return_value=None):
        assert _get_entire_path() is None


def test_parse_entire_output_extracts_and_filters_changed_files():
    content = (
        "High: src/app.py:17 — unused variable `x`\n"
        "Critical: src/app.py:42 — SQL injection\n"
        "Medium: vendor/lib.py:3 — not in the diff, must be filtered\n"
        "Low: src/app.py:88 — nit\n"
    )
    findings = _parse_entire_output(content, ["src/app.py"])
    assert len(findings) == 3  # vendor/lib.py filtered out
    assert all(f.file == "src/app.py" for f in findings)
    sev = {f.severity for f in findings}
    assert sev == {"HIGH", "CRITICAL", "LOW"}
    assert findings[1].line == 42


def test_parse_entire_output_dedupes():
    content = (
        "High: a.py:1 — dup\n"
        "High: a.py:1 — dup\n"
    )
    assert len(_parse_entire_output(content, ["a.py"])) == 1


def test_run_entire_review_skips_when_cli_missing(tmp_path):
    """No CLI → 'skipped' status, error set, workspace note written."""
    with mock.patch("src.entire_review._get_entire_path", return_value=None):
        res = run_entire_review(str(tmp_path))
    assert res["status"] == "skipped"
    assert res["skipped"] is True
    assert "not found" in res["error"]
    workspace = tmp_path / ".hermes" / "review_workspace"
    assert (workspace / "entire_review.md").exists()


def test_run_entire_review_skips_empty_diff(tmp_path):
    """No changed files vs base → pass (nothing to review), no CLI needed."""
    with mock.patch("src.entire_review._get_entire_path", return_value="/usr/local/bin/entire"), \
         mock.patch("src.entire_review._get_changed_files", return_value=[]):
        res = run_entire_review(str(tmp_path))
    assert res["status"] == "pass"
    assert res["skipped"] is False
