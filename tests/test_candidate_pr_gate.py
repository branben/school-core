import subprocess
from pathlib import Path

import pytest

from candidate_manifest import CandidateManifest, CandidateStore, create_candidate
from candidate_pr_gate import (
    GateDecision,
    TeacherApproval,
    VerificationEvidence,
    gate_candidate_publication,
)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


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


def _temp_repo(tmp_path: Path, *, candidate_id: str, seed: str = "") -> Path:
    return tmp_path / f"repo-{candidate_id}-{seed}"


def _make_manifest(tmp_path: Path, *, candidate_id: str, repo_path: Path, dirty: bool = False) -> CandidateManifest:
    if dirty:
        (repo_path / "extra").write_text("x")
    store = CandidateStore(tmp_path / f"candidates-{candidate_id}.json")
    return create_candidate(
        store=store,
        repo_path=repo_path,
        candidate_id=candidate_id,
        bead_id="bead-1",
        issue_number=1,
        repository="owner/repo",
        base_ref="main",
        branch="candidate",
        owner="student-vm",
    )


class _VerificationStub:
    def __init__(self, *, candidate_id: str, head_sha: str, passed: bool, skipped: bool, failures: tuple) -> None:
        self.candidate_id = candidate_id
        self.head_sha = head_sha
        self.passed = passed
        self.skipped = skipped
        self.failures = failures


class _ApprovalStub:
    def __init__(self, *, candidate_id: str, head_sha: str, state: str, approval_id: str) -> None:
        self.candidate_id = candidate_id
        self.head_sha = head_sha
        self.state = state
        self.approval_id = approval_id


def _gate(
    tmp_path: Path,
    *,
    candidate_id: str,
    repo_path: Path,
    verification: VerificationEvidence | None,
    approval: TeacherApproval | None,
    dirty: bool = False,
    manifest: CandidateManifest | None = None,
) -> GateDecision:
    manifest = manifest or _make_manifest(
        tmp_path, candidate_id=candidate_id, repo_path=repo_path, dirty=dirty,
    )
    store = CandidateStore(tmp_path / f"candidates-{candidate_id}.json")
    return gate_candidate_publication(
        store=store,
        manifest=manifest,
        repo_path=manifest.worktree,
        verification=verification,
        approval=approval,
        require_approval=True,
    )


def test_gate_allows_passed_verification_with_approval(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c1", seed="a")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c1",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c1",
            head_sha=_head(repo),
            passed=True,
            skipped=False,
            failures=(),
        ),
        approval=_ApprovalStub(
            candidate_id="c1",
            head_sha=_head(repo),
            state="approved",
            approval_id="teacher-1",
        ),
    )
    assert decision.allowed is True
    assert decision.failure_class == "authorized"


@pytest.mark.parametrize("mismatch", ["candidate_id", "head_sha"])
def test_gate_refuses_verification_not_bound_to_manifest(
    tmp_path: Path, mismatch: str,
) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c1", seed=f"identity-{mismatch}")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c1", repo_path=repo)
    verification = _VerificationStub(
        candidate_id=("other-candidate" if mismatch == "candidate_id" else manifest.candidate_id),
        head_sha=("b" * 40 if mismatch == "head_sha" else manifest.head_sha),
        passed=True,
        skipped=False,
        failures=(),
    )
    decision = _gate(
        tmp_path,
        candidate_id="c1",
        repo_path=repo,
        manifest=manifest,
        verification=verification,
        approval=_ApprovalStub(
            candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
            state="approved", approval_id="teacher-1",
        ),
    )
    assert decision.allowed is False
    assert decision.failure_class == "trusted_verification_identity_mismatch"


def test_gate_refuses_verification_without_candidate_identity(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c1", seed="identity-missing")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c1", repo_path=repo)
    verification = _VerificationStub(
        candidate_id="", head_sha="", passed=True, skipped=False, failures=(),
    )
    decision = _gate(
        tmp_path, candidate_id="c1", repo_path=repo, manifest=manifest,
        verification=verification,
        approval=_ApprovalStub(
            candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
            state="approved", approval_id="teacher-1",
        ),
    )
    assert decision.allowed is False
    assert decision.failure_class == "trusted_verification_identity_mismatch"


def test_gate_refuses_approval_with_mismatched_head(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c1", seed="approval-head")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c1", repo_path=repo)
    decision = _gate(
        tmp_path, candidate_id="c1", repo_path=repo, manifest=manifest,
        verification=_VerificationStub(
            candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
            passed=True, skipped=False, failures=(),
        ),
        approval=_ApprovalStub(
            candidate_id=manifest.candidate_id, head_sha="b" * 40,
            state="approved", approval_id="teacher-1",
        ),
    )
    assert decision.allowed is False
    assert decision.failure_class == "teacher_approval_identity_mismatch"


def test_gate_refuses_approval_not_bound_to_manifest(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c1", seed="approval-identity")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c1",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c1", head_sha=_head(repo), passed=True, skipped=False, failures=(),
        ),
        approval=_ApprovalStub(
            candidate_id="other-candidate", head_sha=_head(repo), state="approved",
            approval_id="teacher-1",
        ),
    )
    assert decision.allowed is False
    assert decision.failure_class == "teacher_approval_identity_mismatch"


def test_gate_refuses_when_trusted_verification_is_missing(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c2", seed="b")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c2",
        repo_path=repo,
        verification=None,
        approval=None,
    )
    assert decision.allowed is False
    assert decision.failure_class == "trusted_verification_missing"


def test_gate_refuses_when_trusted_verification_skipped(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c3", seed="c")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c3",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c3",
            head_sha=_head(repo),
            passed=False,
            skipped=True,
            failures=(),
        ),
        approval=None,
    )
    assert decision.allowed is False
    assert decision.failure_class == "trusted_verification_not_passed"


def test_gate_refuses_when_trusted_verification_failed(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c4", seed="d")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c4",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c4",
            head_sha=_head(repo),
            passed=False,
            skipped=False,
            failures=({"cmd": "pytest", "exit": 1},),
        ),
        approval=None,
    )
    assert decision.allowed is False
    assert decision.failure_class == "trusted_verification_not_passed"


def test_gate_refuses_when_approval_required_but_missing(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c5", seed="e")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c5",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c5",
            head_sha=_head(repo),
            passed=True,
            skipped=False,
            failures=(),
        ),
        approval=None,
    )
    assert decision.allowed is False
    assert decision.failure_class == "teacher_approval_required"


def test_gate_refuses_when_approval_required_but_rejected(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c6", seed="f")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c6",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c6",
            head_sha=_head(repo),
            passed=True,
            skipped=False,
            failures=(),
        ),
        approval=_ApprovalStub(
            candidate_id="c6",
            head_sha=_head(repo),
            state="rejected",
            approval_id="teacher-2",
        ),
    )
    assert decision.allowed is False
    assert decision.failure_class == "teacher_approval_required"


def test_gate_refuses_when_candidate_not_current_dirty_tree(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c7", seed="g")
    _make_repo(repo)
    decision = _gate(
        tmp_path,
        candidate_id="c7",
        repo_path=repo,
        verification=_VerificationStub(
            candidate_id="c7",
            head_sha=_head(repo),
            passed=True,
            skipped=False,
            failures=(),
        ),
        approval=None,
        dirty=True,
    )
    assert decision.allowed is False
    assert decision.failure_class == "candidate_not_current"


def test_gate_refuses_when_candidate_head_changed(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c8", seed="h")
    _make_repo(repo)

    # The manifest is created against the repo state at creation time, before
    # the repo advances. The gate must refuse publication for that manifest
    # because its recorded head_sha no longer matches the repo the bridge
    # cloned. This is the exact-candidate identity check the gate delegates to
    # CandidateStore.validate(...), not a PR-gate-only decision.
    manifest_before_advancing = _make_manifest(
        tmp_path,
        candidate_id="c8",
        repo_path=repo,
        dirty=False,
    )
    (repo / "file.txt").write_text("changed2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "change2"], check=True, capture_output=True)

    decision = _gate(
        tmp_path,
        candidate_id="c8",
        repo_path=repo,
        manifest=manifest_before_advancing,
        verification=_VerificationStub(
            candidate_id="c8",
            head_sha=manifest_before_advancing.head_sha,
            passed=True,
            skipped=False,
            failures=(),
        ),
        approval=_ApprovalStub(
            candidate_id="c8",
            head_sha=manifest_before_advancing.head_sha,
            state="approved",
            approval_id="teacher-c8",
        ),
    )
    assert decision.allowed is False
    assert decision.failure_class == "candidate_not_current"
