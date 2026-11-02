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


def _commit(r: Path, name: str, content: str, msg: str = "") -> None:
    (r / name).parent.mkdir(parents=True, exist_ok=True)
    (r / name).write_text(content)
    run = lambda *a: subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, check=True)
    run("add", ".")
    commit_msg = msg if msg else f"add {name}"
    run("commit", "-qm", commit_msg)


def _gate(r: Path) -> int:
    return subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)), "--base", "HEAD~1", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode


def test_clean_docs_only_change_passes(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "docs/guide.md", "hello\n")
    assert _gate(r) == 0


def test_docs_only_pr_exempt_from_bead_id(tmp_path):
    """NO_BEAD_NEEDED: docs/config/chore changes must not require a bead ID."""
    import json as _json
    r = _repo(tmp_path)
    _commit(r, "README.md", "hello\n")
    out = _gate_args(r, "--json")
    data = _json.loads(out.stdout)
    assert data["pass"] is True, data
    assert not any(f["check"] == "no-bead-link" for f in data["failures"])


def test_config_only_pr_exempt_from_bead_id(tmp_path):
    """YAML/ini/toml changes are also exempt from the bead-ID requirement."""
    import json as _json
    r = _repo(tmp_path)
    _commit(r, ".gitignore", "*.pyc\n")
    out = _gate_args(r, "--json")
    data = _json.loads(out.stdout)
    assert data["pass"] is True, data
    assert not any(f["check"] == "no-bead-link" for f in data["failures"])


def test_mixed_docs_and_source_still_needs_bead_id(tmp_path):
    """If even one changed file is source code, the bead-ID check still fires."""
    import json as _json
    r = _repo(tmp_path)
    _commit(r, "docs/guide.md", "hello\n")
    _commit(r, "module.py", "def f():\n    return 1\n")
    # Do NOT include a bead ID in the source commit message
    out = _gate_args(r, "--base", "HEAD~2", "--head", "HEAD", "--json")
    data = _json.loads(out.stdout)
    assert any(f["check"] == "no-bead-link" for f in data["failures"]), (
        "a PR that mixes docs with source must still require a bead ID"
    )


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
    _commit(r, "tests/test_module.py", "def test_f():\n    assert True\n", msg="add test_module.py school-core-1234")
    # base is two commits back so BOTH files are in the diff
    code = subprocess.run(
        [sys.executable, str(Path(review_gate.__file__)),
         "--base", "HEAD~2", "--head", "HEAD"],
        cwd=r, capture_output=True, text=True).returncode
    assert code == 0, "source + test with bead ID in same PR must pass"


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


def _gate_args(r: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(sys.executable), str(Path(review_gate.__file__)),
         "--base", "HEAD~1", "--head", "HEAD", *args],
        cwd=r, capture_output=True, text=True)


def test_unresolvable_head_ref_fails(tmp_path):
    """The #117 bug: `--head origin/<sha>` is not a ref name, so `git diff`
    failed on stderr, the error was discarded, and the gate reported 0
    changed files -- a permanent PASS. An unresolvable ref must FAIL."""
    r = _repo(tmp_path)
    _commit(r, "docs/a.md", "hello\n")
    out = _gate_args(r, "--base", "HEAD~1", "--head", "origin/deadbeef")
    assert out.returncode == 1, out.stdout + out.stderr


def test_unresolvable_base_ref_fails(tmp_path):
    r = _repo(tmp_path)
    _commit(r, "docs/a.md", "hello\n")
    out = _gate_args(r, "--base", "origin/deadbeef", "--head", "HEAD")
    assert out.returncode == 1, out.stdout + out.stderr


def test_empty_diff_fails(tmp_path):
    """A PR that changes nothing must not pass a review gate."""
    r = _repo(tmp_path)
    out = _gate_args(r, "--base", "HEAD", "--head", "HEAD")
    assert out.returncode == 1
    assert "empty-diff" in out.stdout


def test_empty_diff_reported_in_json(tmp_path):
    r = _repo(tmp_path)
    out = _gate_args(r, "--base", "HEAD", "--head", "HEAD", "--json")
    import json as _json
    data = _json.loads(out.stdout)
    assert data["pass"] is False
    assert data["changedFiles"] == 0
    assert any(f["check"] == "empty-diff" for f in data["failures"])


# ---------------------------------------------------------------------------
# Internal helpers — previously 18% coverage
# ---------------------------------------------------------------------------

class TestIsTest:
    def test_test_prefix(self):
        assert review_gate.is_test("tests/test_foo.py") is True

    def test_test_suffix(self):
        assert review_gate.is_test("src/foo_test.py") is True

    def test_non_test(self):
        assert review_gate.is_test("src/foo.py") is False
        assert review_gate.is_test("docs/guide.md") is False


class TestTestSourceBlob:
    def test_includes_filenames_and_contents(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_alpha.py").write_text("def test_a(): pass\n")
        (tests_dir / "test_beta.py").write_text("def test_b(): pass\n")

        def fake_git(*args):
            if args[:1] == ("rev-parse",):
                return str(repo) + "\n"
            raise SystemExit(1)

        monkeypatch.setattr(review_gate, "git", fake_git)
        blob = review_gate._test_source_blob()
        assert "test_alpha.py" in blob
        assert "test_beta.py" in blob
        assert "def test_a" in blob
        assert "def test_b" in blob


class TestLoadJson:
    def test_head_used_when_present(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "f.json").write_text('{"from": "head"}')
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
        run("add", ".")
        run("commit", "-am", "add f")

        def repo_git(*args):
            proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
            return proc.stdout

        saved = review_gate.git
        review_gate.git = repo_git
        try:
            loaded = review_gate._load_json("HEAD~1", "HEAD", "f.json")
        finally:
            review_gate.git = saved
        assert loaded == {"from": "head"}

    def test_falls_back_to_base_when_head_missing(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "f.json").write_text('{"from": "base"}')
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
        run("add", ".")
        run("commit", "-am", "base")
        (repo / "f.json").unlink()
        run("add", ".")
        run("commit", "-am", "remove")

        def repo_git(*args):
            proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
            return proc.stdout

        saved = review_gate.git
        review_gate.git = repo_git
        try:
            loaded = review_gate._load_json("HEAD~1", "HEAD", "f.json")
        finally:
            review_gate.git = saved
        assert loaded == {"from": "base"}


class TestCheckSourceBlock:
    def test_missing_fields_fail(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "source.json").write_text('{"bead": "x"}')
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
        run("add", ".")
        run("commit", "-am", "add source")
        failures = review_gate._check_source_block("HEAD~1", "HEAD", ["source.json"])
        kinds = {f["check"] for f in failures}
        assert "source-block" in kinds

    def test_absent_artifact_fails(self, tmp_path):
        repo = _repo(tmp_path)
        payload = {
            "bead": "school-core-1234",
            "spec": "spec.md",
            "decisions": "decisions.md",
            "artifacts": ["docs/missing.html"],
            "spec_acceptance": "ok",
        }
        (repo / "source.json").write_text(__import__("json").dumps(payload))
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)
        run("add", ".")
        run("commit", "-am", "add source")
        failures = review_gate._check_source_block("HEAD~1", "HEAD", ["source.json"])
        kinds = {f["check"] for f in failures}
        assert "source-block" in kinds
        assert any("missing" in f["detail"].lower() for f in failures)
