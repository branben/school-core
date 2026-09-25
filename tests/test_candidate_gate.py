"""Behavioral tests for candidate-bound local verification evidence."""

import dataclasses
import subprocess
from pathlib import Path

import pytest

from candidate_manifest import CandidateStore, create_candidate
from candidate_gate import (
    GateEvidenceError,
    evidence_is_current,
    run_candidate_gate,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def candidate_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "candidate/gate")
    (repo / "README.md").write_text("candidate\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "candidate")
    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store, repo_path=repo, candidate_id="candidate-1", bead_id="bead-1",
        issue_number=1, repository="example/school-core", base_ref="main",
        branch="candidate/gate", owner="student-coder",
    )
    return repo, store, manifest


def test_gate_evidence_is_bound_to_candidate_and_exact_head(candidate_repo):
    repo, store, manifest = candidate_repo
    calls = []

    def runner(path, **kwargs):
        calls.append((path, kwargs))
        return {
            "passed": True,
            "ran": 2,
            "failures": [],
            "telemetry": {"shell_starts": 1, "commands": 2, "copied_bytes": 10},
        }

    evidence = run_candidate_gate(
        repo_path=repo, store=store, manifest=manifest, runner=runner,
        commands=[{"name": "test", "cmd": "pytest -q", "cwd": "."}, {"name": "lint", "cmd": "ruff check .", "cwd": "."}],
    )

    assert evidence.disposition == "current"
    assert evidence.candidate_id == manifest.candidate_id
    assert evidence.head_sha == manifest.head_sha
    assert evidence.toolchain["runner"] == "runner"
    assert [check["name"] for check in evidence.checks] == ["test", "lint"]
    assert all(check["exit"] == 0 for check in evidence.checks)
    assert evidence_is_current(evidence, manifest, repo_path=repo, store=store)
    assert calls == [(repo, {"project_verify": None, "flake_path": None, "timeout": 300})]


def test_changed_worktree_makes_gate_evidence_stale(candidate_repo):
    repo, store, manifest = candidate_repo
    evidence = run_candidate_gate(
        repo_path=repo, store=store, manifest=manifest,
        runner=lambda path, **kwargs: {"passed": True, "ran": 1, "failures": []},
        commands=[{"name": "test", "cmd": "pytest -q", "cwd": "."}],
    )
    (repo / "untracked.txt").write_text("changed\n")

    refreshed = evidence_is_current(evidence, manifest, repo_path=repo, store=store)
    assert refreshed is False


def test_failed_or_skipped_gate_is_not_current(candidate_repo):
    repo, store, manifest = candidate_repo
    failed = run_candidate_gate(
        repo_path=repo, store=store, manifest=manifest,
        runner=lambda path, **kwargs: {
            "passed": False, "ran": 1,
            "failures": [{"cmd": "pytest -q", "exit": 1, "stderr": "x" * 10000}],
        },
        commands=[{"name": "test", "cmd": "pytest -q", "cwd": "."}],
    )
    assert failed.disposition == "failed"
    assert len(failed.checks[0]["output"]) <= 4096
    assert not evidence_is_current(failed, manifest, repo_path=repo, store=store)

    skipped = run_candidate_gate(
        repo_path=repo, store=store, manifest=manifest,
        runner=lambda path, **kwargs: {
            "passed": False, "skipped": True, "ran": 0,
            "failures": [{"cmd": "(nix)", "exit": None, "stderr": "Nix missing"}],
        },
        commands=[{"name": "test", "cmd": "pytest -q", "cwd": "."}],
    )
    assert skipped.disposition == "skipped"
    assert not evidence_is_current(skipped, manifest, repo_path=repo, store=store)


def test_gate_rejects_unbound_manifest(candidate_repo):
    repo, store, manifest = candidate_repo
    unbound = dataclasses.replace(manifest, candidate_id="another-candidate")
    with pytest.raises(GateEvidenceError, match="candidate_id"):
        run_candidate_gate(
            repo_path=repo, store=store, manifest=unbound,
            runner=lambda path, **kwargs: {"passed": True, "ran": 0, "failures": []},
            commands=[],
        )
