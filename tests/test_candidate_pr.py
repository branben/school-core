"""Behavioral tests for exact-candidate PR publication."""

import subprocess
from pathlib import Path

import pytest

from candidate_manifest import CandidateStore, create_candidate
from candidate_pr import CandidatePublicationError, publish_candidate_pr


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
    _git(repo, "checkout", "-b", "candidate/exact")
    (repo / "README.md").write_text("candidate\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "candidate")
    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store, repo_path=repo, candidate_id="candidate-1", bead_id="bead-1",
        issue_number=1, repository="example/school-core", base_ref="main",
        branch="candidate/exact", owner="student-coder",
    )
    return repo, store, manifest


class FakePublisher:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {
            "candidate_id": "candidate-1",
            "head_sha": "a" * 40,
            "pr_url": "https://github.com/example/school-core/pull/7",
        }

    def publish(self, **request):
        self.calls.append(request)
        return self.response


def test_publishes_exact_candidate_diff_with_candidate_identity(candidate_repo):
    repo, store, manifest = candidate_repo
    publisher = FakePublisher({**{
        "candidate_id": manifest.candidate_id,
        "head_sha": manifest.head_sha,
        "pr_url": "https://github.com/example/school-core/pull/7",
    }})
    result = publish_candidate_pr(
        repo_path=repo, store=store, manifest=manifest, publisher=publisher,
        title="Exact candidate", body="Review this candidate.",
    )

    assert result.pr_url.endswith("/pull/7")
    assert result.candidate_id == manifest.candidate_id
    assert result.head_sha == manifest.head_sha
    request = publisher.calls[0]
    assert request["candidate_id"] == manifest.candidate_id
    assert request["head_sha"] == manifest.head_sha
    assert request["base_sha"] == manifest.base_sha
    assert request["base_ref"] == "main"
    assert request["branch"] == "candidate/exact"
    assert request["diff"].startswith(b"diff --git ")


def test_publication_refuses_stale_candidate_before_provider_write(candidate_repo):
    repo, store, manifest = candidate_repo
    (repo / "README.md").write_text("new head\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "new head")
    publisher = FakePublisher()

    with pytest.raises(CandidatePublicationError, match="stale"):
        publish_candidate_pr(
            repo_path=repo, store=store, manifest=manifest, publisher=publisher,
            title="Exact candidate", body="Review this candidate.",
        )
    assert publisher.calls == []


def test_publication_rejects_provider_head_or_candidate_mismatch(candidate_repo):
    repo, store, manifest = candidate_repo
    publisher = FakePublisher({
        "candidate_id": "another-candidate", "head_sha": manifest.head_sha,
        "pr_url": "https://github.com/example/school-core/pull/8",
    })
    with pytest.raises(CandidatePublicationError, match="candidate_id"):
        publish_candidate_pr(
            repo_path=repo, store=store, manifest=manifest, publisher=publisher,
            title="Exact candidate", body="Review this candidate.",
        )


def test_publication_rejects_empty_candidate_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "candidate/noop")
    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store, repo_path=repo, candidate_id="candidate-noop", bead_id="bead-1",
        issue_number=1, repository="example/school-core", base_ref="main",
        branch="candidate/noop", owner="student-coder",
    )
    publisher = FakePublisher()

    with pytest.raises(CandidatePublicationError, match="empty"):
        publish_candidate_pr(
            repo_path=repo, store=store, manifest=manifest, publisher=publisher,
            title="No-op candidate", body="Review this candidate.",
        )
    assert publisher.calls == []


def test_artifact_candidate_uses_explicit_artifact_label(candidate_repo):
    repo, store, manifest = candidate_repo
    artifact = manifest.__class__(**{**manifest.__dict__, "candidate_kind": "artifact"})
    publisher = FakePublisher({
        "candidate_id": artifact.candidate_id, "head_sha": artifact.head_sha,
        "pr_url": "https://github.com/example/school-core/pull/9",
    })
    publish_candidate_pr(
        repo_path=repo, store=store, manifest=artifact, publisher=publisher,
        title="Exact artifact", body="Review this artifact.",
    )
    assert publisher.calls[0]["label"] == "school-artifact"
