"""Contract tests for trusted verification in a separate clean verifier VM.

Plan Phase 2 item 5: trusted checks run in their own fresh guest (never the
student VM, never the host) against the exact exported candidate. Verifier
evidence binds task ID, repository, base SHA, candidate ID/head SHA, trusted
manifest digest, and the checks actually run. A missing or unrunnable trusted
gate is a failure, never a pass or a silent skip.
"""

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from student_vm_runner import StudentVMBlocked
from verifier_vm import (
    TrustedCheckManifest,
    VerifierEvidence,
    SmolVmVerifier,
)


def _candidate_archive():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        source = b"candidate source\n"
        member = tarfile.TarInfo("solution.py")
        member.size = len(source)
        tar.addfile(member, io.BytesIO(source))
    return archive.getvalue()


class FakeProcess:
    """Scripted smolvm lifecycle for the verifier guest."""

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
        return type("CP", (), {"args": args, "returncode": rc,
                               "stdout": out, "stderr": err})()

    def subs(self):
        return [c[2] for c in self.calls]


def _manifest(**over):
    values = {
        "task_id": "task-1",
        "repository": "example/project",
        "base_sha": "a" * 40,
        "trusted_checks": (
            {"name": "unit", "cmd": "pytest -q", "cwd": "."},
        ),
    }
    values.update(over)
    return TrustedCheckManifest(**values)


def _run(verifier, *, manifest=None, archive=None, candidate_id="candidate-task-1",
         head_sha="b" * 40):
    return verifier.verify(
        manifest=manifest or _manifest(),
        candidate_archive=archive if archive is not None else _candidate_archive(),
        candidate_id=candidate_id,
        head_sha=head_sha,
    )


def test_real_verifier_guest_runs_trusted_checks_against_the_exported_candidate(
    tmp_path,
):
    pack_value = os.environ.get("SCHOOL_CORE_SMOLVM_PACK")
    if not pack_value:
        pytest.skip("set SCHOOL_CORE_SMOLVM_PACK to an operator-pinned local pack")
    pack = Path(pack_value).expanduser().resolve()
    if not pack.is_file():
        pytest.fail("SCHOOL_CORE_SMOLVM_PACK does not name a file")

    archive = _candidate_archive()
    manifest = _manifest(trusted_checks=(
        {"name": "file-present", "cmd": "test -f solution.py", "cwd": "."},
        {"name": "content", "cmd": 'test "$(cat solution.py)" = "candidate source"', "cwd": "."},
    ))
    verifier = SmolVmVerifier(
        smolvm="smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(pack.read_bytes()).hexdigest(),
    )
    evidence = verifier.verify(
        manifest=manifest, candidate_archive=archive,
        candidate_id="candidate-task-1", head_sha="b" * 40,
        timeout_seconds=60,
    )
    assert evidence.disposition == "current"
    assert evidence.checks_run == ("file-present", "content")
    assert evidence.matches(archive=archive, candidate_id="candidate-task-1", head_sha="b" * 40)
    assert not evidence.matches(archive=b"different bytes" + archive)

    # a failing trusted check must yield 'failed', not 'current'
    failing = _manifest(trusted_checks=(
        {"name": "must-fail", "cmd": "test -f no-such-file", "cwd": "."},
    ))
    evidence2 = verifier.verify(
        manifest=failing, candidate_archive=archive,
        candidate_id="candidate-task-1", head_sha="b" * 40,
        timeout_seconds=60,
    )
    assert evidence2.disposition == "failed"
    assert evidence2.checks_run == ("must-fail",)


# ------------------------------------------------------------ manifest ---

def test_manifest_digest_binds_the_exact_trusted_checks():
    m1 = _manifest()
    m2 = _manifest(trusted_checks=({"name": "unit", "cmd": "pytest -x", "cwd": "."},))
    assert m1.digest() == _manifest().digest(), "digest must be deterministic"
    assert m1.digest() != m2.digest(), "a changed command must change the digest"
    assert len(m1.digest()) == 64


def test_manifest_rejects_invalid_values():
    for over in (
        {"task_id": "Bad/Id"},
        {"repository": ""},
        {"base_sha": "abc"},
        {"base_sha": "A" * 40},
        {"trusted_checks": ()},
        {"trusted_checks": ({"name": "x"},)},  # missing cmd
        {"trusted_checks": ({"name": "x", "cmd": ""},)},
        {"trusted_checks": "pytest"},  # not a tuple
    ):
        with pytest.raises((ValueError, TypeError)):
            _manifest(**over)


# ------------------------------------------------------- fresh guest ---

def test_verification_runs_in_its_own_fresh_guest_never_the_student_vm(tmp_path):
    from pathlib import Path
    pack = Path(tmp_path) / "pack"
    pack.write_bytes(b"pack")
    process = FakeProcess()
    verifier = SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(), process_runner=process,
    )
    evidence = _run(verifier)
    create = process.calls[0]
    name = create[create.index("--name") + 1]
    assert name.startswith("scv-"), "verifier guests need their own namespace"
    assert "student-agent" not in " ".join(create)
    assert evidence.guest_id == name
    assert [c[2] for c in process.calls] == [
        "create", "start", "exec", "delete",
    ]


# --------------------------------------------------------- evidence ---

def test_evidence_binds_task_repo_base_candidate_head_and_manifest(tmp_path):
    from pathlib import Path
    pack = Path(tmp_path) / "pack"
    pack.write_bytes(b"pack")
    manifest = _manifest()
    archive = _candidate_archive()
    process = FakeProcess({
        "exec": (0, b"SCV-CHECK unit exit=0\n", b""),
    })
    verifier = SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(), process_runner=process,
    )
    evidence = _run(verifier, manifest=manifest, archive=archive,
                    candidate_id="candidate-task-1", head_sha="b" * 40)
    assert evidence.task_id == "task-1"
    assert evidence.repository == "example/project"
    assert evidence.base_sha == "a" * 40
    assert evidence.candidate_id == "candidate-task-1"
    assert evidence.head_sha == "b" * 40
    assert evidence.manifest_sha256 == manifest.digest()
    assert evidence.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert evidence.disposition == "current"
    assert evidence.checks_run == ("unit",)


def test_evidence_rejects_swapped_candidate_archive(tmp_path):
    from pathlib import Path
    pack = Path(tmp_path) / "pack"
    pack.write_bytes(b"pack")
    verifier = SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(), process_runner=FakeProcess(),
    )
    evidence = _run(verifier)
    swapped = VerifierEvidence(
        task_id=evidence.task_id, repository=evidence.repository,
        base_sha=evidence.base_sha, candidate_id=evidence.candidate_id,
        head_sha=evidence.head_sha, manifest_sha256=evidence.manifest_sha256,
        archive_sha256="0" * 64, disposition=evidence.disposition,
        checks_run=evidence.checks_run, guest_id=evidence.guest_id,
    )
    assert swapped.archive_sha256 != evidence.archive_sha256
    # the binding check must be possible from the evidence alone
    assert not swapped.matches(archive=_candidate_archive())


# --------------------------------------------- fail closed, no skip ---

def test_exec_failure_blocks_and_still_deletes_the_guest(tmp_path):
    from pathlib import Path
    pack = Path(tmp_path) / "pack"
    pack.write_bytes(b"pack")
    process = FakeProcess({"exec": (1, b"", b"boom")})
    verifier = SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(), process_runner=process,
    )
    with pytest.raises(StudentVMBlocked):
        _run(verifier)
    assert process.subs().count("delete") == 1


def test_create_failure_blocks_without_any_exec(tmp_path):
    from pathlib import Path
    pack = Path(tmp_path) / "pack"
    pack.write_bytes(b"pack")
    process = FakeProcess({"create": (1, b"", b"")})
    verifier = SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(), process_runner=process,
    )
    with pytest.raises(StudentVMBlocked):
        _run(verifier)
    assert "exec" not in process.subs()


def test_zero_declared_trusted_checks_is_rejected_not_a_pass():
    with pytest.raises(ValueError):
        _manifest(trusted_checks=())


def test_partial_check_execution_is_not_current(tmp_path):
    """If any declared check did not actually run, the disposition must not
    be 'current' — a missing or unrunnable trusted gate is a failure, never a
    pass or a silent skip."""
    from pathlib import Path
    pack = Path(tmp_path) / "pack"
    pack.write_bytes(b"pack")
    process = FakeProcess({
        # the guest reports only ONE of the two declared checks
        "exec": (0, b"SCV-CHECK ran exit=0\n", b""),
    })
    verifier = SmolVmVerifier(
        smolvm="/tools/smolvm", pack_path=pack,
        pack_sha256=hashlib.sha256(b"pack").hexdigest(), process_runner=process,
    )
    evidence = _run(verifier, manifest=_manifest(trusted_checks=(
        {"name": "ran", "cmd": "pytest -q", "cwd": "."},
        {"name": "unrunnable", "cmd": "no-such-binary", "cwd": "."},
    )))
    assert evidence.disposition != "current"
    assert evidence.checks_run == ("ran",), (
        "only the check with a guest-side marker may be recorded as run"
    )
