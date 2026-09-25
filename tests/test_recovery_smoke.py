"""End-to-end smoke tests for `recovery.py smoke` — the clean-device recovery
smoke flow (school-core-zbs.2): candidate -> declared local verification ->
exact identity/evidence -> durable state, against real temporary Git target
repositories.

Real boundaries only: real git repos and commits, real subprocess execution of
the target's DECLARED verification commands (with an observable side effect
proving they ran), real SQLite state journal inspected after process exit,
fail-closed behavior on verification failure / missing declarations / bad
targets. No GitHub contact, no Beads mutation.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "recovery.py"

_SCRUBBED_KEYS = (
    "REQUIRED_KEY", "OPTIONAL_KEY",
    "OMNIROUTE_BASE", "OMNIROUTE_API_KEY",
    "AGENT_SCHOOL_REPO", "SCHOOL_REPO", "SMOKE_MARKER",
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def run_cli(*args, env=None, timeout=55):
    full_env = dict(os.environ)
    for key in _SCRUBBED_KEYS:
        full_env.pop(key, None)
    for key, value in (env or {}).items():
        if value is None:
            full_env.pop(key, None)
        else:
            full_env[key] = value
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, timeout=timeout, env=full_env,
    )


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def target_repo(tmp_path):
    """A real temporary target Git repository with declared harmless verify
    commands. `verify.sh` writes to $SMOKE_MARKER — the observable side effect
    proving the declared command genuinely executed."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "smoke@example.com")
    _git(repo, "config", "user.name", "Smoke Test")
    (repo / "README.md").write_text("target repo\n")
    (repo / "verify.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "python3 -c \"import os, pathlib; "
        "pathlib.Path(os.environ['SMOKE_MARKER']).write_text('ran'); "
        "print('verify ok')\"\n"
    )
    (repo / "project_verify.yaml").write_text(
        "verify:\n"
        "  - name: smoke-verify\n"
        "    cmd: bash verify.sh\n"
        "    cwd: .\n"
        "  - name: whitespace-check\n"
        "    cmd: git diff --check\n"
        "    cwd: .\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _report(evidence_dir):
    path = evidence_dir / "report.json"
    assert path.is_file(), f"expected durable report at {path}"
    return json.loads(path.read_text())


def _by_name(report):
    return {c["name"]: c for c in report["checks"]}


# --- happy path --------------------------------------------------------------

def test_smoke_end_to_end_against_target_repo(tmp_path, target_repo):
    evidence = tmp_path / "evidence"
    marker = tmp_path / "verify-marker.txt"
    result = run_cli(
        "smoke", "--target", str(target_repo), "--evidence-dir", str(evidence),
        env={"SMOKE_MARKER": str(marker)},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # The declared verification command genuinely executed.
    assert marker.read_text() == "ran"

    report = _report(evidence)
    assert report["command"] == "smoke"
    assert report["blocking"] == []
    names = _by_name(report)
    for check in ("target", "candidate", "verification", "identity", "journal", "evidence"):
        assert names[check]["status"] == "ok", names[check]
        assert names[check]["required"] is True

    # Candidate identity is real Git state, not output text.
    candidate = json.loads((evidence / "candidate.json").read_text())
    assert _SHA_RE.fullmatch(candidate["base_sha"])
    assert _SHA_RE.fullmatch(candidate["head_sha"])
    assert _SHA_RE.fullmatch(candidate["diff_digest"]) or len(candidate["diff_digest"]) == 64
    assert candidate["dirty_tree"] is False
    assert Path(candidate["worktree"]).resolve() == target_repo.resolve()
    assert _git(target_repo, "branch", "--show-current") == candidate["branch"]
    assert _git(target_repo, "rev-parse", "HEAD") == candidate["head_sha"]
    assert _git(target_repo, "rev-parse", "main") == candidate["base_sha"]

    # Verification evidence is bound to the exact head.
    gate = json.loads((evidence / "gate-evidence.json").read_text())
    assert gate["disposition"] == "current"
    assert gate["head_sha"] == candidate["head_sha"]
    assert gate["candidate_id"] == candidate["candidate_id"]
    by_cmd = {c["name"]: c for c in gate["checks"]}
    assert by_cmd["smoke-verify"]["exit"] == 0
    assert by_cmd["whitespace-check"]["exit"] == 0

    # Durable state journal is inspectable after process exit.
    from state_journal import StateJournal
    ops = StateJournal(evidence / "state.sqlite3").operations()
    assert len(ops) == 1
    assert ops[0].kind == "recovery-smoke"
    assert ops[0].candidate_id == candidate["candidate_id"]
    assert ops[0].head_sha == candidate["head_sha"]
    assert ops[0].status == "confirmed"
    assert [e["kind"] for e in ops[0].events] == ["gate", "confirmed"]

    # The report points at its own artifacts (durable evidence map).
    assert Path(report["artifacts"]["candidate"]).is_file()
    assert Path(report["artifacts"]["gate_evidence"]).is_file()
    assert Path(report["artifacts"]["journal"]).is_file()
    assert "report.json" in result.stdout


def test_smoke_creates_disposable_target_when_none_given(tmp_path):
    evidence = tmp_path / "evidence"
    result = run_cli("smoke", "--evidence-dir", str(evidence))
    assert result.returncode == 0, result.stdout + result.stderr
    report = _report(evidence)
    assert report["blocking"] == []
    candidate = json.loads((evidence / "candidate.json").read_text())
    target = Path(candidate["worktree"])
    # The disposable target survives process exit and is real Git state.
    assert target.is_dir()
    assert (target / ".git").is_dir()
    assert _git(target, "rev-parse", "HEAD") == candidate["head_sha"]
    # Its declared verify command ran with a durable side effect.
    assert (evidence / "verify-marker.txt").read_text() == "ran"


# --- fail-closed paths -------------------------------------------------------

def test_smoke_fails_closed_when_verification_fails(tmp_path, target_repo):
    (target_repo / "project_verify.yaml").write_text(
        "verify:\n"
        "  - name: smoke-verify-fail\n"
        "    cmd: python3 -c \"import sys; print('boom', file=sys.stderr); sys.exit(3)\"\n"
        "    cwd: .\n"
    )
    _git(target_repo, "add", ".")
    _git(target_repo, "commit", "-qm", "failing verify")
    evidence = tmp_path / "evidence"
    result = run_cli(
        "smoke", "--target", str(target_repo), "--evidence-dir", str(evidence),
        env={"SMOKE_MARKER": str(tmp_path / "marker.txt")},
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    report = _report(evidence)
    verification = _by_name(report)["verification"]
    assert verification["status"] == "blocked"
    assert "smoke-verify-fail" in verification["detail"]
    gate = json.loads((evidence / "gate-evidence.json").read_text())
    assert gate["disposition"] == "failed"
    # Failure is durably journaled; candidate identity still persisted.
    from state_journal import StateJournal
    ops = StateJournal(evidence / "state.sqlite3").operations()
    assert ops[0].status == "failed"
    assert [e["kind"] for e in ops[0].events] == ["gate", "failed"]
    assert (evidence / "candidate.json").is_file()


def test_smoke_fails_closed_without_declared_verification(tmp_path, target_repo):
    (target_repo / "project_verify.yaml").unlink()
    _git(target_repo, "add", "-A")
    _git(target_repo, "commit", "-qm", "drop verify manifest")
    evidence = tmp_path / "evidence"
    result = run_cli(
        "smoke", "--target", str(target_repo), "--evidence-dir", str(evidence),
        env={"SMOKE_MARKER": str(tmp_path / "marker.txt")},
    )
    assert result.returncode != 0
    verification = _by_name(_report(evidence))["verification"]
    assert verification["status"] == "missing"
    assert "declar" in verification["detail"].lower()


def test_smoke_rejects_non_git_target(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    evidence = tmp_path / "evidence"
    result = run_cli(
        "smoke", "--target", str(plain), "--evidence-dir", str(evidence),
        env={"SMOKE_MARKER": str(tmp_path / "marker.txt")},
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    report = _report(evidence)
    assert _by_name(report)["target"]["status"] != "ok"


def test_smoke_rejects_dirty_target_tree(tmp_path, target_repo):
    (target_repo / "UNCOMMITTED.txt").write_text("dirty\n")
    evidence = tmp_path / "evidence"
    result = run_cli(
        "smoke", "--target", str(target_repo), "--evidence-dir", str(evidence),
        env={"SMOKE_MARKER": str(tmp_path / "marker.txt")},
    )
    assert result.returncode != 0
    report = _report(evidence)
    assert _by_name(report)["target"]["status"] == "blocked"
    assert "dirty" in _by_name(report)["target"]["detail"]
