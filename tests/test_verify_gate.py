"""Tests for verify_gate.py — the hermetic execution stage.

These run without Nix (they mock the subprocess layer) so they're portable in
any CI. The real `nix develop` path is exercised manually / in integration.
"""

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from verify_gate import (
    _discover_commands,
    _flake_ref,
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


def test_run_verify_gate_passes_when_all_zero(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "true"})
    with mock.patch("verify_gate.subprocess.run") as run, mock.patch(
        "verify_gate._find_nix", return_value="nix"
    ):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        res = run_verify_gate(tmp_path)
    assert res["passed"] is True
    assert res["ran"] == 1


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


def test_run_verify_gate_fails_on_nonzero(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "false"})
    with mock.patch("verify_gate.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
        res = run_verify_gate(tmp_path)
    assert res["passed"] is False
    assert res["failures"][0]["exit"] == 1
    assert "boom" in res["failures"][0]["stderr"]


def test_run_verify_gate_times_out(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "sleep 99"})
    with mock.patch("verify_gate.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
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
