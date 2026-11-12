"""Tests for scripts/check_tdd.py (0% coverage)."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import check_tdd


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    run("add", ".")
    run("commit", "-qm", "base")
    return repo


class TestHelpers:
    def test_is_source_true(self):
        assert check_tdd._is_source("src/foo/bar.py") is True
        assert check_tdd._is_source("src/module.py") is True

    def test_is_source_false(self):
        assert check_tdd._is_source("tests/test_foo.py") is False
        assert check_tdd._is_source("docs/guide.md") is False
        assert check_tdd._is_source("scripts/helper.py") is False

    def test_is_test_true(self):
        assert check_tdd._is_test("tests/test_foo.py") is True
        assert check_tdd._is_test("tests/bar_test.py") is True

    def test_is_test_false(self):
        assert check_tdd._is_test("src/foo.py") is False
        assert check_tdd._is_test("scripts/helper.py") is False

    def test_test_for_source_mapping(self):
        assert check_tdd._test_for_source("src/foo/bar.py") == "tests/test_foo_bar.py"
        assert check_tdd._test_for_source("src/module.py") == "tests/test_module.py"

    def test_test_for_source_short_path(self):
        assert check_tdd._test_for_source("src.py") == ""

    def test_changed_files_returns_list(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "src" / "new.py").mkdir(parents=True)
        (repo / "src" / "new.py" / "mod.py").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)
        files = check_tdd._changed_files()
        assert "src/new.py/mod.py" in files


class TestTddGate:
    def test_new_source_without_test_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_tdd, "_changed_files", lambda: ["src/new.py"])
        monkeypatch.setattr(check_tdd.os.path, "exists", lambda p: p == "src/new.py")
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "git" and cmd[1] == "ls-files":
                class GitR:
                    returncode = 1
                return GitR()
            class ProcR:
                returncode = 0
                stdout = ""
            return ProcR()
        monkeypatch.setattr(check_tdd.subprocess, "run", fake_run)
        assert check_tdd.main() == 1
        assert any("ls-files" in c for c in calls)

    def test_new_source_with_test_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_tdd, "_changed_files", lambda: ["src/new.py", "tests/test_new.py"])
        monkeypatch.setattr(check_tdd.os.path, "exists", lambda p: p in ("src/new.py", "tests/test_new.py"))
        def fake_run(cmd, **kwargs):
            class ProcR:
                returncode = 0
                stdout = ""
            return ProcR()
        monkeypatch.setattr(check_tdd.subprocess, "run", fake_run)
        assert check_tdd.main() == 0

    def test_existing_source_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_tdd, "_changed_files", lambda: ["src/old.py"])
        monkeypatch.setattr(check_tdd.os.path, "exists", lambda p: p == "src/old.py")
        def fake_run(cmd, **kwargs):
            class ProcR:
                returncode = 0
                stdout = ""
            return ProcR()
        monkeypatch.setattr(check_tdd.subprocess, "run", fake_run)
        assert check_tdd.main() == 0

    def test_tddskip_bypass(self, monkeypatch):
        monkeypatch.setenv("TDDSKIP", "1")
        assert check_tdd.main() == 0

    def test_no_changed_sources_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_tdd, "_changed_files", lambda: ["docs/guide.md"])
        monkeypatch.setattr(check_tdd.os.path, "exists", lambda p: False)
        assert check_tdd.main() == 0
