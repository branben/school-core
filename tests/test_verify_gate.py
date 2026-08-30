"""Tests for verify_gate.py — the hermetic execution stage.

These run without Nix (they mock the subprocess layer) so they're portable in
any CI. The real `nix develop` path is exercised manually / in integration.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from verify_gate import (
    _build_verify_script,
    _discover_commands,
    _flake_ref,
    _has_node_modules,
    run_verify_gate,
    ALLOWED_CONFIG_NAMES,
)


@pytest.fixture(autouse=True)
def _no_strict_env_leak(monkeypatch):
    """Default-mode tests assume VERIFY_GATE_STRICT is unset.

    A developer shell exporting it must not silently flip their skip-vs-fail
    expectations; each test starts with a clean env.
    """
    monkeypatch.delenv("VERIFY_GATE_STRICT", raising=False)


def _write_pkg(tmp_path: Path, sub: str, scripts: dict) -> None:
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(json.dumps({"scripts": scripts}))


def _successful_marker_output(count: int) -> str:
    lines = []
    for index in range(count):
        lines.extend([
            f"__SCHOOL_VERIFY_START_{index}__",
            f"__SCHOOL_VERIFY_END_{index}__0",
        ])
    return "\n".join(lines) + "\n"


def test_discovers_root_package_scripts(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "tsc --noEmit", "test": "vitest"})
    cmds = _discover_commands(tmp_path, None)
    names = {c["name"] for c in cmds}
    assert any("npm:typecheck" in n for n in names)
    assert any("npm:test" in n for n in names)


def test_discovers_subproject_separately(tmp_path):
    # Root has no typecheck; a `mobile/` subdir typechecks on its own.
    # This is the exact Orca gap: root typecheck passes, mobile does not.
    _write_pkg(tmp_path, ".", {"test": "vitest"})
    _write_pkg(tmp_path, "mobile", {"typecheck": "tsc --noEmit"})
    cmds = _discover_commands(tmp_path, None)
    mob = [c for c in cmds if c["name"].startswith("mobile/")]
    assert mob, "sub-project typecheck must be discovered"
    assert mob[0]["cwd"] == "mobile"


def test_explicit_project_verify_yaml_wins(tmp_path):
    (tmp_path / "project_verify.yaml").write_text(
        "verify:\n  - name: custom\n    cmd: echo hi\n    cwd: .\n"
    )
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    cmds = _discover_commands(tmp_path, tmp_path / "project_verify.yaml")
    assert len(cmds) == 1
    assert cmds[0]["name"] == "custom"


def test_project_verify_yaml_shadows_recursive_discovery(tmp_path):
    """A declared project_verify.yaml must suppress the recursive npm/pyproject
    inference entirely — this is what keeps school-core's gate at 1 honest
    command instead of the 9 orca/mobile npm commands that can't run in the
    network-less verifyShell (command-not-found → filtered as infra noise)."""
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    _write_pkg(tmp_path, "orca/mobile", {"typecheck": "tsc", "lint": "eslint", "test": "jest"})
    (tmp_path / "project_verify.yaml").write_text(
        "verify:\n  - name: core-python-compile\n    cmd: python3 -m compileall -q *.py\n    cwd: .\n"
    )
    cmds = _discover_commands(tmp_path, tmp_path / "project_verify.yaml")
    assert len(cmds) == 1
    assert cmds[0]["name"] == "core-python-compile"
    assert not any("npm:" in c["name"] or "orca" in c["name"] for c in cmds)


def test_verify_wrapper_executes_each_command_and_emits_markers(tmp_path):
    """Exercise the generated wrapper in a real shell, not only a subprocess mock."""
    commands = [
        {"cmd": "printf pass", "cwd": "."},
        {"cmd": "printf boom; exit 3", "cwd": "."},
    ]

    script, _starts, _ends = _build_verify_script(commands, tmp_path, timeout=5)
    timeout_bin = tmp_path / "bin" / "timeout"
    timeout_bin.parent.mkdir()
    timeout_bin.write_text("#!/usr/bin/env bash\nshift\nexec \"$@\"\n")
    timeout_bin.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{timeout_bin.parent}:{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert "__SCHOOL_VERIFY_START_0__" in result.stdout
    assert "pass" in result.stdout
    assert "__SCHOOL_VERIFY_END_0__0" in result.stdout
    assert "__SCHOOL_VERIFY_START_1__" in result.stdout
    assert "boom" in result.stdout
    assert "__SCHOOL_VERIFY_END_1__3" in result.stdout


def test_run_verify_gate_uses_one_shell_for_multiple_commands(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "true", "test": "true"})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess([], 0, _successful_marker_output(2), "")

    with mock.patch("verify_gate.subprocess.run", side_effect=fake_run), \
         mock.patch("verify_gate._find_nix", return_value="nix"):
        res = run_verify_gate(tmp_path)

    assert res["passed"] is True
    assert res["ran"] == 2
    assert len(calls) == 1
    assert "npm run typecheck" in calls[0][0]
    assert "npm run test" in calls[0][0]
    assert res["telemetry"]["shell_starts"] == 1
    assert res["telemetry"]["commands"] == 2


def test_run_verify_gate_preserves_per_command_failure_evidence(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "true", "test": "false"})

    def fake_run(cmd, **kwargs):
        output = (
            "__SCHOOL_VERIFY_START_0__\n\npass\n"
            "__SCHOOL_VERIFY_END_0__0\n"
            "__SCHOOL_VERIFY_START_1__\n\nboom\n"
            "__SCHOOL_VERIFY_END_1__1\n"
        )
        return subprocess.CompletedProcess([], 0, output, "")

    with mock.patch("verify_gate.subprocess.run", side_effect=fake_run), \
         mock.patch("verify_gate._find_nix", return_value="nix"):
        res = run_verify_gate(tmp_path)

    assert res["ran"] == 2
    assert res["passed"] is False
    assert res["failures"] == [{"cmd": "npm run test", "exit": 1, "stderr": "boom"}]
    assert res["telemetry"]["shell_starts"] == 1


def test_run_verify_gate_rejects_markerless_success(tmp_path):
    """A zero shell exit is not proof when no per-command markers were emitted."""
    _write_pkg(tmp_path, ".", {"typecheck": "true"})
    with mock.patch("verify_gate.subprocess.run") as run, mock.patch(
        "verify_gate._find_nix", return_value="nix"
    ):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        res = run_verify_gate(tmp_path)

    assert res["passed"] is False
    assert res["ran"] == 1
    assert res["failures"][0]["cmd"] == "(verify_shell)"
    assert "markers" in res["failures"][0]["stderr"]


def test_run_verify_gate_rejects_partial_markers_zero_exit(tmp_path):
    """A shell that proves only some declared commands must fail closed."""
    _write_pkg(tmp_path, ".", {"typecheck": "true", "lint": "true"})
    with mock.patch("verify_gate.subprocess.run") as run, mock.patch(
        "verify_gate._find_nix", return_value="nix"
    ):
        run.return_value = subprocess.CompletedProcess(
            [], 0, _successful_marker_output(1), ""
        )
        res = run_verify_gate(tmp_path)

    assert res["passed"] is False
    assert res["failures"][0]["cmd"] == "(verify_shell)"
    assert "markers" in res["failures"][0]["stderr"]
    assert "npm run lint" in res["failures"][0]["stderr"]


def test_run_verify_gate_rejects_partial_markers_nonzero_exit(tmp_path):
    """Partial marker evidence preserves the shell error and missing command."""
    _write_pkg(tmp_path, ".", {"typecheck": "true", "lint": "true"})
    with mock.patch("verify_gate.subprocess.run") as run, mock.patch(
        "verify_gate._find_nix", return_value="nix"
    ):
        run.return_value = subprocess.CompletedProcess(
            [], 1, _successful_marker_output(1), "typecheck failed"
        )
        res = run_verify_gate(tmp_path)

    assert res["passed"] is False
    assert res["failures"][0]["exit"] == 1
    assert "markers" in res["failures"][0]["stderr"]
    assert "typecheck failed" in res["failures"][0]["stderr"]
    assert "npm run lint" in res["failures"][0]["stderr"]


def test_run_verify_gate_passes_when_all_zero(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "true"})
    with mock.patch("verify_gate.subprocess.run") as run, mock.patch(
        "verify_gate._find_nix", return_value="nix"
    ):
        run.return_value = subprocess.CompletedProcess([], 0, _successful_marker_output(1), "")
        res = run_verify_gate(tmp_path)
    assert res["passed"] is True
    assert res["ran"] == 1
    assert res["telemetry"]["shell_starts"] == 1
    assert res["telemetry"]["commands"] == 1
    assert res["telemetry"]["copied_bytes"] > 0


def test_run_verify_gate_fails_on_nonzero(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "false"})
    with mock.patch("verify_gate.subprocess.run") as run, mock.patch(
        "verify_gate._find_nix", return_value="nix"
    ):
        run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
        res = run_verify_gate(tmp_path)
    assert res["passed"] is False
    assert res["failures"][0]["exit"] == 1
    assert "boom" in res["failures"][0]["stderr"]


def test_run_verify_gate_times_out(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "sleep 99"})
    with mock.patch(
        "verify_gate.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)
    ), mock.patch("verify_gate._find_nix", return_value="nix"):
        res = run_verify_gate(tmp_path, timeout=1)
    assert res["passed"] is False
    assert "timed out" in res["failures"][0]["stderr"]


def test_no_commands_is_a_failure_not_a_pass(tmp_path):
    res = run_verify_gate(tmp_path)
    assert res["passed"] is False
    assert res["skipped"] is True
    assert "No typecheck" in res["failures"][0]["stderr"]


def test_skips_loudly_when_nix_missing(tmp_path):
    """The reusable gate soft-skips missing Nix, never faking a compile failure.

    The production school-loop has a separate hard Nix/verifyShell preflight;
    this test covers direct/manual library callers.
    """
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    with mock.patch("verify_gate._find_nix", return_value=None):
        res = run_verify_gate(tmp_path)
    assert res["passed"] is False
    assert res["skipped"] is True
    assert res["ran"] == 0
    assert "Nix not found" in res["failures"][0]["stderr"]


def test_strict_mode_escalates_missing_nix(tmp_path, monkeypatch):
    """VERIFY_GATE_STRICT=1 escalates an unrunnable reusable gate to FAIL."""
    monkeypatch.setenv("VERIFY_GATE_STRICT", "1")
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    with mock.patch("verify_gate._find_nix", return_value=None):
        res = run_verify_gate(tmp_path)
    assert res["passed"] is False
    assert res["skipped"] is False          # escalated — no longer a soft skip
    assert res["strict_escalated"] is True  # bridge treats this as a real failure
    assert res["ran"] == 0
    assert "VERIFY_GATE_STRICT" in res["failures"][0]["stderr"]


def test_strict_mode_escalates_no_commands(tmp_path, monkeypatch):
    """Strict mode also escalates the no-verify-commands verdict."""
    monkeypatch.setenv("VERIFY_GATE_STRICT", "1")
    res = run_verify_gate(tmp_path)  # empty dir → no commands discovered
    assert res["passed"] is False
    assert res["skipped"] is False
    assert res["strict_escalated"] is True


def test_repo_root_project_verify_yaml_auto_probed(tmp_path):
    """With no explicit manifest, the gate honors the repo's OWN
    project_verify.yaml over package.json inference."""
    (tmp_path / "project_verify.yaml").write_text(
        "verify:\n  - name: core-python-compile\n    cmd: python3 -m compileall -q *.py\n    cwd: .\n"
    )
    _write_pkg(tmp_path, ".", {"typecheck": "tsc --noEmit", "test": "vitest"})
    cmds = _discover_commands(tmp_path, None)
    assert len(cmds) == 1
    assert cmds[0]["name"] == "core-python-compile"
    assert "compileall" in cmds[0]["cmd"]
    assert not any("npm" in c["name"] for c in cmds)


def test_scratch_copy_skips_vcs_and_venv_noise(tmp_path):
    """The scratch copy must exclude .git/venv/node_modules bloat so the gate
    stays fast on large checkouts — verify commands never need that noise.

    Note: node_modules IS copied when pre-installed by clone_repo for TS
    projects (detected by presence of package.json + node_modules). This
    test uses a non-TS fixture (no package.json), so node_modules is ignored.
    """
    _write_pkg(tmp_path, ".", {"typecheck": "true"})
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "objects").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("boom")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib").write_text("boom")

    seen_cwds: list[str] = []

    def fake_run(cmd, **kwargs):
        seen_cwds.append(str(kwargs.get("cwd", "")))
        return subprocess.CompletedProcess([], 0, _successful_marker_output(1), "")

    with mock.patch("verify_gate.subprocess.run", side_effect=fake_run), \
         mock.patch("verify_gate._find_nix", return_value="/nix"), \
         mock.patch("verify_gate._flake_ref", return_value="."):
        res = run_verify_gate(tmp_path)
    assert res["passed"] is True
    assert seen_cwds, "verify command should have run in the scratch copy"
    assert not any("node_modules" in c for c in seen_cwds)
    assert not any(".git" in c for c in seen_cwds)
    assert not any(".venv" in c for c in seen_cwds)
    assert res["telemetry"]["shell_starts"] == 1
    assert res["telemetry"]["commands"] == 1
    assert res["telemetry"]["copied_bytes"] > 0


def test_scratch_copy_includes_node_modules_when_preinstalled(tmp_path):
    """For TypeScript projects (package.json present + node_modules pre-installed
    by clone_repo), the scratch copy MUST include node_modules so the hermetic
    gate can run typecheck/test/lint without network access."""
    _write_pkg(tmp_path, ".", {"typecheck": "tsc --noEmit", "test": "vitest"})
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".bin").mkdir()
    (tmp_path / "node_modules" / ".bin" / "tsc").write_text("#!/bin/sh\nexit 0")

    seen_cwds: list[str] = []

    def fake_run(cmd, **kwargs):
        seen_cwds.append(str(kwargs.get("cwd", "")))
        return subprocess.CompletedProcess([], 0, _successful_marker_output(2), "")

    with mock.patch("verify_gate.subprocess.run", side_effect=fake_run), \
         mock.patch("verify_gate._find_nix", return_value="/nix"), \
         mock.patch("verify_gate._flake_ref", return_value="."):
        res = run_verify_gate(tmp_path)
    assert res["passed"] is True
    assert seen_cwds, "verify command should have run in the scratch copy"
    # The work dir should be the scratch/repo directory
    assert any("repo" in c for c in seen_cwds if c)


def test_default_mode_not_affected_by_env_gap(tmp_path, monkeypatch):
    """Without the env var (or with it unset), behavior stays soft-skip."""
    monkeypatch.delenv("VERIFY_GATE_STRICT", raising=False)
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    with mock.patch("verify_gate._find_nix", return_value=None):
        res = run_verify_gate(tmp_path)
    assert res["skipped"] is True
    assert "strict_escalated" not in res


def test_flake_ref_prefers_directory(tmp_path):
    """The clean `nix develop` reference is the dir, not the flake.nix file."""
    (tmp_path / "flake.nix").write_text("{}\n")
    assert _flake_ref(tmp_path) == tmp_path
    # A file path resolves to its parent directory (kills nix's warning).
    assert _flake_ref(tmp_path / "flake.nix") == tmp_path
    # Unknown path keeps the legacy <path>/flake.nix shape so nix's error
    # message still names the file.
    assert _flake_ref(tmp_path / "missing.nix") == tmp_path / "missing.nix" / "flake.nix"
