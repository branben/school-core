import hashlib
import io
import os
import signal
import subprocess
import tarfile
from pathlib import Path

from candidate_manifest import CandidateStore

import pytest

from student_vm_runner import (
    StudentTaskRequest,
    StudentVMBlocked,
    SmolVmRunner,
    dispatch_student_task,
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
    _git(repo, "config", "user.email", "vm-test@example.invalid")
    _git(repo, "config", "user.name", "VM Test")
    (repo / "README.md").write_text("trusted base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _candidate_archive():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        source = b"candidate source\\n"
        member = tarfile.TarInfo("solution.py")
        member.size = len(source)
        tar.addfile(member, io.BytesIO(source))
    return archive.getvalue()


def _pack(tmp_path):
    pack = tmp_path / "student.smolmachine"
    data = b"test pack marker"
    pack.write_bytes(data)
    return pack, hashlib.sha256(data).hexdigest()


def _request(repo, base_sha, *, task_id="task-1", **overrides):
    values = {
        "task_id": task_id,
        "repository": "example/project",
        "repo_path": repo,
        "base_sha": base_sha,
        "task": {"prompt": "Inspect the repository and report."},
        "command": ("student-agent", "--task-file", "/workspace/task.json"),
    }
    values.update(overrides)
    return StudentTaskRequest(**values)


def test_real_smolvm_candidate_archive_round_trip(task_repo, tmp_path, monkeypatch):
    pack_value = os.environ.get("SCHOOL_CORE_SMOLVM_PACK")
    if not pack_value:
        pytest.skip("set SCHOOL_CORE_SMOLVM_PACK to an operator-pinned local pack")
    pack = Path(pack_value).expanduser().resolve()
    if not pack.is_file():
        pytest.fail("SCHOOL_CORE_SMOLVM_PACK does not name a file")

    repo, base_sha = task_repo
    machine_prefix = "sc-task-integration-"
    pack_digest = hashlib.sha256(pack.read_bytes()).hexdigest()
    runner = SmolVmRunner(
        smolvm="smolvm", pack_path=pack, pack_sha256=pack_digest,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )
    from student_vm_runner import materialize_and_verify_student_result

    def trusted_check(path, **_kwargs):
        source = Path(path) / "vm-result.txt"
        passed = source.is_file() and source.read_text() == "written in guest\n"
        return {
            "passed": passed,
            "ran": 1,
            "failures": [] if passed else [
                {"cmd": "vm-output-check", "exit": 1, "stderr": "guest output mismatch"}
            ],
        }

    verified = materialize_and_verify_student_result(
        dispatch_student_task(
            runner,
            _request(
                repo, base_sha, task_id="task-integration",
                command=("sh", "-c", "printf 'written in guest\\n' > vm-result.txt"),
                timeout_seconds=60, memory_mib=512, storage_gib=1,
                max_output_bytes=4096, max_bundle_bytes=1024 * 1024,
            ),
        ),
        repo_path=repo,
        destination=tmp_path / "candidate-repo",
        candidate_store=CandidateStore(tmp_path / "candidates.json"),
        expected_task_id="task-integration",
        expected_repository="example/project",
        expected_base_sha=base_sha,
        candidate_id="candidate-task-integration",
        bead_id="school-core-sjv.7.7",
        issue_number=17,
        branch="candidate/task-integration",
        runner=trusted_check,
        commands=[{"name": "vm-output-check", "cmd": "fixture", "cwd": "."}],
        max_archive_bytes=1024 * 1024,
        max_file_bytes=1024 * 1024,
        max_entries=100,
    )
    result = verified.task_result
    assert result.exit_code == 0
    assert (verified.candidate.repo_path / "vm-result.txt").read_text() == "written in guest\n"
    assert verified.evidence.disposition == "current"
    assert verified.evidence.candidate_id == verified.candidate.manifest.candidate_id
    assert verified.evidence.head_sha == verified.candidate.manifest.head_sha
    assert result.stderr == ""
    machine_names = subprocess.run(
        ["smolvm", "machine", "ls", "--quiet"], capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.splitlines()
    assert not any(name.startswith(machine_prefix) for name in machine_names)
    assert not (tmp_path / "quarantine.jsonl").exists()


def test_vm_task_uses_named_guest_minimal_host_env_and_deletes(task_repo, tmp_path, monkeypatch):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-secret-must-not-forward")

    candidate_archive = _candidate_archive()

    def fake_process(args, *, timeout_seconds, output_limit_bytes, env, **limits):
        calls.append((list(args), dict(env), dict(limits)))
        if args[1:3] == ["machine", "exec"]:
            return subprocess.CompletedProcess(args, 0, candidate_archive, b"student log")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fake_process, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    result = dispatch_student_task(runner, _request(repo, base_sha))

    commands = [args for args, _, _ in calls]
    create, start, execute, delete = commands
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "student log"
    assert result.candidate_archive == candidate_archive
    assert result.candidate_archive.startswith(b"solution.py")
    assert "--volume" in create and create[create.index("--volume") + 1].endswith(":/run/school-core-input:ro")
    assert "--volume" not in execute, "guest execution must not receive a writable host output mount"
    assert calls[2][2] == {
        "stdout_limit_bytes": 128 * 1024 * 1024,
        "stderr_limit_bytes": 256 * 1024,
    }
    assert result.guest_id == create[create.index("--name") + 1]
    assert create[:3] == ["/tools/smolvm", "machine", "create"]
    assert create[create.index("--from") + 1] == str(pack.resolve())
    assert create[create.index("--cpus") + 1] == "2"
    assert create[create.index("--mem") + 1] == "4096"
    assert create[create.index("--storage") + 1] == "8"
    mount = create[create.index("--volume") + 1]
    assert mount.endswith(":/run/school-core-input:ro")
    assert str(repo) not in create
    assert "--net" not in create and "--ssh-agent" not in create
    assert "--secret-env" not in create and "--secret-file" not in create
    assert "--docker-socket" not in create and "--allow-system-mounts" not in create
    assert start[:3] == ["/tools/smolvm", "machine", "start"]
    assert execute[:3] == ["/tools/smolvm", "machine", "exec"]
    assert execute[execute.index("--timeout") + 1] == "900s"
    assert delete[:3] == ["/tools/smolvm", "machine", "delete"]
    assert delete[-1] == "--force"
    assert calls[0][1]["PATH"]
    assert "MODEL_API_KEY" not in calls[0][1]
    assert not (tmp_path / "quarantine.jsonl").exists()


def test_bounded_process_keeps_stdout_and_stderr_separate_with_independent_limits():
    from student_vm_runner import _bounded_process

    result = _bounded_process(
        ["python3", "-c", "import os; os.write(1, b'archive'); os.write(2, b'log')"],
        timeout_seconds=5,
        output_limit_bytes=16,
        stdout_limit_bytes=16,
        stderr_limit_bytes=16,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 0
    assert result.stdout == b"archive"
    assert result.stderr == b"log"


def test_bounded_process_kills_child_when_stdout_exceeds_cap():
    from student_vm_runner import _bounded_process

    result = _bounded_process(
        ["python3", "-c", "import os; os.write(1, b'x' * 1000000)"],
        timeout_seconds=5,
        output_limit_bytes=64,
        stdout_limit_bytes=128,
        stderr_limit_bytes=64,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 125
    assert len(result.stdout) <= 128
    assert result.stderr == b"output limit exceeded"


def test_bounded_process_kills_child_when_stderr_exceeds_cap():
    from student_vm_runner import _bounded_process

    result = _bounded_process(
        ["python3", "-c", "import os; os.write(2, b'x' * 1000000)"],
        timeout_seconds=5,
        output_limit_bytes=64,
        stdout_limit_bytes=128,
        stderr_limit_bytes=64,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 125
    assert len(result.stderr) <= 64
    assert result.stderr == b"output limit exceeded"


def test_bundle_contains_only_selected_git_archive_and_task_metadata(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    captured = {}

    candidate_archive = _candidate_archive()

    def fake_process(args, **kwargs):
        if args[1:3] == ["machine", "create"]:
            staging = Path(args[args.index("--volume") + 1].split(":", 1)[0])
            captured["files"] = {p.name: p.read_bytes() for p in staging.iterdir()}
        stdout = candidate_archive if args[1:3] == ["machine", "exec"] else b"ok"
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fake_process, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    result = runner.execute(_request(repo, base_sha))
    assert result.candidate_archive == candidate_archive

    with tarfile.open(fileobj=io.BytesIO(captured["files"]["repository.tar"]), mode="r:") as archive:
        assert "README.md" in archive.getnames()
        assert archive.extractfile("README.md").read() == b"trusted base\n"
    metadata = captured["files"]["task.json"]
    assert b'"task_id":"task-1"' in metadata
    assert b'"repository":"example/project"' in metadata
    assert len(captured["files"]) == 2


def test_guest_script_redirects_command_stdout_away_from_the_archive_stream():
    from student_vm_runner import _guest_collection_script

    script = _guest_collection_script(("student-agent", "--task-file", "/workspace/task.json"))
    command_lines = [line for line in script.splitlines() if "student-agent" in line]
    assert len(command_lines) == 1
    assert command_lines[0].endswith("1>&2"), (
        "the student command's stdout must not mix into the candidate tar stream; "
        "even benign progress chatter would corrupt the archive checksum"
    )


def test_task_prompt_with_metacharacters_is_inert_json_data(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    captured = {}
    hostile_prompt = '"; rm -rf / #\n`id` $(id) && echo pwned'

    def fake_process(args, **kwargs):
        if args[1:3] == ["machine", "create"]:
            staging = Path(args[args.index("--volume") + 1].split(":", 1)[0])
            captured["task.json"] = (staging / "task.json").read_bytes()
        if args[1:3] == ["machine", "exec"]:
            captured["script"] = args[-1]
        stdout = _candidate_archive() if args[1:3] == ["machine", "exec"] else b"ok"
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fake_process, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    runner.execute(_request(repo, base_sha, task={"prompt": hostile_prompt}))

    import json as _json
    metadata = _json.loads(captured["task.json"])
    assert metadata["task"]["prompt"] == hostile_prompt, (
        "the prompt must round-trip as JSON string data, not be reinterpreted"
    )
    assert hostile_prompt not in captured["script"], (
        "the prompt must travel only inside task.json, never in the guest shell script"
    )


def test_oversized_repository_archive_is_stream_limited_before_guest_creation(
    task_repo, tmp_path, monkeypatch,
):
    repo, base_sha = task_repo
    (repo / "large.bin").write_bytes(b"x" * 8192)
    _git(repo, "add", "large.bin")
    _git(repo, "commit", "-qm", "large input")
    base_sha = _git(repo, "rev-parse", "HEAD")
    pack, pack_digest = _pack(tmp_path)
    calls = []
    original_run = subprocess.run

    def reject_buffered_archive(args, *positional, **kwargs):
        if "archive" in args:
            pytest.fail("git archive must not buffer an unbounded result via subprocess.run")
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr("student_vm_runner.subprocess.run", reject_buffered_archive)
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=lambda args, **kwargs: calls.append(list(args)),
        quarantine_path=tmp_path / "quarantine.jsonl",
    )

    with pytest.raises(StudentVMBlocked, match="archive exceeds configured size limit"):
        runner.execute(_request(repo, base_sha, max_bundle_bytes=1024))

    assert calls == [], "oversized repository input must fail before VM creation"


def test_start_failure_deletes_guest_and_does_not_fall_back(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []
    host_fallback_calls = []

    def fail_start(args, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["machine", "start"]:
            return subprocess.CompletedProcess(args, 17, b"", b"provider detail")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_start, quarantine_path=tmp_path / "quarantine.jsonl",
    )

    with pytest.raises(StudentVMBlocked, match="start failed"):
        dispatch_student_task(runner, _request(repo, base_sha))

    assert [call[1:3] for call in calls] == [
        ["machine", "create"], ["machine", "start"], ["machine", "delete"],
    ]
    assert all(call[0] == "/tools/smolvm" for call in calls)
    assert host_fallback_calls == []


def test_supervisor_timeout_deletes_guest_and_does_not_fall_back(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []
    host_fallback_calls = []

    def time_out_guest(args, *, timeout_seconds, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["machine", "exec"]:
            raise subprocess.TimeoutExpired(args, timeout_seconds)
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=time_out_guest, quarantine_path=tmp_path / "quarantine.jsonl",
    )

    with pytest.raises(StudentVMBlocked, match="supervisor timed out"):
        dispatch_student_task(runner, _request(repo, base_sha))

    assert [call[1:3] for call in calls] == [
        ["machine", "create"], ["machine", "start"],
        ["machine", "exec"], ["machine", "delete"],
    ]
    assert all(call[0] == "/tools/smolvm" for call in calls)
    assert host_fallback_calls == []
    assert not (tmp_path / "quarantine.jsonl").exists()


def test_guest_process_death_deletes_guest_and_does_not_fall_back(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []
    host_fallback_calls = []

    def guest_died(args, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["machine", "exec"]:
            return subprocess.CompletedProcess(args, -signal.SIGKILL, b"", b"")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=guest_died, quarantine_path=tmp_path / "quarantine.jsonl",
    )

    with pytest.raises(StudentVMBlocked, match="guest execution failed with exit status -9"):
        dispatch_student_task(runner, _request(repo, base_sha))

    assert [call[1:3] for call in calls] == [
        ["machine", "create"], ["machine", "start"],
        ["machine", "exec"], ["machine", "delete"],
    ]
    assert all(call[0] == "/tools/smolvm" for call in calls)
    assert host_fallback_calls == []


def test_invalid_guest_candidate_archive_blocks_and_deletes_guest(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []
    host_fallback_calls = []

    def invalid_archive(args, **kwargs):
        calls.append(list(args))
        stdout = b"not a tar archive" if args[1:3] == ["machine", "exec"] else b"ok"
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=invalid_archive, quarantine_path=tmp_path / "quarantine.jsonl",
    )

    with pytest.raises(StudentVMBlocked, match="malformed"):
        dispatch_student_task(runner, _request(repo, base_sha))

    assert [call[1:3] for call in calls] == [
        ["machine", "create"], ["machine", "start"],
        ["machine", "exec"], ["machine", "delete"],
    ]
    assert all(call[0] == "/tools/smolvm" for call in calls)
    assert host_fallback_calls == []


def test_vm_failure_deletes_guest_and_never_falls_back_to_host(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []

    def fail_exec(args, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["machine", "exec"]:
            return subprocess.CompletedProcess(args, 9, b"", b"secret-bearing error should not leak")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_exec, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked, match="guest execution failed") as error:
        dispatch_student_task(runner, _request(repo, base_sha))
    assert "secret-bearing" not in str(error.value)
    assert [call[1:3] for call in calls] == [
        ["machine", "create"], ["machine", "start"],
        ["machine", "exec"], ["machine", "delete"],
    ]
    assert not (tmp_path / "quarantine.jsonl").exists()


def test_invalid_request_is_rejected_before_runner_invocation(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    called = []
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=lambda *args, **kwargs: called.append(args),
    )
    with pytest.raises(ValueError, match="task_id"):
        _request(repo, base_sha, task_id="../escape")
    with pytest.raises(ValueError, match="command"):
        _request(repo, base_sha, command=("student-agent", "bad\x00arg"))
    with pytest.raises(ValueError, match="repository"):
        _request(repo, base_sha, repository="https://example.invalid/path")
    assert called == []
    assert runner is not None


def test_dirty_or_mismatched_target_fails_before_guest_creation(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    calls = []
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=lambda *args, **kwargs: calls.append(args),
    )
    (repo / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(StudentVMBlocked, match="clean"):
        runner.execute(_request(repo, base_sha))
    assert calls == []


def test_pack_hash_is_pinned_and_checked(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, digest = _pack(tmp_path)
    pack.write_bytes(b"tampered")
    called = []
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=digest,
        process_runner=lambda *args, **kwargs: called.append(args),
    )
    with pytest.raises(StudentVMBlocked, match="checksum mismatch"):
        runner.execute(_request(repo, base_sha))
    assert called == []


def test_delete_failure_is_persisted_as_private_cleanup_quarantine(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    quarantine = tmp_path / "quarantine.jsonl"
    calls = []
    host_fallback_calls = []

    def fail_delete(args, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["machine", "delete"]:
            return subprocess.CompletedProcess(args, 1, b"", b"provider details")
        stdout = _candidate_archive() if args[1:3] == ["machine", "exec"] else b"ok"
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_delete, quarantine_path=quarantine,
    )
    with pytest.raises(StudentVMBlocked, match="cleanup_quarantined"):
        dispatch_student_task(runner, _request(repo, base_sha))
    assert [call[1:3] for call in calls] == [
        ["machine", "create"], ["machine", "start"],
        ["machine", "exec"], ["machine", "delete"],
    ]
    assert all(call[0] == "/tools/smolvm" for call in calls)
    assert host_fallback_calls == []
    assert '"state":"cleanup_quarantined"' in quarantine.read_text()
    assert '"task_id":"task-1"' in quarantine.read_text()
    assert "provider details" not in quarantine.read_text()
    assert quarantine.stat().st_mode & 0o077 == 0


def test_guest_archive_paths_are_checked():
    from student_vm_runner import _validate_archive

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("../outside")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(StudentVMBlocked, match="traversal"):
        _validate_archive(archive.getvalue(), 16384)


def test_missing_smolvm_binary_is_a_blocked_result_not_an_os_error(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    runner = SmolVmRunner(
        smolvm=str(tmp_path / "no-such-smolvm"), pack_path=pack,
        pack_sha256=pack_digest, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha))


def test_missing_git_is_a_blocked_result_not_an_os_error(
    task_repo, tmp_path, monkeypatch,
):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha))


def test_process_layer_oserror_is_a_blocked_result(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)

    def broken_process(args, **kwargs):
        raise OSError("simulated fork failure")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=broken_process, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha))


def test_cleanup_failure_chains_the_original_execution_failure(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)

    def fail_exec_and_delete(args, **kwargs):
        if args[1:3] == ["machine", "exec"]:
            return subprocess.CompletedProcess(args, 1, b"", b"boom")
        if args[1:3] == ["machine", "delete"]:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_exec_and_delete,
        quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))
    message = str(excinfo.value)
    assert "cleanup_quarantined" in message
    assert "guest execution failed" in message, (
        "the cleanup error must preserve the original failure cause"
    )
    assert isinstance(excinfo.value.__cause__, StudentVMBlocked)
    assert "guest execution failed" in str(excinfo.value.__cause__)


class SpyRelay:
    def __init__(self):
        self.revoke_reasons = []

    def revoke(self, reason):
        self.revoke_reasons.append(reason)


def test_relay_capability_is_revoked_on_the_success_path(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)

    def ok_process(args, **kwargs):
        stdout = _candidate_archive() if args[1:3] == ["machine", "exec"] else b"ok"
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    relay = SpyRelay()
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=ok_process, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    runner.execute(_request(repo, base_sha), relay=relay)
    assert relay.revoke_reasons, "the relay must be revoked on the terminal success path"


def test_relay_capability_is_revoked_on_the_failure_path(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)

    def fail_exec(args, **kwargs):
        if args[1:3] == ["machine", "exec"]:
            return subprocess.CompletedProcess(args, 1, b"", b"boom")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    relay = SpyRelay()
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_exec, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha), relay=relay)
    assert relay.revoke_reasons, "the relay must be revoked even when the task blocks"


def test_relay_capability_is_revoked_on_cleanup_failure(task_repo, tmp_path):
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)

    def fail_delete(args, **kwargs):
        if args[1:3] == ["machine", "delete"]:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        stdout = _candidate_archive() if args[1:3] == ["machine", "exec"] else b"ok"
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    relay = SpyRelay()
    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_delete, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked, match="cleanup_quarantined"):
        runner.execute(_request(repo, base_sha), relay=relay)
    assert relay.revoke_reasons, "the relay must be revoked on the quarantine path too"


def test_no_host_execution_fallback_on_vm_failure(task_repo, tmp_path, monkeypatch):
    """A VM failure must never run the student command on the host.

    Positive control included: the spy must capture real host-side git
    plumbing, so an empty capture cannot pass vacuously.
    """
    repo, base_sha = task_repo
    pack, pack_digest = _pack(tmp_path)
    host_commands = []
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def spy_run(args, *pos, **kwargs):
        host_commands.append(list(args))
        return real_run(args, *pos, **kwargs)

    def spy_popen(args, *pos, **kwargs):
        host_commands.append(list(args))
        return real_popen(args, *pos, **kwargs)

    monkeypatch.setattr("student_vm_runner.subprocess.run", spy_run)
    monkeypatch.setattr("student_vm_runner.subprocess.Popen", spy_popen)

    guest_calls = []

    def fail_exec(args, **kwargs):
        guest_calls.append(list(args))
        if args[1:3] == ["machine", "exec"]:
            return subprocess.CompletedProcess(args, 9, b"", b"secret stderr must not leak")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    runner = SmolVmRunner(
        smolvm="/tools/smolvm", pack_path=pack, pack_sha256=pack_digest,
        process_runner=fail_exec, quarantine_path=tmp_path / "quarantine.jsonl",
    )
    with pytest.raises(StudentVMBlocked, match="guest execution failed"):
        dispatch_student_task(runner, _request(repo, base_sha))

    # Positive control: the spy captured the host-side work.
    assert host_commands, "spy captured nothing; the test would be vacuous"
    # The host ran only git plumbing — no shell, no student toolchain.
    assert all(cmd and cmd[0] == "git" for cmd in host_commands), host_commands
    # The student command itself never executed on the host.
    assert not any(
        "student-agent" in arg for cmd in host_commands for arg in cmd
    ), host_commands
    # The only non-git execution crossed the guest boundary via smolvm.
    assert [call[1:3] for call in guest_calls] == [
        ["machine", "create"], ["machine", "start"],
        ["machine", "exec"], ["machine", "delete"],
    ]
