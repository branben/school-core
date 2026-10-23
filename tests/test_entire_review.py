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


def _drive_review(monkeypatch, tmp_path, review_output, returncode=0, raise_exc=None,
                  changed_files=("src/app.py",)):
    """Run run_entire_review with the CLI present, a non-empty diff, and a
    stubbed `entire review` subprocess. Returns (result, review_cmd)."""
    monkeypatch.setattr("src.entire_review._get_entire_path",
                        lambda: "/usr/local/bin/entire")
    monkeypatch.setattr("src.entire_review._get_changed_files",
                        lambda *a, **k: list(changed_files))
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


def test_real_clean_review_with_verdict_still_passes(monkeypatch, tmp_path):
    """A real run that reached a verdict of `approve with nits` and found no
    defect is a genuine PASS. Fixture is verbatim from a live run, not invented."""
    res, _ = _drive_review(
        monkeypatch, tmp_path, REAL_APPROVE_WITH_NITS, returncode=0,
        changed_files=["feature.py"],
    )
    assert res["status"] == "pass"
    assert res["error"] is None
    assert res["findings"] == []


def test_no_verdict_and_no_findings_is_error_not_pass(monkeypatch, tmp_path):
    """A run that produced NEITHER a verdict NOR findings reached no conclusion.

    `entire review` exits 0 in this case, so exit code alone cannot separate
    "reviewed, nothing found" from "never reviewed". The verdict line is the
    reviewer's own statement that it looked — without it, this is ERROR.
    """
    res, _ = _drive_review(
        monkeypatch, tmp_path,
        "Running review with claude-code...\nNo issues found.\n",
        returncode=0,
    )
    assert res["status"] == "error"
    assert res["findings"] == []
    assert "no verdict" in res["error"].lower()


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


# ── The reviewer's model must be pinned, and a dead model must not read as pass ──
#
# Measured 2026-10-21: `entire review` exits 0 and prints "1 succeeded" even
# when the reviewer agent could not run at all. The only signal is the agent's
# own line, so both halves matter — pin a reachable model, and treat the
# failure line as an ERROR if it ever appears again.


# Verbatim from a real run against this machine (review handle 20261021T231516).
REAL_MODEL_FAILURE_OUTPUT = (
    "Reviewing feature vs main: 1 commit, 1 file changed, 2 uncommitted\n"
    "Running review with claude-code...\n"
    "# claude-code review\n"
    "\n"
    "There's an issue with the selected model "
    "(nvidia/nvidia/nemotron-3-super-120b-a12b). It may not exist or you may "
    "not have access to it. Run --model to pick a different model.\n"
    "1 agent(s) done — 1 succeeded, 0 failed, 0 cancelled\n"
    "\n"
    "Review complete.\n"
)


def test_real_model_failure_is_error_not_pass(monkeypatch, tmp_path):
    """A verbatim real failure must be ERROR even though it exits 0 and claims
    '1 succeeded' — previously this read as a clean PASS."""
    res, _ = _drive_review(
        monkeypatch, tmp_path, REAL_MODEL_FAILURE_OUTPUT, returncode=0,
    )
    assert res["status"] == "error"
    assert res["findings"] == []
    assert "selected model" in res["error"]


def test_ensure_review_profile_pins_a_model(monkeypatch, tmp_path):
    """`--set-model agent=model` must be passed, or the reviewer falls back to
    its own config (here: an upstream that times out)."""
    from src.entire_review import _DEFAULT_REVIEW_MODEL, _ensure_review_profile

    seen = {}

    def fake_run(cmd, **kwargs):
        if "--configure" in cmd:
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")
        if "--agents" in cmd:
            return mock.Mock(returncode=0,
                             stdout="claude-code: hooks installed", stderr="")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.delenv("ENTIRE_REVIEW_MODEL", raising=False)
    assert _ensure_review_profile(tmp_path, "/usr/local/bin/entire") is None
    assert f"claude-code={_DEFAULT_REVIEW_MODEL}" in seen["cmd"]


def test_review_model_is_env_overridable(monkeypatch):
    from src.entire_review import _DEFAULT_REVIEW_MODEL, _review_model
    monkeypatch.delenv("ENTIRE_REVIEW_MODEL", raising=False)
    assert _review_model() == _DEFAULT_REVIEW_MODEL
    monkeypatch.setenv("ENTIRE_REVIEW_MODEL", "auto/fast")
    assert _review_model() == "auto/fast"
    monkeypatch.setenv("ENTIRE_REVIEW_MODEL", "   ")
    assert _review_model() == _DEFAULT_REVIEW_MODEL


# ── The parser must read the format the reviewer ACTUALLY emits ──────────────
#
# Measured 2026-10-21 from two live `entire review` runs (model auto/best-coding,
# cost $0.34 each). The reviewer's final report is NOT `Critical: file.py:42 — msg`
# — the only format `_parse_entire_output` ever understood:
#
#   run A (handle er-reg-58284, 18:59): a verdict line only, no findings.
#   run B (handle real-77844, 19:20): a fenced ```json array of findings — and
#                                     that review HAD found a defect.
#
# Because neither matched, every real review parsed to ZERO findings. Run B is
# the proof: a defect the reviewer surfaced was reported to the caller as a
# clean PASS. These fixtures are verbatim, not invented.

# Verbatim final report, live run 2026-10-21T18:59 (handle er-reg-58284).
REAL_APPROVE_WITH_NITS = (
    "approve with nits: The new file `feature.py` contains a placeholder "
    "function with a `TODO` comment, indicating incomplete work. This is not a "
    "bug, but a notice for future implementation."
)

# Verbatim final report, live run 2026-10-21T19:20 (handle real-77844).
REAL_JSON_FINDINGS = (
    "```json\n"
    "[\n"
    "  {\n"
    '    "file": "/private/tmp/real-77844/test_run_entire_review_real_in0/feature.py",\n'
    '    "line": 1,\n'
    '    "summary": "Incomplete feature implementation, function is a placeholder.",\n'
    '    "failure_scenario": "The `feature_func` only contains a `pass" statement."\n'
    "  }\n"
    "]\n"
    "```"
)


def test_parse_real_json_array_findings():
    """A real review's JSON-array report must yield findings, not []."""
    findings = _parse_entire_output(REAL_JSON_FINDINGS, ["feature.py"])
    assert len(findings) == 1
    assert findings[0].file.endswith("feature.py")
    assert findings[0].line == 1
    assert "placeholder" in findings[0].message


def test_parse_real_json_findings_filtered_to_changed_files():
    """A JSON finding outside the diff is still filtered out."""
    findings = _parse_entire_output(REAL_JSON_FINDINGS, ["other.py"])
    assert findings == []


def test_extract_verdict_from_real_output():
    from src.entire_review import _extract_verdict
    assert _extract_verdict(REAL_APPROVE_WITH_NITS) == "approve with nits"
    assert _extract_verdict("request changes: the guard is inverted") == "request changes"
    assert _extract_verdict("approve: looks good") == "approve"
    assert _extract_verdict("no verdict line in here") is None


def test_real_json_findings_review_is_not_pass(monkeypatch, tmp_path):
    """The live run that FOUND a defect must not be reported as PASS."""
    res, _ = _drive_review(
        monkeypatch, tmp_path, REAL_JSON_FINDINGS, returncode=0,
        changed_files=["feature.py"],
    )
    assert res["status"] == "fail"
    assert len(res["findings"]) == 1
    assert res["findings"][0]["file"].endswith("feature.py")


def test_request_changes_verdict_fails_even_with_no_findings(monkeypatch, tmp_path):
    """The reviewer's own verdict outranks the parsed findings."""
    res, _ = _drive_review(
        monkeypatch, tmp_path,
        "request changes: the retry loop can spin forever",
        returncode=0, changed_files=["feature.py"],
    )
    assert res["status"] == "fail"
    assert res["error"] is None

