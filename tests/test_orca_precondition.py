"""Tests for the School Loop Orca precondition probe (defect 2, SCH-11).

The regression being pinned: `orca status` returns 0 while `orca repo add`
returns {"code":"runtime_unavailable"}. Under `set -euo pipefail` that non-zero
rc killed the whole execute job, and steps 6-11 — including "Run bridge loop
(executes issues)" — were SKIPPED. 21 of the last 30 runs failed this way.

The probe must classify that condition as BLOCKED_ENV (explicitly named) rather
than letting it silently skip the only step that does real work.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "orca_precondition.py"
_spec = importlib.util.spec_from_file_location("orca_precondition", _MODULE_PATH)
orca_precondition = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orca_precondition)


class TestClassifyRepoAdd:
    def test_runtime_unavailable_is_blocked_env(self):
        """The exact live failure shape: rc=0 but code=runtime_unavailable."""
        code, msg = orca_precondition._classify_repo_add(
            0, json.dumps({"code": "runtime_unavailable"}), "",
        )
        assert code == orca_precondition.BLOCKED_ENV
        assert "runtime_unavailable" in msg

    def test_nonzero_rc_is_blocked_env(self):
        code, msg = orca_precondition._classify_repo_add(1, "", "connection refused")
        assert code == orca_precondition.BLOCKED_ENV
        assert "connection refused" in msg

    def test_success_is_ready(self):
        code, _ = orca_precondition._classify_repo_add(
            0, json.dumps({"id": "abc", "path": "/tmp/repo"}), "",
        )
        assert code == orca_precondition.READY

    def test_unparseable_stdout_with_rc0_is_ready(self):
        code, _ = orca_precondition._classify_repo_add(0, "not json", "")
        assert code == orca_precondition.READY


class TestMain:
    def test_reports_blocked_env_when_runtime_unavailable(self, monkeypatch, capsys):
        """The regression: a runtime_unavailable repo add must not be silent."""
        monkeypatch.setattr(orca_precondition.shutil, "which", lambda _: "/usr/local/bin/orca")

        def fake_run_orca(args, timeout=60):
            if args[0] == "status":
                return 0, json.dumps({"runtime": {"state": "ready"}}), ""
            if args[:2] == ["repo", "add"]:
                return 0, json.dumps({"code": "runtime_unavailable"}), ""
            return 0, "", ""

        monkeypatch.setattr(orca_precondition, "_run_orca", fake_run_orca)
        rc = orca_precondition.main(["/tmp/repo"])
        out = capsys.readouterr().out
        assert rc == orca_precondition.BLOCKED_ENV
        assert "::error::BLOCKED_ENV" in out
        assert "runtime_unavailable" in out

    def test_ready_when_repo_add_succeeds(self, monkeypatch, capsys):
        monkeypatch.setattr(orca_precondition.shutil, "which", lambda _: "/usr/local/bin/orca")

        def fake_run_orca(args, timeout=60):
            if args[:2] == ["repo", "add"]:
                return 0, json.dumps({"id": "r1", "path": "/tmp/repo"}), ""
            return 0, json.dumps({"runtime": {"state": "ready"}}), ""

        monkeypatch.setattr(orca_precondition, "_run_orca", fake_run_orca)
        rc = orca_precondition.main(["/tmp/repo"])
        assert rc == orca_precondition.READY
        assert "::error::" not in capsys.readouterr().out

    def test_missing_cli_is_blocked_env(self, monkeypatch, capsys):
        monkeypatch.setattr(orca_precondition.shutil, "which", lambda _: None)
        rc = orca_precondition.main(["/tmp/repo"])
        out = capsys.readouterr().out
        assert rc == orca_precondition.BLOCKED_ENV
        assert "BLOCKED_ENV" in out

    def test_repairs_runtime_when_status_down(self, monkeypatch):
        """`orca open` is attempted once when status fails, then repo add runs."""
        monkeypatch.setattr(orca_precondition.shutil, "which", lambda _: "/usr/local/bin/orca")
        calls = []

        def fake_run_orca(args, timeout=60):
            calls.append(args[0] if args[0] != "repo" else "repo-add")
            if args[0] == "status":
                return 1, "", "down"
            if args[0] == "open":
                return 0, "", ""
            if args[:2] == ["repo", "add"]:
                return 0, json.dumps({"id": "r1"}), ""
            return 0, "", ""

        monkeypatch.setattr(orca_precondition, "_run_orca", fake_run_orca)
        rc = orca_precondition.main(["/tmp/repo"])
        assert rc == orca_precondition.READY
        assert "open" in calls
        assert "repo-add" in calls

    def test_timeout_is_blocked_env(self, monkeypatch, capsys):
        import subprocess

        monkeypatch.setattr(orca_precondition.shutil, "which", lambda _: "/usr/local/bin/orca")

        def fake_run_orca(args, timeout=60):
            if args[:2] == ["repo", "add"]:
                raise subprocess.TimeoutExpired(cmd="orca", timeout=60)
            return 0, json.dumps({"runtime": {"state": "ready"}}), ""

        monkeypatch.setattr(orca_precondition, "_run_orca", fake_run_orca)
        rc = orca_precondition.main(["/tmp/repo"])
        assert rc == orca_precondition.BLOCKED_ENV
        assert "BLOCKED_ENV" in capsys.readouterr().out
