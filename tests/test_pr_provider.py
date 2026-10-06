"""Behavioral tests for the candidate-bound PR provider seam.

Run: python -m pytest tests/test_pr_provider.py -v

Covers the Phase 1 PR-correctness contract: durable pr_pending/pr_failed/
pr_published state bound to (candidate_id, head_sha), reconcile-before-retry,
idempotent replay (no duplicate provider writes), and fail-closed handling of
ambiguous provider outcomes. No live GitHub write is part of these tests.
"""

import json
import subprocess
from pathlib import Path

import pytest

from candidate_manifest import CandidateStore, create_candidate
from candidate_pr import (
    CandidatePublicationError,
    ProviderWriteError,
    PublicationResult,
    publish_candidate_pr,
)
from pr_provider import (
    GitHubCliPublisher,
    PrPublicationPending,
    PrStateStore,
    publish_candidate_pr_idempotent,
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
    _git(repo, "checkout", "-b", "candidate/exact")
    (repo / "README.md").write_text("candidate\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "candidate")
    store = CandidateStore(tmp_path / "candidates.json")
    manifest = create_candidate(
        store=store, repo_path=repo, candidate_id="candidate-1", bead_id="bead-1",
        issue_number=12, repository="example/school-core", base_ref="main",
        branch="candidate/exact", owner="student-coder",
    )
    journal = PrStateStore(tmp_path / "pr_state.json")
    return repo, store, manifest, journal


_UNSET = object()


class FakePublisher:
    """Fake provider adapter: publish + optional find_pr reconciliation."""

    def __init__(self, *, raises=None, response_override=_UNSET, existing=None,
                 find_error=None):
        self.publish_calls = []
        self.find_calls = []
        self.raises = raises
        self.response_override = response_override
        self.existing = dict(existing or {})
        self.find_error = find_error

    def publish(self, **request):
        self.publish_calls.append(request)
        if self.raises is not None:
            raise self.raises
        if self.response_override is not _UNSET:
            return self.response_override
        return {
            "candidate_id": request["candidate_id"],
            "head_sha": request["head_sha"],
            "pr_url": "https://github.com/example/school-core/pull/7",
        }

    def find_pr(self, *, repository, candidate_id, head_sha, branch):
        self.find_calls.append({
            "repository": repository, "candidate_id": candidate_id,
            "head_sha": head_sha, "branch": branch,
        })
        if self.find_error is not None:
            raise self.find_error
        return self.existing.get((candidate_id, head_sha))


class NoFindPublisher:
    """Publisher without reconciliation capability."""

    def __init__(self):
        self.publish_calls = []

    def publish(self, **request):
        self.publish_calls.append(request)
        return {
            "candidate_id": request["candidate_id"],
            "head_sha": request["head_sha"],
            "pr_url": "https://github.com/example/school-core/pull/7",
        }


def _publish(*, repo, store, manifest, publisher, journal):
    return publish_candidate_pr_idempotent(
        repo_path=repo, store=store, manifest=manifest, publisher=publisher,
        journal=journal, title="Exact candidate", body="Review this candidate.",
    )


# ── Happy path + idempotent replay ─────────────────────────────────────────

def test_first_publication_records_published_state(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    publisher = FakePublisher()

    result = _publish(repo=repo, store=store, manifest=manifest,
                      publisher=publisher, journal=journal)

    assert result.pr_url.endswith("/pull/7")
    record = journal.get(manifest.candidate_id)
    assert record.state == "pr_published"
    assert record.head_sha == manifest.head_sha
    assert record.pr_url == result.pr_url
    assert len(publisher.publish_calls) == 1


def test_replay_returns_recorded_url_without_provider_write(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    publisher = FakePublisher()
    first = _publish(repo=repo, store=store, manifest=manifest,
                     publisher=publisher, journal=journal)

    second = _publish(repo=repo, store=store, manifest=manifest,
                      publisher=publisher, journal=journal)

    assert second == first
    assert len(publisher.publish_calls) == 1, "replay must not re-publish"
    assert publisher.find_calls == []


def test_journal_survives_store_reopen(candidate_repo, tmp_path):
    repo, store, manifest, journal = candidate_repo
    publisher = FakePublisher()
    result = _publish(repo=repo, store=store, manifest=manifest,
                      publisher=publisher, journal=journal)

    reopened = PrStateStore(journal.path)
    record = reopened.get(manifest.candidate_id)
    assert record.state == "pr_published"
    assert record.pr_url == result.pr_url


# ── Ambiguous provider outcomes become pr_pending ──────────────────────────

@pytest.mark.parametrize("outcome", ["raises", "none", "mismatch", "no_url"])
def test_ambiguous_provider_outcome_marks_pending(candidate_repo, outcome):
    repo, store, manifest, journal = candidate_repo
    if outcome == "raises":
        publisher = FakePublisher(raises=RuntimeError("create timed out"))
    elif outcome == "none":
        publisher = FakePublisher(response_override=None)
    elif outcome == "mismatch":
        publisher = FakePublisher(response_override={
            "candidate_id": "another-candidate", "head_sha": manifest.head_sha,
            "pr_url": "https://github.com/example/school-core/pull/8",
        })
    else:
        publisher = FakePublisher(response_override={
            "candidate_id": manifest.candidate_id, "head_sha": manifest.head_sha,
        })

    with pytest.raises(PrPublicationPending):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=publisher, journal=journal)

    record = journal.get(manifest.candidate_id)
    assert record.state == "pr_pending"
    assert record.head_sha == manifest.head_sha
    assert record.error
    # The write was attempted exactly once — ambiguity is not a license to retry blindly.
    assert len(publisher.publish_calls) == 1


def test_provider_write_error_is_distinguishable_from_refusal(candidate_repo):
    """Provider-call failures raise ProviderWriteError (ambiguous); pre-write
    refusals raise plain CandidatePublicationError."""
    repo, store, manifest, journal = candidate_repo
    publisher = FakePublisher(raises=RuntimeError("boom"))
    with pytest.raises(ProviderWriteError):
        publish_candidate_pr(
            repo_path=repo, store=store, manifest=manifest, publisher=publisher,
            title="t", body="b",
        )
    assert issubclass(ProviderWriteError, CandidatePublicationError)


# ── Pre-write refusals become pr_failed with zero provider writes ──────────

def test_pre_write_refusal_marks_failed_without_provider_write(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    (repo / "README.md").write_text("moved on\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "new head")
    publisher = FakePublisher()

    with pytest.raises(CandidatePublicationError):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=publisher, journal=journal)

    record = journal.get(manifest.candidate_id)
    assert record.state == "pr_failed"
    assert publisher.publish_calls == [], "refusal must precede any provider write"


def test_failed_state_retries_directly_without_reconcile(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    # Force a refusal first (stale candidate), then restore the worktree state
    # is impossible for the same manifest — so record the failed row directly.
    journal.record_failed(
        candidate_id=manifest.candidate_id, issue_number=manifest.issue_number,
        repository=manifest.repository, branch=manifest.branch,
        head_sha=manifest.head_sha, error="earlier refusal",
    )
    publisher = FakePublisher()

    result = _publish(repo=repo, store=store, manifest=manifest,
                      publisher=publisher, journal=journal)

    assert result.pr_url.endswith("/pull/7")
    assert publisher.find_calls == [], "a known no-write failure needs no reconcile"
    assert len(publisher.publish_calls) == 1


# ── Reconcile-before-retry (duplicate-PR safety) ───────────────────────────

def test_reconcile_adopts_existing_pr_without_second_write(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    first = FakePublisher(raises=RuntimeError("timeout after create"))
    with pytest.raises(PrPublicationPending):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=first, journal=journal)

    existing_url = "https://github.com/example/school-core/pull/9"
    second = FakePublisher(existing={
        (manifest.candidate_id, manifest.head_sha): existing_url,
    })
    result = _publish(repo=repo, store=store, manifest=manifest,
                      publisher=second, journal=journal)

    assert result.pr_url == existing_url
    assert second.publish_calls == [], "reconciled replay must not create a duplicate PR"
    record = journal.get(manifest.candidate_id)
    assert record.state == "pr_published"
    assert record.pr_url == existing_url


def test_reconcile_missing_pr_permits_safe_retry(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    first = FakePublisher(raises=RuntimeError("timeout"))
    with pytest.raises(PrPublicationPending):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=first, journal=journal)

    second = FakePublisher(existing={})  # provider confirms: no PR exists
    result = _publish(repo=repo, store=store, manifest=manifest,
                      publisher=second, journal=journal)

    assert result.pr_url.endswith("/pull/7")
    assert len(second.publish_calls) == 1
    assert len(second.find_calls) == 1
    assert second.find_calls[0]["candidate_id"] == manifest.candidate_id
    assert second.find_calls[0]["head_sha"] == manifest.head_sha


def test_unreconcilable_pending_blocks_republish(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    first = FakePublisher(raises=RuntimeError("timeout"))
    with pytest.raises(PrPublicationPending):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=first, journal=journal)

    blind = FakePublisher(find_error=RuntimeError("provider unreachable"))
    with pytest.raises(PrPublicationPending):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=blind, journal=journal)

    assert blind.publish_calls == [], (
        "an unreconciled pending state must not risk a duplicate PR"
    )
    assert journal.get(manifest.candidate_id).state == "pr_pending"


def test_pending_without_find_pr_blocks_republish(candidate_repo):
    repo, store, manifest, journal = candidate_repo
    journal.record_pending(
        candidate_id=manifest.candidate_id, issue_number=manifest.issue_number,
        repository=manifest.repository, branch=manifest.branch,
        head_sha=manifest.head_sha, error="unknown outcome",
    )
    blind = NoFindPublisher()

    with pytest.raises(PrPublicationPending):
        _publish(repo=repo, store=store, manifest=manifest,
                 publisher=blind, journal=journal)

    assert blind.publish_calls == []


# ── GitHub CLI adapter (captured argv only — no live writes) ───────────────

class RecordingRunner:
    def __init__(self, outputs=None, failing=None):
        self.calls = []
        self.outputs = outputs or {}
        self.failing = failing or set()

    def __call__(self, argv):
        self.calls.append(list(argv))
        key = argv[1] if len(argv) > 1 else ""
        if key in self.failing:
            raise RuntimeError(f"command failed: {argv}")
        return self.outputs.get(key, "")


def test_github_cli_publisher_pushes_branch_and_opens_pr():
    runner = RecordingRunner(outputs={"pr": "https://github.com/example/school-core/pull/42\n"})
    publisher = GitHubCliPublisher(repo_path=Path("/tmp/repo"), runner=runner)

    response = publisher.publish(
        repository="example/school-core", candidate_id="candidate-1",
        head_sha="a" * 40, base_ref="main", base_sha="b" * 40,
        branch="candidate/exact", title="Exact candidate", body="Body",
        diff=b"diff --git a/x b/x\n", label="school-candidate",
    )

    assert response["candidate_id"] == "candidate-1"
    assert response["head_sha"] == "a" * 40
    assert response["pr_url"] == "https://github.com/example/school-core/pull/42"
    push = next(c for c in runner.calls if c[0] == "git")
    assert push[1:4] == ["-C", "/tmp/repo", "push"]
    assert "candidate/exact" in push
    create = next(c for c in runner.calls if c[0] == "gh")
    assert create[1:3] == ["pr", "create"]
    assert create[create.index("--head") + 1] == "candidate/exact"
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--label") + 1] == "school-candidate"


def test_github_cli_find_pr_matches_head_sha_only():
    runner = RecordingRunner(outputs={
        "pr": json.dumps([{"url": "https://github.com/example/school-core/pull/9",
                           "headRefOid": "a" * 40}]),
    })
    publisher = GitHubCliPublisher(repo_path=Path("/tmp/repo"), runner=runner)

    found = publisher.find_pr(
        repository="example/school-core", candidate_id="candidate-1",
        head_sha="a" * 40, branch="candidate/exact",
    )
    assert found == "https://github.com/example/school-core/pull/9"

    missing = publisher.find_pr(
        repository="example/school-core", candidate_id="candidate-1",
        head_sha="c" * 40, branch="candidate/exact",
    )
    assert missing is None, "a PR for another head is not this candidate's PR"


def test_github_cli_find_pr_fails_closed_on_command_error():
    runner = RecordingRunner(failing={"pr"})
    publisher = GitHubCliPublisher(repo_path=Path("/tmp/repo"), runner=runner)

    with pytest.raises(RuntimeError):
        publisher.find_pr(
            repository="example/school-core", candidate_id="candidate-1",
            head_sha="a" * 40, branch="candidate/exact",
        )
