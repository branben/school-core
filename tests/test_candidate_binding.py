import subprocess
from pathlib import Path

import pytest

from candidate_manifest import CandidateManifest, CandidateStore, create_candidate
from candidate_pr_gate import GateDecision, TeacherApproval, VerificationEvidence, gate_candidate_publication
from candidate_binding import (
    CandidateBinding,
    CandidateBindingStore,
    bind_trusted_evidence,
    lookup_binding,
    trusted_verification_from_binding,
    teacher_approval_from_binding,
)


def _sha() -> str:
    return "a" * 40


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


def _make_manifest(tmp_path: Path, *, candidate_id: str, repo_path: Path) -> CandidateManifest:
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


def test_binding_carries_candidate_identity_and_posture(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c1", seed="a")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c1", repo_path=repo)
    verification = _VerificationStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        passed=True,
        skipped=False,
        failures=(),
    )
    approval = _ApprovalStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        state="approved",
        approval_id="teacher-1",
    )
    binding = bind_trusted_evidence(
        manifest=manifest,
        verification=verification,
        approval=approval,
    )
    assert binding.candidate_id == manifest.candidate_id
    assert binding.head_sha == manifest.head_sha
    assert binding.repository == manifest.repository
    assert binding.issue_number == manifest.issue_number
    assert binding.verification_passed is True
    assert binding.verification_skipped is False
    assert binding.approval_state == "approved"


def test_binding_refuses_candidate_mismatch_for_verification(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c2", seed="b")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c2", repo_path=repo)
    bad = _VerificationStub(
        candidate_id="wrong-candidate",
        head_sha=manifest.head_sha,
        passed=True,
        skipped=False,
        failures=(),
    )
    with pytest.raises(ValueError, match="verification"):
        bind_trusted_evidence(manifest=manifest, verification=bad, approval=None)


def test_binding_refuses_candidate_mismatch_for_approval(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c3", seed="c")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c3", repo_path=repo)
    verified = _VerificationStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        passed=True,
        skipped=False,
        failures=(),
    )
    bad = _ApprovalStub(
        candidate_id="wrong-candidate",
        head_sha=manifest.head_sha,
        state="approved",
        approval_id="teacher-2",
    )
    with pytest.raises(ValueError, match="approval"):
        bind_trusted_evidence(manifest=manifest, verification=verified, approval=bad)


def test_binding_lookup_matches_by_candidate_id(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c4", seed="d")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c4", repo_path=repo)
    verified = _VerificationStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        passed=True,
        skipped=False,
        failures=(),
    )
    approval = _ApprovalStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        state="approved",
        approval_id="teacher-3",
    )
    binding = bind_trusted_evidence(manifest=manifest, verification=verified, approval=approval)
    store_path = tmp_path / "binding-store.json"
    binding_store = CandidateBindingStore(store_path)
    binding_store.put(binding)
    found = lookup_binding(binding_store, candidate_id=manifest.candidate_id)
    assert found is not None
    assert found.candidate_id == manifest.candidate_id
    assert found.head_sha == manifest.head_sha


def test_gate_uses_bound_posture_instead_of_raw_stubs(tmp_path: Path) -> None:
    repo = _temp_repo(tmp_path, candidate_id="c5", seed="e")
    _make_repo(repo)
    manifest = _make_manifest(tmp_path, candidate_id="c5", repo_path=repo)
    verified = _VerificationStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        passed=True,
        skipped=False,
        failures=(),
    )
    approval = _ApprovalStub(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        state="approved",
        approval_id="teacher-4",
    )
    binding = bind_trusted_evidence(manifest=manifest, verification=verified, approval=approval)
    store_path = tmp_path / "binding-store.json"
    binding_store = CandidateBindingStore(store_path)
    binding_store.put(binding)
    decision = gate_candidate_publication(
        store=CandidateStore(tmp_path / "candidates-gate.json"),
        manifest=manifest,
        repo_path=manifest.worktree,
        verification=trusted_verification_from_binding(binding),
        approval=teacher_approval_from_binding(binding),
        require_approval=True,
    )
    assert decision.allowed is True
    assert decision.failure_class == "authorized"
