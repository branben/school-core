"""In-process coverage tests for src/core/recovery.py.

The existing test_recovery.py drives recovery mostly through subprocess CLI
invocations, which single-process --cov does not attribute to the module.
These tests call the check functions and helpers directly so `--cov=src`
measures them.
"""

import json
import sys
from pathlib import Path

import pytest

import src.core.recovery as rec


# ---------------------------------------------------------------- Check

def test_check_validates_status():
    rec.Check("python", "ok")
    with pytest.raises(ValueError):
        rec.Check("python", "bogus")


def test_check_to_dict_carries_fields():
    c = rec.Check("git", "missing", "nope", "install git", required=True)
    d = c.to_dict()
    assert d["name"] == "git" and d["status"] == "missing"
    assert d["remedy"] == "install git" and d["required"] is True


# ---------------------------------------------------------------- Report

def test_report_aggregates_blocking_and_ok(tmp_path):
    report = rec.Report("doctor", str(tmp_path))
    report.add(rec.Check("ok-check", "ok"))
    report.add(rec.Check("adv", "missing", required=False))
    report.add(rec.Check("req", "blocked", "boom"))
    assert [c.name for c in report.blocking] == ["req"]
    assert report.ok is False
    report2 = rec.Report("doctor", str(tmp_path))
    report2.add(rec.Check("ok-check", "ok"))
    assert report2.ok is True


def test_report_to_dict_json_text(tmp_path):
    report = rec.Report("doctor", str(tmp_path))
    report.add(rec.Check("a", "ok", "fine"))
    data = report.to_dict()
    assert data["command"] == "doctor"
    assert data["ok"] is True
    parsed = json.loads(report.to_json())
    assert parsed["checks"][0]["name"] == "a"
    text = report.to_text()
    assert "result: OK" in text
    report.add(rec.Check("blk", "missing", "nope", "fix"))
    text2 = report.to_text()
    assert "result: BLOCKED" in text2 and "remedy: fix" in text2


def test_report_write_creates_file(tmp_path):
    report = rec.Report("bootstrap", str(tmp_path))
    report.add(rec.Check("a", "ok"))
    dest = rec._report_path(tmp_path, "bootstrap")
    report.write(dest)
    assert dest.is_file()
    assert json.loads(dest.read_text())["command"] == "bootstrap"


# ---------------------------------------------------------------- helpers

def test_tail_joins_last_lines():
    assert rec._tail("  \n one\ntwo\nthree#ignored\n  four\n") == "two | three#ignored | four"
    assert rec._tail("") == ""


def test_host_system_shape():
    value = rec._host_system()
    assert "-" in value and value.split("-")[-1] in ("linux", "darwin", sys.platform)


def test_path_helpers(tmp_path):
    assert rec._venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"
    assert rec._context_dest(tmp_path) == tmp_path / "data" / "context"
    assert rec._report_path(tmp_path, "doctor") == tmp_path / "data" / "recovery" / "doctor-report.json"


def test_run_missing_command():
    rc, _, err = rec._run(["/nonexistent/binary-xyz"])
    assert rc == 127 and "not found" in err


def test_run_timeout():
    rc, _, err = rec._run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)
    assert rc == 124 and "timed out" in err


# ---------------------------------------------------------------- individual checks (cheap)

def test_check_python_ok():
    c = rec.check_python()
    assert c.status == "ok" and "CPython" in c.detail


def test_check_git_ok(monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (0, "git version 2.40", ""))
    assert rec.check_git().status == "ok"


def test_check_git_missing(monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (127, "", "git: not found"))
    c = rec.check_git()
    assert c.status == "missing" and "install git" in c.remedy


def test_check_venv_skip():
    c = rec.check_venv(Path("/tmp/nope"), skip=True)
    assert c.status == "unknown" and "skipped" in c.detail


def test_check_venv_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (1, "", "no venv"))
    c = rec.check_venv(tmp_path)
    assert c.status == "missing" and "bootstrap" in c.remedy


def test_check_deps_skip():
    c = rec.check_deps(Path("/tmp/nope"), skip=True)
    assert c.status == "unknown"


def test_check_deps_no_requirements(tmp_path, monkeypatch):
    # no requirements.txt -> ok regardless of venv
    (tmp_path / ".venv").mkdir(parents=True)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    c = rec.check_deps(tmp_path)
    assert c.status == "ok"


def test_check_deps_unknown_without_venv(tmp_path):
    c = rec.check_deps(tmp_path)
    assert c.status == "unknown"


def test_check_nix_ok(monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (0, "nix (Nix) 2.20", ""))
    assert rec.check_nix(Path("/tmp")).status == "ok"


def test_check_nix_missing(monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (127, "", "nix: not found"))
    c = rec.check_nix(Path("/tmp"))
    assert c.status == "missing" and "install" in c.remedy


def test_check_flake_platform_no_flake(tmp_path):
    assert rec.check_flake_platform(tmp_path).status == "unknown"


def test_check_flake_platform_match(tmp_path, monkeypatch):
    (tmp_path / "flake.nix").write_text('{{ system = "{}"; }}'.format(rec._host_system()))
    assert rec.check_flake_platform(tmp_path).status == "ok"


def test_check_flake_platform_mismatch(tmp_path):
    (tmp_path / "flake.nix").write_text('{ system = "nonsense-arch"; }')
    c = rec.check_flake_platform(tmp_path)
    assert c.status == "not_configured" and "adapt" in c.remedy


# ---------------------------------------------------------------- context / souls / secrets

def test_check_context_missing_manifest(tmp_path):
    c = rec.check_context(tmp_path)
    assert c.status == "missing"


def test_check_context_corrupt_manifest(tmp_path):
    (tmp_path / "envit.json").write_text("{ not json")
    (tmp_path / "envit.lock.json").write_text("{}")
    c = rec.check_context(tmp_path)
    assert c.status == "blocked" and "invalid JSON" in c.detail


def test_check_context_empty_repos(tmp_path):
    (tmp_path / "envit.json").write_text('{"repos": {}}')
    (tmp_path / "envit.lock.json").write_text('{"repos": {}}')
    c = rec.check_context(tmp_path)
    assert c.status == "blocked" and "no pinned repos" in c.detail


def test_check_context_materialized(tmp_path):
    (tmp_path / "envit.json").write_text('{"repos": {"a": {}}}')
    (tmp_path / "envit.lock.json").write_text('{"repos": {"a": {}}}')
    skills = tmp_path / "data" / "context" / "skills" / "foo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# foo")
    c = rec.check_context(tmp_path)
    assert c.status == "ok" and "materialized" in c.detail


def test_check_souls_missing(tmp_path):
    c = rec.check_souls(tmp_path)
    assert c.status == "missing"


def test_check_souls_empty(tmp_path):
    profiles = tmp_path / "config" / "profiles" / "demo"
    profiles.mkdir(parents=True)
    (profiles / "SOUL.md").write_text("   ")
    c = rec.check_souls(tmp_path)
    assert c.status == "blocked" and "empty" in c.detail


def test_check_souls_ok(tmp_path):
    profiles = tmp_path / "config" / "profiles" / "demo"
    profiles.mkdir(parents=True)
    (profiles / "SOUL.md").write_text("You are demo.")
    c = rec.check_souls(tmp_path)
    assert c.status == "ok"


def test_env_names_reads_dotenv(tmp_path):
    (tmp_path / ".env").write_text("FOO=1\n# comment\nBAR=2\n")
    names = rec._env_names(tmp_path, {"BAZ": "x"})
    assert {"FOO", "BAR", "BAZ"} <= names


def test_check_secrets_missing_example(tmp_path):
    c = rec.check_secrets(tmp_path, {})
    assert c.status == "missing" and ".env.example" in c.detail


def test_check_secrets_not_configured(tmp_path):
    (tmp_path / ".env.example").write_text("REQUIRED_KEY=\n# OPTIONAL_KEY=\n")
    c = rec.check_secrets(tmp_path, {})
    assert c.status == "not_configured" and "REQUIRED_KEY" in c.detail


def test_check_secrets_ok_with_optional(tmp_path):
    (tmp_path / ".env.example").write_text("REQ=\n# OPT=\n")
    c = rec.check_secrets(tmp_path, {"REQ": "1", "OPT": "2"})
    assert c.status == "ok" and "optional set: 1/1" in c.detail


# ---------------------------------------------------------------- services / target / state

def test_env_values_reads_dotenv(tmp_path):
    (tmp_path / ".env").write_text('URL="http://x"\n')
    values = rec._env_values(tmp_path, {"EXIST": "1"})
    assert values["URL"] == "http://x" and values["EXIST"] == "1"


def test_check_services_not_configured(tmp_path):
    c = rec.check_services(tmp_path, {})
    assert c.status == "not_configured"


def test_check_services_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_probe_service", lambda *a, **k: (False, "service unavailable"))
    c = rec.check_services(tmp_path, {"OMNIROUTE_BASE": "http://127.0.0.1:1"})
    assert c.status == "blocked"


def test_check_services_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_env_values", lambda *a, **k: {"OMNIROUTE_BASE": "http://x"})
    monkeypatch.setattr(rec, "_probe_service", lambda *a, **k: (True, "HTTP 200"))
    c = rec.check_services(tmp_path, {"OMNIROUTE_BASE": "http://x"})
    assert c.status == "ok" and "reachable" in c.detail


def test_probe_service_invalid_url():
    ok, note = rec._probe_service(":::not-a-url:::")
    assert ok is False and "invalid" in note


def test_check_target_env_override(tmp_path):
    c = rec.check_target(tmp_path, {"AGENT_SCHOOL_REPO": "branben/school-core"})
    assert c.status == "ok" and "from environment" in c.detail


def test_check_target_no_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (128, "", "no remote"))
    c = rec.check_target(tmp_path, {})
    assert c.status == "not_configured"


def test_check_target_github_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_run", lambda *a, **k: (0, "git@github.com:branben/school-core.git", ""))
    c = rec.check_target(tmp_path, {})
    assert c.status == "ok" and "branben/school-core" in c.detail


def test_check_state_ok(tmp_path):
    c = rec.check_state(tmp_path)
    assert c.status == "ok" and "writable" in c.detail


def test_check_state_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_raise(OSError("nope"))))
    c = rec.check_state(tmp_path)
    assert c.status == "blocked"


def _raise(exc):
    raise exc


# ---------------------------------------------------------------- beads helpers

def test_beads_remote_nested(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    (beads / "config.yaml").write_text("sync:\n  remote: git@example.com:org/repo.git\n")
    assert rec._beads_remote(tmp_path) == "git@example.com:org/repo.git"


def test_beads_remote_missing(tmp_path):
    assert rec._beads_remote(tmp_path) is None


def test_beads_database_embedded(tmp_path):
    db = rec._beads_database(tmp_path, {"dolt_mode": "embedded", "dolt_database": "beads"})
    assert db == tmp_path / ".beads" / "embeddeddolt" / "beads"


def test_beads_database_plain(tmp_path):
    db = rec._beads_database(tmp_path, {})
    assert db == tmp_path / ".beads" / "dolt" / "beads"


def test_beads_state_missing(tmp_path):
    status, _ = rec._beads_state(tmp_path, {})
    assert status == "missing"


def test_beads_state_blocked_backend(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    status, _ = rec._beads_state(tmp_path, {"backend": "sqlite"})
    assert status == "blocked"


def test_beads_state_missing_database(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    status, _ = rec._beads_state(tmp_path, {"backend": "dolt", "dolt_mode": "embedded"})
    assert status == "missing"


def test_beads_state_corrupt_repo_state(tmp_path):
    db = tmp_path / ".beads" / "embeddeddolt" / "beads"
    (db / ".dolt").mkdir(parents=True)
    (db / ".dolt" / "repo_state.json").write_text("{bad json")
    status, detail = rec._beads_state(tmp_path, {"backend": "dolt", "dolt_mode": "embedded"})
    assert status == "blocked" and "corrupt" in detail


# ---------------------------------------------------------------- run_* entry points

def test_run_doctor_writes_report(tmp_path):
    report = rec.run_doctor(tmp_path, env={"AGENT_SCHOOL_REPO": "branben/school-core"})
    assert report.command == "doctor"
    dest = rec._report_path(tmp_path, "doctor")
    assert dest.is_file()


def test_run_bootstrap_writes_report(tmp_path):
    report = rec.run_bootstrap(tmp_path, env={}, venv=False, deps=False)
    assert report.command == "bootstrap"
    assert rec._report_path(tmp_path, "bootstrap").is_file()