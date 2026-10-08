"""Tests for verify_gate.py — the hermetic execution stage.

These run without Nix (they mock the subprocess layer) so they're portable in
any CI. The real `nix develop` path is exercised manually / in integration.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import verify_gate

from verify_gate import (
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


@pytest.fixture
def shim_gate(monkeypatch, tmp_path):
    nix = tmp_path / "nix"
    nix.write_text('#!/bin/bash\nwhile [ "$1" != "--command" ]; do shift; done; shift; exec "$@"\n')
    sandbox = tmp_path / "sbx"
    sandbox.write_text('#!/bin/bash\nshift 2; exec "$@"\n')
    nix.chmod(0o755)
    sandbox.chmod(0o755)
    monkeypatch.setattr(verify_gate, "_find_nix", lambda: str(nix))
    monkeypatch.setattr(verify_gate, "sandbox_exec_path", lambda: str(sandbox))
    flake = tmp_path / "flake"
    flake.mkdir()
    (flake / "flake.nix").write_text("{}")
    return flake


@pytest.fixture
def shim_contract(shim_gate):
    """Return a function that freezes a verification contract for a repo."""
    def freeze(repo_path):
        return verify_gate.freeze_verification_contract(repo_path)
    return freeze


def _manifest(root, commands):
    root.mkdir(exist_ok=True)
    (root / "project_verify.yaml").write_text(json.dumps({"verify": commands}))


@pytest.mark.parametrize("cmd,code", [
    ("exit 0", 0), ("exit 1", 1),
    ("printf '__SCHOOL_VERIFY_START_0__\\n__SCHOOL_VERIFY_END_0__0 0.1\\n'; exit 1", 1),
    ("echo arbitrary-output; exit 0", 0),
    pytest.param("sleep 5", 124, marks=pytest.mark.skipif(
        shutil.which("timeout") is None,
        reason="timeout command not available on this platform",
    )),
    ("does-not-exist-school-check", 127),
])
def test_gate_uses_exit_status_not_candidate_output(tmp_path, shim_gate, shim_contract, cmd, code):
    root = tmp_path / "repo"
    _manifest(root, [{"name": "one", "cmd": cmd, "cwd": "."}])
    contract = shim_contract(root)
    result = run_verify_gate(root, flake_path=shim_gate, timeout=1, trusted_contract=contract)
    assert result["results"][0]["exit"] == code
    assert result["passed"] is (code == 0)
    assert result["results"][0]["duration_s"] >= 0


def test_gate_runs_every_command_independently(tmp_path, shim_gate, shim_contract):
    root = tmp_path / "repo"
    _manifest(root, [{"cmd": "exit 2", "cwd": "."}, {"cmd": "exit 0", "cwd": "."}])
    contract = shim_contract(root)
    result = run_verify_gate(root, flake_path=shim_gate, trusted_contract=contract)
    assert [r["exit"] for r in result["results"]] == [2, 0]
    assert not result["passed"]
    assert result["telemetry"]["shell_starts"] == 2


@pytest.mark.parametrize("cmd,cwd", [(True, "."), (42, "."), ("true", "../outside"), ("", ".")])
def test_bad_contract_is_a_failure_not_an_exception(tmp_path, cmd, cwd, shim_contract):
    _manifest(tmp_path, [{"cmd": cmd, "cwd": cwd}])
    contract = shim_contract(tmp_path)
    result = run_verify_gate(tmp_path, trusted_contract=contract)
    assert not result["passed"]
    assert result["strict_escalated"]
    assert not result["skipped"]


def test_response_text_cannot_skip_language_checks(tmp_path, shim_gate, shim_contract):
    root = tmp_path / "repo"
    _manifest(root, [{"cmd": "exit 3", "cwd": ".", "languages": ["python"]}])
    contract = shim_contract(root)
    result = run_verify_gate(root, flake_path=shim_gate, diff_text="```markdown\nonly docs\n```", trusted_contract=contract)
    assert result["ran"] == 1
    assert not result["passed"]


def test_candidate_cannot_rewrite_frozen_manifest(tmp_path, shim_gate):
    root = tmp_path / "repo"
    _manifest(root, [{"cmd": "exit 1", "cwd": "."}])
    contract = verify_gate.freeze_verification_contract(root)
    _manifest(root, [{"cmd": "true", "cwd": "."}])
    result = run_verify_gate(root, flake_path=shim_gate, trusted_contract=contract)
    assert not result["passed"] and result["strict_escalated"]
    assert "policy files" in result["failures"][0]["stderr"]


def test_candidate_cannot_rewrite_package_script(tmp_path, shim_gate):
    root = tmp_path / "repo"
    _manifest(root, [{"cmd": "npm test", "cwd": "."}])
    _write_pkg(root, ".", {"test": "exit 1"})
    contract = verify_gate.freeze_verification_contract(root)
    _write_pkg(root, ".", {"test": "true"})
    result = run_verify_gate(root, flake_path=shim_gate, trusted_contract=contract)
    assert not result["passed"] and result["strict_escalated"]


def test_unchanged_contract_runs_candidate_source(tmp_path, shim_gate):
    root = tmp_path / "repo"
    _manifest(root, [{"cmd": "bash check.sh", "cwd": "."}])
    (root / "check.sh").write_text("exit 0")
    contract = verify_gate.freeze_verification_contract(root)
    (root / "check.sh").write_text("exit 4")
    result = run_verify_gate(root, flake_path=shim_gate, trusted_contract=contract)
    assert result["results"][0]["exit"] == 4
    assert not result["passed"]


def test_no_commands_is_a_failure_not_a_pass(tmp_path, shim_contract):
    # Create a repo with a manifest so the contract has commands
    _manifest(tmp_path, [{"name": "check", "cmd": "exit 0", "cwd": "."}])
    contract = shim_contract(tmp_path)
    # Now remove the manifest so no commands are discovered
    (tmp_path / "project_verify.yaml").unlink()
    res = run_verify_gate(tmp_path, trusted_contract=contract)
    assert res["passed"] is False
    # The gate detects the manifest was removed (hash mismatch) and escalates
    assert res["strict_escalated"] is True
    assert res["skipped"] is False


def test_skips_loudly_when_nix_missing(tmp_path, shim_contract):
    """The reusable gate soft-skips missing Nix, never faking a compile failure.

    The production school-loop has a separate hard Nix/verifyShell preflight;
    this test covers direct/manual library callers.
    """
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    contract = shim_contract(tmp_path)
    with mock.patch("verify_gate._find_nix", return_value=None):
        res = run_verify_gate(tmp_path, trusted_contract=contract)
    assert res["passed"] is False
    assert res["skipped"] is True
    assert res["ran"] == 0
    assert "Nix not found" in res["failures"][0]["stderr"]


def test_strict_mode_escalates_missing_nix(tmp_path, monkeypatch, shim_contract):
    """VERIFY_GATE_STRICT=1 escalates an unrunnable reusable gate to FAIL."""
    monkeypatch.setenv("VERIFY_GATE_STRICT", "1")
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    contract = shim_contract(tmp_path)
    with mock.patch("verify_gate._find_nix", return_value=None):
        res = run_verify_gate(tmp_path, trusted_contract=contract)
    assert res["passed"] is False
    assert res["skipped"] is False          # escalated — no longer a soft skip
    assert res["strict_escalated"] is True  # bridge treats this as a real failure
    assert res["ran"] == 0
    assert "VERIFY_GATE_STRICT" in res["failures"][0]["stderr"]


def test_strict_mode_escalates_no_commands(tmp_path, monkeypatch, shim_contract):
    """Strict mode also escalates the no-verify-commands verdict."""
    monkeypatch.setenv("VERIFY_GATE_STRICT", "1")
    # Create a repo with a manifest so the contract has commands
    _manifest(tmp_path, [{"name": "check", "cmd": "exit 0", "cwd": "."}])
    contract = shim_contract(tmp_path)
    # Now remove the manifest so no commands are discovered
    (tmp_path / "project_verify.yaml").unlink()
    res = run_verify_gate(tmp_path, trusted_contract=contract)
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


def test_scratch_copy_skips_vcs_and_venv_noise(tmp_path, shim_contract):
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
    (tmp_path / ".env").write_text("API_KEY=private-canary\n")
    (tmp_path / ".npmrc").write_text("//registry.example/:_authToken=private-canary\n")
    contract = shim_contract(tmp_path)

    seen_cwds: list[str] = []
    sensitive_files_present: list[bool] = []

    def fake_run(cmd, **kwargs):
        work = Path(kwargs.get("cwd", ""))
        seen_cwds.append(str(work))
        sensitive_files_present.append((work / ".env").exists() or (work / ".npmrc").exists())
        return subprocess.CompletedProcess([], 0, _successful_marker_output(1), "")

    with mock.patch("verify_gate.subprocess.run", side_effect=fake_run), \
         mock.patch("verify_gate._find_nix", return_value="/nix"), \
         mock.patch("verify_gate.sandbox_exec_path", return_value="/usr/bin/sandbox-exec"), \
         mock.patch("verify_gate._flake_ref", return_value="."):
        res = run_verify_gate(tmp_path, trusted_contract=contract)
    assert res["passed"] is True
    assert seen_cwds, "verify command should have run in the scratch copy"
    assert not any("node_modules" in c for c in seen_cwds)
    assert not any(".git" in c for c in seen_cwds)
    assert not any(".venv" in c for c in seen_cwds)
    assert sensitive_files_present == [False], "host credentials must not be copied into the untrusted workspace"
    assert ".env" in verify_gate._VERIFY_COPY_IGNORE("repo", [".env"])
    assert ".npmrc" in verify_gate._VERIFY_COPY_IGNORE("repo", [".npmrc"])
    assert res["telemetry"]["shell_starts"] == 1
    assert res["telemetry"]["commands"] == 1
    assert res["telemetry"]["copied_bytes"] > 0


def test_scratch_copy_includes_node_modules_when_preinstalled(tmp_path, shim_contract):
    """For TypeScript projects (package.json present + node_modules pre-installed
    by clone_repo), the scratch copy MUST include node_modules so the hermetic
    gate can run typecheck/test/lint without network access."""
    _write_pkg(tmp_path, ".", {"typecheck": "tsc --noEmit", "test": "vitest"})
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".bin").mkdir()
    (tmp_path / "node_modules" / ".bin" / "tsc").write_text("#!/bin/sh\nexit 0")
    contract = shim_contract(tmp_path)

    seen_cwds: list[str] = []

    def fake_run(cmd, **kwargs):
        seen_cwds.append(str(kwargs.get("cwd", "")))
        return subprocess.CompletedProcess([], 0, _successful_marker_output(2), "")

    with mock.patch("verify_gate.subprocess.run", side_effect=fake_run), \
         mock.patch("verify_gate._find_nix", return_value="/nix"), \
         mock.patch("verify_gate.sandbox_exec_path", return_value="/usr/bin/sandbox-exec"), \
         mock.patch("verify_gate._flake_ref", return_value="."):
        res = run_verify_gate(tmp_path, trusted_contract=contract)
    assert res["passed"] is True
    assert seen_cwds, "verify command should have run in the scratch copy"
    # The work dir should be the scratch/repo directory
    assert any("repo" in c for c in seen_cwds if c)


def test_default_mode_not_affected_by_env_gap(tmp_path, monkeypatch, shim_contract):
    """Without the env var (or with it unset), behavior stays soft-skip."""
    monkeypatch.delenv("VERIFY_GATE_STRICT", raising=False)
    _write_pkg(tmp_path, ".", {"typecheck": "tsc"})
    contract = shim_contract(tmp_path)
    with mock.patch("verify_gate._find_nix", return_value=None):
        res = run_verify_gate(tmp_path, trusted_contract=contract)
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


def test_scratch_base_env_overridable_and_portable(tmp_path, monkeypatch):
    """The scratch base must not be a hardcoded absolute home path.

    verify_gate.py:330 hardcoded /Users/brandonbennett/tmp (added 27623e1,
    Aug 30, for local disk space). On any other machine — including the
    Linux GitHub runners — mkdir('/Users') raises PermissionError and ALL
    run_verify_gate tests fail (10 failed on main CI runs since Sep 23).

    Contract: SCHOOL_VERIFY_SCRATCH overrides the base; the default is the
    platform temp dir, which exists everywhere.
    """
    import tempfile as _tempfile

    from verify_gate import scratch_base_path

    # Default: portable, exists on every platform (Linux runners included)
    default = scratch_base_path()
    assert str(default) != "/Users/brandonbennett/tmp"
    assert default.exists(), "default scratch base must already exist"

    # Env override wins (operator with a big local disk keeps their path)
    monkeypatch.setenv("SCHOOL_VERIFY_SCRATCH", str(tmp_path / "custom"))
    assert scratch_base_path() == Path(tmp_path / "custom")
