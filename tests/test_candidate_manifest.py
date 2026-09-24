"""Behavioral tests for immutable candidate identity and branch ownership."""

import dataclasses
import subprocess
from pathlib import Path

import pytest

from candidate_manifest import CandidateManifestError, CandidateStore, create_candidate


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base")
    return path


def test_create_candidate_round_trips_and_records_exact_git_identity(repo, tmp_path):
    _git(repo, "checkout", "-b", "candidate/one")
    (repo / "README.md").write_text("candidate\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "candidate")

    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store,
        repo_path=repo,
        candidate_id="school-core-4be-1",
        bead_id="school-core-4be",
        issue_number=4,
        repository="example/school-core",
        base_ref="main",
        branch="candidate/one",
        owner="student-coder",
    )

    loaded = store.get(manifest.candidate_id)
    assert loaded == manifest
    assert loaded.base_sha == _git(repo, "rev-parse", "main")
    assert loaded.head_sha == _git(repo, "rev-parse", "HEAD")
    assert loaded.dirty_tree is False
    assert store.branch_owner("candidate/one") == manifest.candidate_id
    assert store.validate(repo, manifest).status == "current"


def test_create_candidate_rejects_a_branch_owned_by_another_candidate(repo, tmp_path):
    _git(repo, "checkout", "-b", "candidate/shared")
    store = CandidateStore(tmp_path / "candidates.json")
    first = create_candidate(
        store=store,
        repo_path=repo,
        candidate_id="school-core-4be-1",
        bead_id="school-core-4be",
        issue_number=4,
        repository="example/school-core",
        base_ref="main",
        branch="candidate/shared",
        owner="student-coder",
    )
    assert first.candidate_id == "school-core-4be-1"

    with pytest.raises(CandidateManifestError, match="branch.*owned"):
        create_candidate(
            store=store,
            repo_path=repo,
            candidate_id="school-core-4be-2",
            bead_id="school-core-4be",
            issue_number=4,
            repository="example/school-core",
            base_ref="main",
            branch="candidate/shared",
            owner="other-coder",
        )


def test_validate_marks_changed_head_or_dirty_tree_stale(repo, tmp_path):
    _git(repo, "checkout", "-b", "candidate/stale")
    (repo / "README.md").write_text("candidate\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "candidate")
    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store,
        repo_path=repo,
        candidate_id="school-core-4be-1",
        bead_id="school-core-4be",
        issue_number=4,
        repository="example/school-core",
        base_ref="main",
        branch="candidate/stale",
        owner="student-coder",
    )

    (repo / "README.md").write_text("changed after candidate\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "new head")
    result = store.validate(repo, manifest)
    assert result.status == "stale"
    assert "head_sha" in result.reason

    # A dirty tree is also not current evidence, even if HEAD is unchanged.
    dirty_manifest = store.get(manifest.candidate_id)
    (repo / "untracked.txt").write_text("dirty\n")
    dirty_result = store.validate(repo, dirty_manifest)
    assert dirty_result.status == "stale"
    assert "dirty" in dirty_result.reason


def test_candidate_records_fail_closed_when_git_state_cannot_be_read(repo, tmp_path):
    _git(repo, "checkout", "-b", "candidate/malformed")
    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store,
        repo_path=repo,
        candidate_id="school-core-4be-1",
        bead_id="school-core-4be",
        issue_number=4,
        repository="example/school-core",
        base_ref="main",
        branch="candidate/malformed",
        owner="student-coder",
    )
    manifest = dataclasses.replace(manifest, head_sha="not-a-sha")

    result = store.validate(repo, manifest)
    assert result.status == "malformed"
    assert "head_sha" in result.reason
