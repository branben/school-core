"""Bridge-level fail-closed coverage for the PR-creation gate.

These tests drive `_build_trusted_approval_evidence` through the bridge's own
accessors, because the seam that matters is not the gate in isolation but the
bridge refusing to turn automated review output into authorization.

Zero provider writes is asserted at the gate boundary: `allowed is False` means
`_publish_bound_candidate_pr` is never reached, because the bridge raises before
that call.
"""

import importlib
import os
from pathlib import Path

import pytest

import issue_bridge
from candidate_manifest import CandidateManifest, CandidateStore, create_candidate
from state_journal import StateJournal
from teacher_approval_ingress import TRUSTED_APPROVAL_SCOPE


def _make_repo(repo: Path) -> None:
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "School Core"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "school-core@example.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "main"], check=True, capture_output=True)
    (repo / "file.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base", "--date", "2000-01-01T00:00:00+00:00"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "candidate"], check=True, capture_output=True)
    (repo / "file.txt").write_text("changed\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "change", "--date", "2000-01-01T00:00:00+00:00"], check=True, capture_output=True)


def _manifest(tmp_path: Path, *, candidate_id: str, repo: Path) -> CandidateManifest:
    return create_candidate(
        store=CandidateStore(tmp_path / f"candidates-{candidate_id}.json"),
        repo_path=repo,
        candidate_id=candidate_id,
        bead_id="bead-1",
        issue_number=7,
        repository="owner/repo",
        base_ref="main",
        branch="candidate",
        owner="student-vm",
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Default to the fail-closed configuration for every test here."""
    monkeypatch.delenv("APPROVAL_JOURNAL_FILE", raising=False)
    monkeypatch.delenv("APPROVED_ACTORS", raising=False)


def test_review_accepted_is_never_teacher_approval(tmp_path: Path, monkeypatch) -> None:
    """Automated acceptance with an approval_id must yield NO authorization."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-review", repo=repo)

    journal_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("APPROVAL_JOURNAL_FILE", str(journal_path))
    monkeypatch.setenv("APPROVED_ACTORS", "human@example.com")
    StateJournal(journal_path)  # journal exists but holds no approvals

    review_evidence = {
        "accepted": True,
        "approval_id": "reviewer-generated-approval-id",
        "cto_verdict": "PASS",
        "coo_verdict": "PASS",
    }
    approval = issue_bridge._build_trusted_approval_evidence(
        review_evidence, manifest,
        journal=issue_bridge._trusted_approval_journal(),
        allowed_approvers=issue_bridge._trusted_approver_allowlist(),
    )
    assert approval is None, (
        "review.accepted / approval_id was converted into teacher authorization"
    )


def test_wrong_candidate_approval_is_not_authorization(tmp_path: Path, monkeypatch) -> None:
    """An approval for another candidate cannot authorize this one."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _make_repo(repo_a)
    _make_repo(repo_b)
    manifest_a = _manifest(tmp_path, candidate_id="cand-a", repo=repo_a)
    manifest_b = _manifest(tmp_path, candidate_id="cand-b", repo=repo_b)

    journal_path = tmp_path / "journal.sqlite3"
    StateJournal(journal_path).issue_approval(
        approval_id="appr-a",
        candidate_id=manifest_a.candidate_id,
        head_sha=manifest_a.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    approval = issue_bridge._build_trusted_approval_evidence(
        {}, manifest_b,
        journal=StateJournal(journal_path),
        allowed_approvers=("human@example.com",),
    )
    assert approval is None


def test_superseded_head_approval_is_not_authorization(tmp_path: Path) -> None:
    """Approval for the same candidate at an OLD head cannot authorize."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-head", repo=repo)

    journal_path = tmp_path / "journal.sqlite3"
    StateJournal(journal_path).issue_approval(
        approval_id="appr-old",
        candidate_id=manifest.candidate_id,
        head_sha="b" * 40,  # a different, superseded head
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    approval = issue_bridge._build_trusted_approval_evidence(
        {}, manifest,
        journal=StateJournal(journal_path),
        allowed_approvers=("human@example.com",),
    )
    assert approval is None


def test_empty_allowlist_authorizes_nobody(tmp_path: Path, monkeypatch) -> None:
    """Even a genuine, correctly-bound approval is refused with no allowlist."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-allow", repo=repo)

    journal_path = tmp_path / "journal.sqlite3"
    StateJournal(journal_path).issue_approval(
        approval_id="appr-allow",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )
    monkeypatch.setenv("APPROVED_ACTORS", "   ")

    approval = issue_bridge._build_trusted_approval_evidence(
        {}, manifest,
        journal=StateJournal(journal_path),
        allowed_approvers=issue_bridge._trusted_approver_allowlist(),
    )
    assert approval is None


def test_journal_absent_means_no_approval(monkeypatch) -> None:
    """No configured journal => no authorization, no exception."""
    monkeypatch.delenv("APPROVAL_JOURNAL_FILE", raising=False)
    assert issue_bridge._trusted_approval_journal() is None
    monkeypatch.setenv("APPROVED_ACTORS", "human@example.com")
    assert issue_bridge._trusted_approver_allowlist() == ("human@example.com",)


def test_allowlist_parses_multiple_actors(monkeypatch) -> None:
    monkeypatch.setenv("APPROVED_ACTORS", "a@x.com, b@x.com ,")
    assert issue_bridge._trusted_approver_allowlist() == ("a@x.com", "b@x.com")


def test_unreadable_journal_path_is_not_authorization(tmp_path: Path, monkeypatch) -> None:
    """A journal path that cannot be opened fails closed, not open."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file, not a directory\n")
    monkeypatch.setenv("APPROVAL_JOURNAL_FILE", str(blocker / "nested" / "j.sqlite3"))
    assert issue_bridge._trusted_approval_journal() is None


def test_merge_scoped_approval_does_not_open_a_pr(tmp_path: Path) -> None:
    """Merge authority must not be consumed by the PR-creation gate."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-merge", repo=repo)

    journal_path = tmp_path / "journal.sqlite3"
    StateJournal(journal_path).issue_approval(
        approval_id="appr-merge",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope="merge_this_candidate",
    )

    approval = issue_bridge._build_trusted_approval_evidence(
        {}, manifest,
        journal=StateJournal(journal_path),
        allowed_approvers=("human@example.com",),
    )
    assert approval is None, "merge-scoped approval was consumed to open a PR"


def test_exact_authenticated_approval_is_returned(tmp_path: Path) -> None:
    """The positive case: exact candidate, exact head, allowlisted actor."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-ok", repo=repo)

    journal_path = tmp_path / "journal.sqlite3"
    StateJournal(journal_path).issue_approval(
        approval_id="appr-ok",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    approval = issue_bridge._build_trusted_approval_evidence(
        {}, manifest,
        journal=StateJournal(journal_path),
        allowed_approvers=("human@example.com",),
    )
    assert approval is not None
    assert approval.state == "approved"
    assert approval.candidate_id == manifest.candidate_id
    assert approval.head_sha == manifest.head_sha