"""End-to-end local proof: student guest -> exact export -> materialize ->
clean verifier guest -> candidate-bound evidence, in one tested flow.

Plan Phase 2 verification: "a local proof demonstrates create -> execute ->
exact candidate export -> clean verifier check -> artifact collection ->
destroy on the target machine." Every join is fail-closed: a trusted manifest
for the wrong task, or verifier evidence for a different candidate, blocks.
"""

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

from candidate_manifest import CandidateStore

import pytest

from student_vm_runner import (
    SmolVmRunner,
    StudentTaskRequest,
    StudentVMBlocked,
    dispatch_student_task,
)
from verifier_vm import (
    SmolVmVerifier,
    TrustedCheckManifest,
    VerifiedStudentFlow,
    dispatch_and_verify_student_task,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def task_repo(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "flow-test@example.invalid")
    _git(repo, "config", "user.name", "Flow Test")
    (repo / "README.md").write_text("trusted base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _candidate_archive():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        source = b"candidate source\n"
        member = tarfile.TarInfo("solution.py")
        member.size = len(source)
        tar.addfile(member, io.BytesIO(source))
    return archive.getvalue()


def _request(repo, base_sha, **over):
    values = {
        "task_id": "task-1",
        "repository": "example/project",
        "repo_path": repo,
        "base_sha": base_sha,
        "task": {"prompt": "Solve it."},
        "command": ("student-agent",),
    }
    values.update(over)
    return StudentTaskRequest(**values)


class FakeProcess:
    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def __call__(self, args, *, timeout_seconds, output_limit_bytes, env,
                 stdout_limit_bytes, stderr_limit_bytes):
        sub = args[2] if len(args) > 2 else "?"
        self.calls.append(list(args))
        action = self.script.get(sub, (0, b"", b""))
        if isinstance(action, Exception):
            raise action
        rc, out, err = action
        return subprocess.CompletedProcess(args, rc, out, err)

    def subs(self):
        return [c[2] for c in self.calls]


def _fake_student_runner(tmp_path, process=None):
    pack = tmp_path / "pack"
    pack.write_bytes(b"pack")
    return SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(),
        process_runner=process or FakeProcess({"exec": (0, _candidate_archive(), b"")}),
        quarantine_path=tmp_path / "quarantine.jsonl",
    )


def _fake_verifier(tmp_path, process=None):
    pack = tmp_path / "pack"
    pack.write_bytes(b"pack")
    return SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(),
        process_runner=process or FakeProcess({"exec": (0, b"SCV-CHECK unit exit=0\n", b"")}),
    )


def _trusted_manifest(base_sha, **over):
    values = {
        "task_id": "task-1",
        "repository": "example/project",
        "base_sha": base_sha,
        "trusted_checks": ({"name": "unit", "cmd": "pytest -q", "cwd": "."},),
    }
    values.update(over)
    return TrustedCheckManifest(**values)


def _run_flow(task_repo, tmp_path, **over):
    repo, base_sha = task_repo
    kwargs = {
        "student_runner": _fake_student_runner(tmp_path),
        "request": _request(repo, base_sha),
        "verifier": _fake_verifier(tmp_path),
        "trusted_manifest": _trusted_manifest(base_sha),
        "repo_path": repo,
        "destination": tmp_path / "flow-out",
        "candidate_store": CandidateStore(tmp_path / "candidates.json"),
        "candidate_id": "candidate-task-1",
        "bead_id": "school-core-sjv.7.17",
        "issue_number": 21,
        "branch": "candidate/task-1",
    }
    kwargs.update(over)
    return dispatch_and_verify_student_task(**kwargs)


def test_flow_produces_candidate_bound_verifier_evidence(task_repo, tmp_path):
    flow = _run_flow(task_repo, tmp_path)
    assert isinstance(flow, VerifiedStudentFlow)
    assert flow.task_result.exit_code == 0
    assert flow.evidence.disposition == "current"
    assert flow.evidence.checks_run == ("unit",)
    # evidence binds the exact exported bytes and the materialized head
    assert flow.evidence.matches(
        archive=flow.task_result.candidate_archive_bytes,
        candidate_id=flow.candidate.manifest.candidate_id,
        head_sha=flow.candidate.manifest.head_sha,
    )
    assert flow.evidence.head_sha == flow.candidate.manifest.head_sha
    assert flow.evidence.base_sha == flow.candidate.manifest.base_ref
    assert flow.evidence.manifest_sha256 == flow.trusted_manifest.digest()
    # the materialized candidate is a real commit at the recorded head
    materialized_head = _git(Path(flow.candidate.repo_path), "rev-parse", "HEAD")
    assert materialized_head == flow.candidate.manifest.head_sha
    # and the student file actually landed in the materialized tree
    assert (Path(flow.candidate.repo_path) / "solution.py").read_text() == "candidate source\n"


def test_flow_refuses_a_trusted_manifest_for_a_different_task(task_repo, tmp_path):
    _, base_sha = task_repo
    with pytest.raises(StudentVMBlocked, match="task"):
        _run_flow(task_repo, tmp_path,
                  trusted_manifest=_trusted_manifest(base_sha, task_id="task-2"))


def test_flow_refuses_trusted_manifest_base_sha_mismatch(task_repo, tmp_path):
    with pytest.raises(StudentVMBlocked, match="base_sha"):
        _run_flow(task_repo, tmp_path,
                  trusted_manifest=_trusted_manifest("c" * 40))


def test_flow_rejects_evidence_for_a_different_candidate(task_repo, tmp_path):
    """Even if the verifier misbehaves and returns evidence bound elsewhere,
    the flow must refuse it — the join is fail-closed."""
    from verifier_vm import VerifierEvidence

    class LyingVerifier:
        def verify(self, **kwargs):
            return VerifierEvidence(
                task_id="task-1", repository="example/project",
                base_sha=kwargs["manifest"].base_sha,
                candidate_id="candidate-someone-else", head_sha="b" * 40,
                manifest_sha256=kwargs["manifest"].digest(),
                archive_sha256="0" * 64, disposition="current",
                checks_run=("unit",), guest_id="scv-task-1-deadbeef",
            )

    with pytest.raises(StudentVMBlocked, match="evidence"):
        _run_flow(task_repo, tmp_path, verifier=LyingVerifier())


def test_real_guest_end_to_end_student_export_checked_by_clean_verifier(
    task_repo, tmp_path,
):
    pack_value = os.environ.get("SCHOOL_CORE_SMOLVM_PACK")
    if not pack_value:
        pytest.skip("set SCHOOL_CORE_SMOLVM_PACK to an operator-pinned local pack")
    pack = Path(pack_value).expanduser().resolve()
    if not pack.is_file():
        pytest.fail("SCHOOL_CORE_SMOLVM_PACK does not name a file")

    repo, base_sha = task_repo
    student_runner = SmolVmRunner(
        smolvm="smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(pack.read_bytes()).hexdigest(),
        quarantine_path=tmp_path / "quarantine.jsonl",
    )
    verifier = SmolVmVerifier(
        smolvm="smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(pack.read_bytes()).hexdigest(),
    )
    flow = dispatch_and_verify_student_task(
        student_runner=student_runner,
        request=_request(
            repo, base_sha, task_id="task-e2e",
            command=("sh", "-c", "printf 'written in guest\\n' > vm-result.txt"),
            timeout_seconds=60, memory_mib=512, storage_gib=1,
            max_output_bytes=4096, max_bundle_bytes=1024 * 1024,
        ),
        verifier=verifier,
        trusted_manifest=TrustedCheckManifest(
            task_id="task-e2e", repository="example/project", base_sha=base_sha,
            trusted_checks=(
                {"name": "artifact-file", "cmd": "test -f vm-result.txt", "cwd": "."},
                {"name": "artifact-content",
                 "cmd": 'test "$(cat vm-result.txt)" = "written in guest"', "cwd": "."},
            ),
        ),
        repo_path=repo,
        destination=tmp_path / "flow-out",
        candidate_store=CandidateStore(tmp_path / "candidates.json"),
        candidate_id="candidate-task-e2e",
        bead_id="school-core-sjv.7.17",
        issue_number=22,
        branch="candidate/task-e2e",
    )
    assert flow.evidence.disposition == "current"
    assert flow.evidence.checks_run == ("artifact-file", "artifact-content")
    assert flow.evidence.matches(
        archive=flow.task_result.candidate_archive_bytes,
        candidate_id=flow.candidate.manifest.candidate_id,
        head_sha=flow.candidate.manifest.head_sha,
    )
    # both guests torn down: no sc- or scv- machines remain
    names = subprocess.run(
        ["smolvm", "machine", "ls", "--quiet"], capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.splitlines()
    assert not any(name.startswith(("sc-", "scv-")) for name in names)
