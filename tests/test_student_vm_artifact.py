import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from candidate_gate import run_candidate_gate
from candidate_manifest import CandidateStore
from student_vm_runner import (
    StudentTaskResult,
    StudentVMBlocked,
    durable_record_from_verified_candidate,
    import_guest_candidate_archive,
    materialize_and_verify_student_result,
    DurableCandidateRecord,
    materialize_guest_candidate,
    materialized_candidate_from_durable_record,
)


def _archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            if kind == "file":
                content = value if isinstance(value, bytes) else value.encode()
                member.size = len(content)
                member.mode = 0o755 if name.endswith(".sh") else 0o644
                archive.addfile(member, io.BytesIO(content))
            elif kind == "dir":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = value
                archive.addfile(member)
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = value
                archive.addfile(member)
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
                archive.addfile(member)
            else:
                raise AssertionError(f"unknown fixture kind: {kind}")
    return stream.getvalue()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _import(data, destination, **limits):
    return import_guest_candidate_archive(
        data,
        destination,
        task_id="task-17",
        repository="example/project",
        base_sha="a" * 40,
        **limits,
    )


def _expected_file_text(relative, base_text="base state\n", solution_text="def answer(): return 42\n"):
    if relative == "README.md":
        return base_text
    if relative == "solution.py":
        return solution_text
    raise AssertionError(f"unexpected relative path in fixture: {relative}")


def test_guest_archive_import_becomes_exact_candidate_and_verifier_evidence(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    guest_archive = _archive([
        ("README.md", "file", "base state\n"),
        ("solution.py", "file", "def answer(): return 42\n"),
    ])
    imported = tmp_path / "imported"
    artifact = import_guest_candidate_archive(
        guest_archive, imported, task_id="task-17",
        repository="example/project", base_sha=base_sha,
    )
    store = CandidateStore(tmp_path / "candidates.json")

    materialized = materialize_guest_candidate(
        artifact, repo_path=base, destination=tmp_path / "candidate-repo",
        candidate_store=store, expected_task_id="task-17",
        candidate_id="candidate-task-17",
        bead_id="school-core-sjv.7", issue_number=17,
        expected_repository="example/project", expected_base_sha=base_sha,
        branch="candidate/task-17",
    )
    candidate = materialized.repo_path
    manifest = materialized.manifest
    commands = [{"name": "candidate-content", "cmd": "trusted-test", "cwd": "."}]
    observed = []

    def trusted_test(path, **_kwargs):
        observed.append((path, _git(path, "rev-parse", "HEAD")))
        source = (Path(path) / "solution.py").read_text()
        return {
            "passed": source == "def answer(): return 42\n",
            "ran": 1,
            "failures": [] if source == "def answer(): return 42\n" else [
                {"cmd": "trusted-test", "exit": 1, "stderr": "candidate mismatch"}
            ],
        }

    evidence = run_candidate_gate(
        repo_path=candidate, store=store, manifest=manifest,
        runner=trusted_test, commands=commands,
    )

    assert artifact.task_id == "task-17"
    assert manifest.repository == "example/project"
    assert manifest.base_sha == base_sha
    assert manifest.head_sha == observed[0][1]
    assert evidence.candidate_id == manifest.candidate_id
    assert evidence.head_sha == manifest.head_sha
    assert evidence.disposition == "current"
    assert observed == [(candidate, manifest.head_sha)]

    for relative in artifact.files:
        assert (candidate / relative).read_text() == _expected_file_text(relative)


def test_durable_record_snaps_identity_and_evidence(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    guest_archive = _archive([
        ("README.md", "file", "base state\n"),
        ("solution.py", "file", "def answer(): return 42\n"),
    ])
    result = StudentTaskResult(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        guest_id="guest-task-17", exit_code=0, stdout="", stderr="student report",
        duration_ms=12, bundle_sha256="d" * 64, candidate_archive=guest_archive,
    )
    store = CandidateStore(tmp_path / "candidates.json")

    def trusted_test(path, **_kwargs):
        head = _git(path, "rev-parse", "HEAD")
        passed = (Path(path) / "solution.py").read_text() == "def answer(): return 42\n"
        return {
            "passed": passed, "ran": 1,
            "failures": [] if passed else [{"cmd": "trusted", "exit": 1, "stderr": "bad"}],
        }

    verified = materialize_and_verify_student_result(
        result, repo_path=base, destination=tmp_path / "candidate",
        candidate_store=store, expected_task_id="task-17",
        expected_repository="example/project", expected_base_sha=base_sha,
        candidate_id="candidate-task-17", bead_id="school-core-sjv.7", issue_number=17,
        branch="candidate/task-17", runner=trusted_test,
        commands=[{"name": "trusted", "cmd": "trusted", "cwd": "."}],
    )
    record = DurableCandidateRecord(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        candidate_id=verified.candidate.manifest.candidate_id, head_sha=verified.candidate.manifest.head_sha,
        disposition="current", checks=verified.evidence.checks,
        runner=verified.evidence.toolchain.get("runner", "unknown"),
    )

    assert record.task_id == "task-17"
    assert record.repository == "example/project"
    assert record.base_sha == base_sha
    assert record.candidate_id == verified.candidate.manifest.candidate_id
    assert record.head_sha == verified.candidate.manifest.head_sha
    assert record.disposition == "current"
    assert record.checks
    assert record.runner
    assert record.candidate_id == verified.evidence.candidate_id
    assert record.head_sha == verified.evidence.head_sha


def test_materialized_candidate_rehydrates_from_durable_record(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    solved = import_guest_candidate_archive(
        _archive([
            ("README.md", "file", "base state\n"),
            ("solution.py", "file", "def answer(): return 42\n"),
        ]),
        tmp_path / "imported", task_id="task-17", repository="example/project",
        base_sha=base_sha,
    )
    manifest = materialize_guest_candidate(
        solved, repo_path=base, destination=tmp_path / "candidate",
        candidate_store=CandidateStore(tmp_path / "candidates.json"),
        expected_task_id="task-17", candidate_id="candidate-rehydrate-17",
        bead_id="school-core-sjv.7", issue_number=17,
        expected_repository="example/project", expected_base_sha=base_sha,
        branch="candidate/rehydrate-17",
    ).manifest

    record = DurableCandidateRecord(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
        disposition="current", checks=(),
        runner="test-runner",
    )
    replay_store = CandidateStore(tmp_path / "candidates-replay.json")
    replay = materialized_candidate_from_durable_record(
        record, artifact=solved, repo_path=base, destination=tmp_path / "replay",
        candidate_store=replay_store,
        bead_id="school-core-sjv.7", issue_number=17,
        branch="candidate/rehydrate-17",
    )

    assert replay.manifest.candidate_id == record.candidate_id
    assert replay.manifest.base_sha == record.base_sha
    assert replay.manifest.repository == record.repository

    replay_head = _git(replay.repo_path, "rev-parse", "HEAD")
    replay_base = _git(replay.repo_path, "rev-parse", "HEAD~1")

    assert replay_head == record.head_sha
    assert replay_base == record.base_sha
    assert (replay.repo_path / "README.md").read_text() == "base state\n"
    assert (replay.repo_path / "solution.py").read_text() == "def answer(): return 42\n"

    assert replay.manifest.head_sha == record.head_sha

    def trusted_test(path, **_kwargs):
        passed = (Path(path) / "solution.py").read_text() == "def answer(): return 42\n"
        return {
            "passed": passed, "ran": 1,
            "failures": [] if passed else [{"cmd": "trusted", "exit": 1, "stderr": "bad"}],
        }

    evidence = run_candidate_gate(
        repo_path=replay.repo_path, store=replay_store, manifest=replay.manifest,
        runner=trusted_test, commands=[{"name": "trusted", "cmd": "trusted", "cwd": "."}],
    )
    assert evidence.head_sha == record.head_sha
    assert evidence.disposition == "current"


def test_durable_record_binds_artifact_digest(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    guest_archive = _archive([
        ("README.md", "file", "base state\n"),
        ("solution.py", "file", "def answer(): return 42\n"),
    ])
    result = StudentTaskResult(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        guest_id="guest-task-17", exit_code=0, stdout="", stderr="student report",
        duration_ms=12, bundle_sha256="d" * 64, candidate_archive=guest_archive,
    )

    def trusted_test(path, **_kwargs):
        passed = (Path(path) / "solution.py").read_text() == "def answer(): return 42\n"
        return {"passed": passed, "ran": 1, "failures": []}

    verified = materialize_and_verify_student_result(
        result, repo_path=base, destination=tmp_path / "candidate",
        candidate_store=CandidateStore(tmp_path / "candidates.json"),
        expected_task_id="task-17", expected_repository="example/project",
        expected_base_sha=base_sha, candidate_id="candidate-task-17",
        bead_id="school-core-sjv.7", issue_number=17,
        branch="candidate/task-17", runner=trusted_test,
        commands=[{"name": "trusted", "cmd": "trusted", "cwd": "."}],
    )
    record = durable_record_from_verified_candidate(verified)

    assert record.archive_sha256 == verified.candidate.artifact.archive_sha256
    assert record.archive_sha256 != ""


def test_rehydration_rejects_swapped_artifact(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    solved = import_guest_candidate_archive(
        _archive([
            ("README.md", "file", "base state\n"),
            ("solution.py", "file", "def answer(): return 42\n"),
        ]),
        tmp_path / "imported", task_id="task-17", repository="example/project",
        base_sha=base_sha,
    )
    manifest = materialize_guest_candidate(
        solved, repo_path=base, destination=tmp_path / "candidate",
        candidate_store=CandidateStore(tmp_path / "candidates.json"),
        expected_task_id="task-17", candidate_id="candidate-rehydrate-17",
        bead_id="school-core-sjv.7", issue_number=17,
        expected_repository="example/project", expected_base_sha=base_sha,
        branch="candidate/rehydrate-17",
    ).manifest

    swapped = import_guest_candidate_archive(
        _archive([
            ("README.md", "file", "base state\n"),
            ("solution.py", "file", "def answer(): return 43\n"),
        ]),
        tmp_path / "imported-swapped", task_id="task-17", repository="example/project",
        base_sha=base_sha,
    )

    record = DurableCandidateRecord(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
        disposition="current", checks=(), runner="test-runner",
        archive_sha256=solved.archive_sha256,
    )
    with pytest.raises(StudentVMBlocked, match="archive_sha256"):
        materialized_candidate_from_durable_record(
            record, artifact=swapped, repo_path=base, destination=tmp_path / "replay",
            candidate_store=CandidateStore(tmp_path / "candidates-replay.json"),
            bead_id="school-core-sjv.7", issue_number=17,
            branch="candidate/rehydrate-17",
        )


def test_rehydration_rejects_changed_head(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    solved = import_guest_candidate_archive(
        _archive([
            ("README.md", "file", "base state\n"),
            ("solution.py", "file", "def answer(): return 42\n"),
        ]),
        tmp_path / "imported", task_id="task-17", repository="example/project",
        base_sha=base_sha,
    )
    wrong_head_record = DurableCandidateRecord(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        candidate_id="candidate-rehydrate-17", head_sha="b" * 40,
        disposition="current", checks=(),
        runner="test-runner",
    )
    with pytest.raises(StudentVMBlocked, match="manifest head_sha"):
        materialized_candidate_from_durable_record(
            wrong_head_record, artifact=solved,
            repo_path=base, destination=tmp_path / "replay-3",
            candidate_store=CandidateStore(tmp_path / "candidates-replay-3.json"),
            bead_id="school-core-sjv.7", issue_number=17,
            branch="candidate/rehydrate-17",
        )

    replay3 = tmp_path / "replay-3"
    assert replay3.exists()
    assert (replay3 / "README.md").read_text() == "base state\n"


def test_rehydration_rejects_reused_destination(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    solved = import_guest_candidate_archive(
        _archive([
            ("README.md", "file", "base state\n"),
            ("solution.py", "file", "def answer(): return 42\n"),
        ]),
        tmp_path / "imported", task_id="task-17", repository="example/project",
        base_sha=base_sha,
    )
    record = DurableCandidateRecord(
        task_id="task-17", repository="example/project", base_sha=base_sha,
        candidate_id="candidate-rehydrate-17", head_sha="b" * 40,
        disposition="current", checks=(),
        runner="test-runner",
    )
    destination = tmp_path / "replay"
    destination.mkdir()
    (destination / "keep.txt").write_text("leave")

    with pytest.raises(StudentVMBlocked, match="destination must be new"):
        materialized_candidate_from_durable_record(
            record, artifact=solved, repo_path=base, destination=destination,
            candidate_store=CandidateStore(tmp_path / "candidates-replay-4.json"),
            bead_id="school-core-sjv.7", issue_number=17,
            branch="candidate/rehydrate-17",
        )
    assert (destination / "keep.txt").read_text() == "leave"


def test_import_accepts_tracked_dotfiles_from_real_base(tmp_path):
    """A real `git archive` of a base that tracks dotfiles must import cleanly.

    Every real repository tracks `.gitignore`/`.gitattributes`; `git archive`
    always emits them and the guest returns the whole extracted tree
    (`tar -cf - -C /workspace .`). The importer must treat them as inert data,
    not as unsafe paths, and the materialized candidate must reproduce the base
    dotfile content. Regression guard for the SCH-18 dotfile-rejection defect.
    """
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _git(base, "config", "user.email", "artifact-test@example.invalid")
    _git(base, "config", "user.name", "Artifact Test")
    (base / ".gitignore").write_text("build/\n*.pyc\n")
    (base / ".gitattributes").write_text("*.py text eol=lf\n")
    (base / ".gitmodules").write_text(
        '[submodule "vendor"]\n\tpath = vendor\n\turl = https://example.invalid/vendor.git\n'
    )
    (base / "README.md").write_text("base state\n")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "base")
    base_sha = _git(base, "rev-parse", "HEAD")

    # Mirror the guest collection exactly: extract the real `git archive` of the
    # base (tracked dotfiles included) into /workspace, apply the student's edit,
    # then `tar -cf - -C /workspace .` — the same bytes the guest returns.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base_archive = subprocess.run(
        ["git", "-C", str(base), "archive", "--format=tar", base_sha],
        check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(base_archive), mode="r:") as archive:
        members = archive.getnames()
        archive.extractall(workspace)
    assert ".gitignore" in members
    assert ".gitattributes" in members
    assert ".gitmodules" in members
    (workspace / "solution.py").write_text("def answer(): return 42\n")

    archive_bytes = subprocess.run(
        ["tar", "-cf", "-", "-C", str(workspace), "."],
        check=True, capture_output=True,
    ).stdout

    artifact = import_guest_candidate_archive(
        archive_bytes, tmp_path / "imported", task_id="task-17",
        repository="example/project", base_sha=base_sha,
    )
    for dotfile in (".gitignore", ".gitattributes", ".gitmodules"):
        assert dotfile in artifact.files

    materialized = materialize_guest_candidate(
        artifact, repo_path=base, destination=tmp_path / "candidate",
        candidate_store=CandidateStore(tmp_path / "candidates.json"),
        expected_task_id="task-17", candidate_id="candidate-task-17",
        bead_id="school-core-sjv.7", issue_number=17,
        expected_repository="example/project", expected_base_sha=base_sha,
        branch="candidate/task-17",
    )
    candidate = materialized.repo_path

    # Materialized candidate reproduces the base dotfile content byte-for-byte.
    assert (candidate / ".gitignore").read_text() == "build/\n*.pyc\n"
    assert (candidate / ".gitattributes").read_text() == "*.py text eol=lf\n"
    assert (candidate / ".gitmodules").read_text().startswith('[submodule "vendor"]')
    # Worktree is clean and every dotfile is tracked (re-materialized from content,
    # no submodule init run).
    assert _git(candidate, "status", "--porcelain") == ""
    assert sorted(_git(candidate, "ls-files").splitlines()) == sorted(artifact.files)
