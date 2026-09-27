"""Tests for scripts/review_gate.py.

The gate is worthless if it cannot FAIL. These tests build real git repos
in tmp_path with real diffs, so the assertions exercise the same code
path CI runs -- not a mocked stand-in.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import review_gate  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (r / "README.md").write_text("base\n")
    run("add", ".")
    run("commit", "-qm", "base")
    return r


def _commit(r: Path, name: str, content: str) -> None:
    (r / name).parent.mkdir(parents=True, exist_ok=True)
    (r / name).write_text(content)
    run = lambda *a: subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, check=True)
    run("add", ".")
    run("commit", "-qm", f"add {name}")


def _gate(r: Path) -> int:
    return subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)), "--base", "HEAD~1", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode


def test_clean_docs_only_change_passes(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "docs/guide.md", "hello\n")
    assert _gate(r) == 0


def test_detects_github_pat(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "config.yml", 'token: ghp_' + "a" * 36 + "\n")
    assert _gate(r) == 1, "a committed classic PAT must fail the gate"


def test_detects_aws_key(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "settings.ini", "key = AKIAIOSFODNN7EXAMPLE\n")
    assert _gate(r) == 1


def test_detects_private_key(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "deploy.yml", "-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    assert _gate(r) == 1


def test_source_change_without_test_fails(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "module.py", "def f():\n    return 1\n")
    assert _gate(r) == 1, "source changed with no test changed must fail"


def test_source_change_with_test_passes(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "module.py", "def f():\n    return 1\n")
    _commit(r, "tests/test_module.py", "def test_f():\n    assert True\n")
    # base is two commits back so BOTH files are in the diff
    code = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~2", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode
    assert code == 0, "source + test in the same PR must pass"


def test_detects_junk_file(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "module.pyc", "junk\n")
    _commit(r, "tests/test_x.py", "def test_x():\n    assert True\n")
    code = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~2", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode
    assert code == 1, "a committed .pyc must fail the gate"


def test_reports_every_failure_not_just_the_first(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "a.py", "TOKEN = 'ghp_" + "a" * 36 + "'\n")
    _commit(r, "b.pyc", "junk\n")
    code = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~2", "--head", "HEAD", "--json"],
        cwd=r, capture_output=True, text=True)
    import json as _json
    data = _json.loads(code.stdout)
    kinds = {f["check"] for f in data["failures"]}
    assert code.returncode == 1
    assert "secret" in kinds and "junk" in kinds, kinds


def test_json_output_is_machine_readable(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "docs/x.md", "ok\n")
    out = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~1", "--head", "HEAD", "--json"],
        cwd=r, capture_output=True, text=True)
    import json as _json
    data = _json.loads(out.stdout)
    assert data["pass"] is True
    assert data["changedFiles"] == 1


def test_markdown_trailing_whitespace_is_not_a_finding(tmp_path):
    """Markdown tables legitimately use padding spaces."""
    r = _repo(tmp_path)
    (r / "docs").mkdir()
    (r / "docs" / "table.md").write_text("| a | b |   \n|---|---|---|\n")
    run = lambda *a: subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, check=True)
    run("add", ".")
    run("commit", "-qm", "docs table")
    code = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~1", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode
    assert code == 0, "markdown padding must not fail the gate"


def test_python_trailing_whitespace_still_fails(tmp_path):
    r = _repo(tmp_path)
    (r / "mod.py").write_text("x = 1   \n")
    (r / "tests").mkdir()
    (r / "tests" / "test_mod.py").write_text("def test_x():\n    assert True\n")
    run = lambda *a: subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, check=True)
    run("add", ".")
    run("commit", "-qm", "ws")
    code = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~1", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode
    assert code == 1, "trailing whitespace in python must still fail"
