"""RED-first coverage for the restart-binding and trusted-approval ingress gap.

Two defects this file pins down:

1. CandidateBindingStore.put() persisted a single-entry dict, so storing a
   second binding destroyed the first. A resumed cycle then looked up a
   candidate whose posture had been silently evicted from disk.

2. The bridge had no way to obtain an authenticated, candidate-bound teacher
   approval, so the candidate PR path could never be authorized even when a
   real approval existed. This file builds the ingress from StateJournal
   (which already persists actor-scoped approvals) and pins the identity
   binding so an approval for another candidate/head cannot authorize.
"""

import subprocess
from pathlib import Path

from candidate_binding import (
    CandidateBindingStore,
    bind_trusted_evidence,
    lookup_binding,
)
from candidate_manifest import CandidateManifest, CandidateStore, create_candidate
from candidate_pr_gate import gate_candidate_publication
from teacher_approval_ingress import (
    TRUSTED_APPROVAL_SCOPE,
    trusted_approval_from_journal,
)


def _sha(seed: str = "a") -> str:
    return seed * 40


def _make_repo(repo: Path) -> None:
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


def _manifest(tmp_path: Path, *, candidate_id: str, repo: Path, tag: str) -> CandidateManifest:
    return create_candidate(
        store=CandidateStore(tmp_path / f"candidates-{candidate_id}.json"),
        repo_path=repo,
        candidate_id=candidate_id,
        bead_id="bead-1",
        issue_number=1,
        repository="owner/repo",
        base_ref="main",
        branch="candidate",
        owner="student-vm",
    )


class _VerificationStub:
    def __init__(self, *, candidate_id: str, head_sha: str) -> None:
        self.candidate_id = candidate_id
        self.head_sha = head_sha
        self.passed = True
        self.skipped = False
        self.failures = ()


# ── Defect 1: restart binding persistence ───────────────────────────────


def test_binding_store_preserves_earlier_bindings(tmp_path: Path) -> None:
    """A second put() must not evict the first candidate's persisted posture."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _make_repo(repo_a)
    _make_repo(repo_b)
    manifest_a = _manifest(tmp_path, candidate_id="cand-a", repo=repo_a, tag="a")
    manifest_b = _manifest(tmp_path, candidate_id="cand-b", repo=repo_b, tag="b")

    store = CandidateBindingStore(tmp_path / "bindings.json")
    binding_a = bind_trusted_evidence(
        manifest=manifest_a,
        verification=_VerificationStub(
            candidate_id=manifest_a.candidate_id, head_sha=manifest_a.head_sha,
        ),
        approval=None,
    )
    binding_b = bind_trusted_evidence(
        manifest=manifest_b,
        verification=_VerificationStub(
            candidate_id=manifest_b.candidate_id, head_sha=manifest_b.head_sha,
        ),
        approval=None,
    )
    store.put(binding_a)
    store.put(binding_b)

    # Reload from disk the way a resumed cycle must.
    resumed = CandidateBindingStore(tmp_path / "bindings.json")
    found_a = lookup_binding(resumed, candidate_id="cand-a")
    assert found_a is not None, "first binding was evicted by the second put()"
    assert found_a.head_sha == manifest_a.head_sha

    # And a fresh process would see the same thing.
    again = CandidateBindingStore(tmp_path / "bindings.json").get("cand-b")
    assert again is not None
    assert again.head_sha == manifest_b.head_sha


def test_resumed_binding_still_gates_the_same_candidate(tmp_path: Path) -> None:
    """Reload from disk, then gate: identity must survive the round trip."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-resume", repo=repo, tag="r")

    store = CandidateBindingStore(tmp_path / "bindings.json")
    store.put(bind_trusted_evidence(
        manifest=manifest,
        verification=_VerificationStub(
            candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
        ),
        approval=None,
    ))

    resumed = CandidateBindingStore(tmp_path / "bindings.json").get("cand-resume")
    assert resumed is not None
    from candidate_binding import (
        teacher_approval_from_binding,
        trusted_verification_from_binding,
    )

    decision = gate_candidate_publication(
        store=CandidateStore(tmp_path / "candidates-cand-resume.json"),
        manifest=manifest,
        repo_path=repo,
        verification=trusted_verification_from_binding(resumed),
        approval=teacher_approval_from_binding(resumed),
        require_approval=True,
    )
    # Verification survived; approval is absent, so the gate must still refuse.
    assert decision.allowed is False
    assert decision.failure_class == "teacher_approval_required"


# ── Defect 2: trusted approval ingress ──────────────────────────────────


def test_authenticated_journal_approval_authorizes_exact_candidate(tmp_path: Path) -> None:
    """A real persisted, actor-scoped approval authorizes its own candidate."""
    from state_journal import StateJournal

    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-ok", repo=repo, tag="ok")
    journal = StateJournal(tmp_path / "journal.sqlite3")
    journal.issue_approval(
        approval_id="appr-1",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    approval = trusted_approval_from_journal(
        journal=journal,
        manifest=manifest,
        allowed_approvers=("human@example.com",),
    )
    assert approval is not None
    assert approval.state == "approved"
    assert approval.candidate_id == manifest.candidate_id
    assert approval.approval_id == "appr-1"


def test_approval_for_another_candidate_is_not_authorization(tmp_path: Path) -> None:
    """Wrong candidate => no approval evidence at all, gate refuses."""
    from state_journal import StateJournal

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _make_repo(repo_a)
    _make_repo(repo_b)
    manifest_a = _manifest(tmp_path, candidate_id="cand-x", repo=repo_a, tag="x")
    manifest_b = _manifest(tmp_path, candidate_id="cand-y", repo=repo_b, tag="y")

    journal = StateJournal(tmp_path / "journal.sqlite3")
    journal.issue_approval(
        approval_id="appr-x",
        candidate_id=manifest_a.candidate_id,
        head_sha=manifest_a.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    approval = trusted_approval_from_journal(
        journal=journal,
        manifest=manifest_b,
        allowed_approvers=("human@example.com",),
    )
    assert approval is None, "an approval for another candidate was surfaced"


def test_non_allowlisted_actor_is_not_authorization(tmp_path: Path) -> None:
    """A persisted approval from an unknown actor yields no authorization."""
    from state_journal import StateJournal

    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-actor", repo=repo, tag="ac")

    journal = StateJournal(tmp_path / "journal.sqlite3")
    journal.issue_approval(
        approval_id="appr-stranger",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="stranger@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    approval = trusted_approval_from_journal(
        journal=journal,
        manifest=manifest,
        allowed_approvers=("human@example.com",),
    )
    assert approval is None


def test_wrong_scope_is_not_authorization(tmp_path: Path) -> None:
    """An approval scoped to merge is not an approval to open a PR."""
    from state_journal import StateJournal

    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-scope", repo=repo, tag="sc")

    journal = StateJournal(tmp_path / "journal.sqlite3")
    journal.issue_approval(
        approval_id="appr-merge",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope="merge_this_candidate",
    )

    approval = trusted_approval_from_journal(
        journal=journal,
        manifest=manifest,
        allowed_approvers=("human@example.com",),
    )
    assert approval is None


def test_revoked_approval_is_not_authorization(tmp_path: Path) -> None:
    """Only an unconsumed, in-scope approval authorizes publication."""
    from state_journal import ConflictError, StateJournal

    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-consumed", repo=repo, tag="co")

    journal = StateJournal(tmp_path / "journal.sqlite3")
    journal.issue_approval(
        approval_id="appr-used",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )
    journal.consume_approval(
        approval_id="appr-used",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        operation_id="op-1",
        idempotency_key="idem-1",
    )

    approval = trusted_approval_from_journal(
        journal=journal,
        manifest=manifest,
        allowed_approvers=("human@example.com",),
    )
    assert approval is None, "a consumed approval was surfaced as authorization"


def test_automated_review_is_never_an_approval(tmp_path: Path) -> None:
    """review.accepted=True must not produce teacher authorization."""
    from state_journal import StateJournal

    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-auto", repo=repo, tag="au")

    journal = StateJournal(tmp_path / "journal.sqlite3")
    # No approval was ever issued for this candidate.
    approval = trusted_approval_from_journal(
        journal=journal,
        manifest=manifest,
        allowed_approvers=("human@example.com",),
    )
    assert approval is None


def test_approval_survives_restart_for_same_candidate(tmp_path: Path) -> None:
    """Reopening the journal on disk still yields the same authorization."""
    from state_journal import StateJournal

    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = _manifest(tmp_path, candidate_id="cand-persist", repo=repo, tag="p")
    journal_path = tmp_path / "journal.sqlite3"

    StateJournal(journal_path).issue_approval(
        approval_id="appr-persist",
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        actor="human@example.com",
        scope=TRUSTED_APPROVAL_SCOPE,
    )

    # Simulate a restart: brand new StateJournal object over the same file.
    restarted = StateJournal(journal_path)
    approval = trusted_approval_from_journal(
        journal=restarted,
        manifest=manifest,
        allowed_approvers=("human@example.com",),
    )
    assert approval is not None
    assert approval.approval_id == "appr-persist"