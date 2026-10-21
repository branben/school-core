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


# ── Fail-closed: a review that produced no verdict must never read as "pass" ──
#
# Three separate defects each reported "pass" for a review that never happened:
# a missing profile/hooks (exit 1), a too-short timeout, and an unreachable
# reviewer API (zero findings). These pin the contract: such runs are ERROR.


def _drive_review(monkeypatch, tmp_path, review_output, returncode=0, raise_exc=None):
    """Run run_entire_review with the CLI present, a non-empty diff, and a
    stubbed `entire review` subprocess. Returns (result, review_cmd)."""
    monkeypatch.setattr("src.entire_review._get_entire_path",
                        lambda: "/usr/local/bin/entire")
    monkeypatch.setattr("src.entire_review._get_changed_files",
                        lambda *a, **k: ["src/app.py"])
    monkeypatch.setattr("src.entire_review._ensure_review_profile",
                        lambda *a, **k: None)
    seen = {}

    def fake_run(cmd, **kwargs):
        if "--profile" in cmd:
            seen["cmd"] = cmd
            if raise_exc is not None:
                raise raise_exc
            return mock.Mock(returncode=returncode,
                             stdout=review_output, stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    return run_entire_review(str(tmp_path)), seen["cmd"]


def test_missing_profile_and_hooks_is_error_not_pass(monkeypatch, tmp_path):
    """`entire review` exiting 1 (no profile/hooks) is ERROR, not a clean pass."""
    res, _ = _drive_review(
        monkeypatch, tmp_path,
        "no agents with review runner adapters and hooks installed",
        returncode=1,
    )
    assert res["status"] == "error"
    assert res["skipped"] is False
    assert res["error"]


def test_review_timeout_is_error_not_pass(monkeypatch, tmp_path):
    """A killed review (TimeoutExpired) is ERROR — it reached no verdict."""
    import subprocess as _sp
    res, _ = _drive_review(
        monkeypatch, tmp_path, "",
        raise_exc=_sp.TimeoutExpired(cmd="entire", timeout=1),
    )
    assert res["status"] == "error"
    assert res["error"]


def test_zero_findings_with_agent_error_is_error(monkeypatch, tmp_path):
    """Reviewer API unreachable → zero findings + an error marker = ERROR."""
    res, _ = _drive_review(
        monkeypatch, tmp_path,
        "Running review with claude-code...\nAPI Error: Connection refused\n",
        returncode=0,
    )
    assert res["status"] == "error"
    assert res["findings"] == []


def test_clean_review_with_no_findings_still_passes(monkeypatch, tmp_path):
    """A real run with no defects and no error marker is a genuine PASS."""
    res, _ = _drive_review(
        monkeypatch, tmp_path,
        "Running review with claude-code...\nNo issues found.\n",
        returncode=0,
    )
    assert res["status"] == "pass"
    assert res["error"] is None


def test_review_timeout_is_configurable(monkeypatch):
    """ENTIRE_REVIEW_TIMEOUT overrides the cap; junk falls back to the default."""
    from src.entire_review import _DEFAULT_REVIEW_TIMEOUT_S, _review_timeout_s
    monkeypatch.delenv("ENTIRE_REVIEW_TIMEOUT", raising=False)
    assert _review_timeout_s() == _DEFAULT_REVIEW_TIMEOUT_S
    monkeypatch.setenv("ENTIRE_REVIEW_TIMEOUT", "42")
    assert _review_timeout_s() == 42
    monkeypatch.setenv("ENTIRE_REVIEW_TIMEOUT", "not-a-number")
    assert _review_timeout_s() == _DEFAULT_REVIEW_TIMEOUT_S


# ── Bootstrap: the profile/hooks a fresh clone is missing ─────────────────────


def test_ensure_review_profile_skips_agent_add_when_hooks_present(monkeypatch, tmp_path):
    """`agent add` rewrites .claude/settings.json — run it only when hooks are absent."""
    from src.entire_review import _ensure_review_profile

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--configure" in cmd:
            return mock.Mock(returncode=0, stdout="", stderr="")
        if "--agents" in cmd:
            return mock.Mock(returncode=0,
                             stdout="claude-code: hooks installed", stderr="")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _ensure_review_profile(tmp_path, "/usr/local/bin/entire") is None
    assert not any(len(c) >= 3 and c[1:3] == ["agent", "add"] for c in calls)


def test_ensure_review_profile_installs_hooks_when_missing(monkeypatch, tmp_path):
    """Hooks reported missing → `agent add` runs."""
    from src.entire_review import _ensure_review_profile

    def fake_run(cmd, **kwargs):
        if "--configure" in cmd:
            return mock.Mock(returncode=0, stdout="", stderr="")
        if "--agents" in cmd:
            return mock.Mock(returncode=0,
                             stdout="claude-code: hooks NOT installed", stderr="")
        if len(cmd) >= 3 and cmd[1:3] == ["agent", "add"]:
            return mock.Mock(returncode=0, stdout="Installed 8 hooks", stderr="")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _ensure_review_profile(tmp_path, "/usr/local/bin/entire") is None


def test_ensure_review_profile_reports_configure_failure(monkeypatch, tmp_path):
    """A failing `--configure` returns the error string instead of continuing."""
    from src.entire_review import _ensure_review_profile

    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock.Mock(
        returncode=1, stdout="", stderr="boom"))
    err = _ensure_review_profile(tmp_path, "/usr/local/bin/entire")
    assert err and "boom" in err


def test_setup_failure_is_error_not_pass(monkeypatch, tmp_path):
    """When the bootstrap itself fails, the sensor reports ERROR."""
    monkeypatch.setattr("src.entire_review._get_entire_path",
                        lambda: "/usr/local/bin/entire")
    monkeypatch.setattr("src.entire_review._get_changed_files",
                        lambda *a, **k: ["src/app.py"])
    monkeypatch.setattr("src.entire_review._ensure_review_profile",
                        lambda *a, **k: "entire review --configure failed: boom")
    res = run_entire_review(str(tmp_path))
    assert res["status"] == "error"
    assert "boom" in res["error"]
    assert res["skipped"] is False


# ── A diff that could not be computed is NOT an empty diff ───────────────────
#
# `_get_changed_files` used to swallow every failure into `[]`, so "not a git
# repo / missing base ref / git failure" was indistinguishable from "no changes"
# and the sensor reported a clean PASS. Unknown must never read as reviewed.


def test_get_changed_files_returns_none_when_git_fails(tmp_path):
    """A non-repo yields None (unknown), not [] (empty)."""
    from src.entire_review import _get_changed_files
    assert _get_changed_files(str(tmp_path), "main") is None


def test_get_changed_files_returns_list_for_real_repo(tmp_path):
    """A real repo with a commit on a feature branch yields the changed files."""
    import subprocess
    from src.entire_review import _get_changed_files
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "config", "user.email", "t@e"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "feat"], cwd=tmp_path, check=True)
    assert _get_changed_files(str(tmp_path), "main") == ["b.py"]


def test_failed_diff_is_error_not_pass(monkeypatch, tmp_path):
    """A diff that could not be computed is ERROR, never a silent pass."""
    monkeypatch.setattr("src.entire_review._get_entire_path",
                        lambda: "/usr/local/bin/entire")
    monkeypatch.setattr("src.entire_review._get_changed_files",
                        lambda *a, **k: None)
    res = run_entire_review(str(tmp_path))
    assert res["status"] == "error"
    assert "could not compute git diff" in res["error"]
    assert res["skipped"] is False


def test_hooks_check_is_anchored_to_the_agent_name(monkeypatch, tmp_path):
    """Another agent having hooks must NOT make us skip installing OURS."""
    from src.entire_review import _ensure_review_profile

    calls = []

    def fake_run(cmd, **kwargs):
        if "--configure" in cmd:
            return mock.Mock(returncode=0, stdout="", stderr="")
        if "--agents" in cmd:
            # a DIFFERENT agent has hooks; ours does not
            return mock.Mock(returncode=0,
                             stdout="codex: hooks installed", stderr="")
        if len(cmd) >= 3 and cmd[1:3] == ["agent", "add"]:
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="Installed", stderr="")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _ensure_review_profile(tmp_path, "/usr/local/bin/entire") is None
    assert calls, "agent add must run for OUR agent even when another has hooks"

