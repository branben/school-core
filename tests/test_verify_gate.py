"""Tests for verify_gate.py — the hermetic execution stage.

These run without Nix (they mock the subprocess layer) so they're portable in
any CI. The real `nix develop` path is exercised manually / in integration.
"""

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from verify_gate import _discover_commands, run_verify_gate, ALLOWED_CONFIG_NAMES


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


def test_run_verify_gate_passes_when_all_zero(tmp_path):
    _write_pkg(tmp_path, ".", {"typecheck": "true"})
    with mock.patch("verify_gate.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        res = run_verify_gate(tmp_path)
    assert res["passed"] is True
    assert res["ran"] == 1


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
    assert "No typecheck" in res["failures"][0]["stderr"]
