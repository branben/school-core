"""Opt-in real-guest security probes for the disposable VM boundary.

Each probe runs against the operator-pinned local pack and is skipped unless
``SCHOOL_CORE_SMOLVM_PACK`` names that pack. These are boundary *measurements*,
not simulated contract tests: the guest is a real smolvm machine and the
assertions read what the guest could actually observe.

Probes (Phase 2 acceptance):
- host canary file and host env canary are unreadable inside the guest
- host credentials are not visible to the guest
- general network egress is denied without ``--net``
"""

import hashlib
import io
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

from candidate_manifest import CandidateStore  # noqa: F401  (import surface parity)

import pytest

from student_vm_runner import (
    SmolVmRunner,
    StudentTaskRequest,
    StudentVMBlocked,
    dispatch_student_task,
)

_MACHINE_PREFIX = "sc-"


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
    _git(repo, "config", "user.email", "vm-probe@example.invalid")
    _git(repo, "config", "user.name", "VM Probe")
    (repo / "README.md").write_text("trusted base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _runner(tmp_path):
    pack_value = os.environ.get("SCHOOL_CORE_SMOLVM_PACK")
    if not pack_value:
        pytest.skip("set SCHOOL_CORE_SMOLVM_PACK to an operator-pinned local pack")
    pack = Path(pack_value).expanduser().resolve()
    if not pack.is_file():
        pytest.fail("SCHOOL_CORE_SMOLVM_PACK does not name a file")
    return SmolVmRunner(
        smolvm="smolvm",
        pack_path=pack,
        pack_sha256=hashlib.sha256(pack.read_bytes()).hexdigest(),
        quarantine_path=tmp_path / "quarantine.jsonl",
    )


def _probe(runner, repo, base_sha, *, task_id: str, script: str,
           max_output_bytes: int = 1024 * 1024):
    """Run one probe script in the guest and return (result, extracted_files)."""
    result = dispatch_student_task(
        runner,
        StudentTaskRequest(
            task_id=task_id,
            repository="example/project",
            repo_path=repo,
            base_sha=base_sha,
            task={"prompt": "Probe the guest execution boundary."},
            command=("sh", "-c", script),
            timeout_seconds=60,
            memory_mib=512,
            storage_gib=1,
            max_output_bytes=max_output_bytes,
            max_bundle_bytes=1024 * 1024,
        ),
    )
    return result, _extract_files(result.candidate_archive)


def _extract_files(archive: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isreg():
                continue
            handle = tar.extractfile(member)
            assert handle is not None
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            files[name] = handle.read()
    return files


def _assert_guest_torn_down(tmp_path) -> None:
    names = subprocess.run(
        ["smolvm", "machine", "ls", "--quiet"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.splitlines()
    assert not any(name.startswith(_MACHINE_PREFIX) for name in names)
    assert not (tmp_path / "quarantine.jsonl").exists()


def test_guest_cannot_read_host_canary_file_or_env(task_repo, tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    repo, base_sha = task_repo

    file_token = "CANARY-FILE-" + hashlib.sha256(b"host-file-canary").hexdigest()[:16]
    env_token = "CANARY-ENV-" + hashlib.sha256(b"host-env-canary").hexdigest()[:16]
    canary_path = tmp_path / "host-canary.txt"
    canary_path.write_text(file_token + "\n")
    monkeypatch.setenv("SCHOOL_CORE_CANARY_ENV", env_token)

    script = (
        "if cat " + shlex.quote(str(canary_path)) + " > /workspace/canary-read.txt 2>/workspace/canary-read.err\n"
        "then echo READ_OK > /workspace/canary-report.txt\n"
        "else echo READ_DENIED > /workspace/canary-report.txt\n"
        "fi\n"
        "env > /workspace/env-report.txt 2>&1\n"
        "echo alive > /workspace/alive.txt\n"
    )
    result, files = _probe(runner, repo, base_sha, task_id="task-canary", script=script)

    assert files.get("alive.txt", b"").strip() == b"alive", "probe script did not run"
    assert files.get("canary-report.txt", b"").strip() == b"READ_DENIED"
    observed = result.candidate_archive + result.stderr.encode("utf-8", "replace")
    assert file_token.encode() not in observed, "host canary file leaked into the guest"
    assert env_token.encode() not in observed, "host env canary leaked into the guest"
    _assert_guest_torn_down(tmp_path)


def test_guest_sees_no_host_credentials(task_repo, tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    repo, base_sha = task_repo

    secrets = {
        "GITHUB_TOKEN": "ghp_" + hashlib.sha256(b"github").hexdigest()[:24],
        "AWS_SECRET_ACCESS_KEY": "aws-" + hashlib.sha256(b"aws").hexdigest()[:24],
        "OPENAI_API_KEY": "sk-" + hashlib.sha256(b"openai").hexdigest()[:24],
        "SSH_AUTH_SOCK": "/tmp/fake-ssh-agent-" + hashlib.sha256(b"ssh").hexdigest()[:12],
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)

    script = (
        "env > /workspace/env-report.txt 2>&1\n"
        "ls -la /root/.ssh > /workspace/ssh-report.txt 2>&1 || true\n"
        "echo alive > /workspace/alive.txt\n"
    )
    result, files = _probe(runner, repo, base_sha, task_id="task-creds", script=script)

    assert files.get("alive.txt", b"").strip() == b"alive", "probe script did not run"
    assert "env-report.txt" in files
    observed = result.candidate_archive + result.stderr.encode("utf-8", "replace")
    for key, value in secrets.items():
        assert value.encode() not in observed, f"{key} leaked into the guest"
    _assert_guest_torn_down(tmp_path)


def test_guest_stdout_chatter_does_not_corrupt_candidate_archive(task_repo, tmp_path):
    """A student agent printing benign progress to stdout must not poison the
    tar stream — the archive would fail its checksum and block the task."""
    runner = _runner(tmp_path)
    repo, base_sha = task_repo

    script = (
        "echo 'model loading... done'\n"  # benign stdout chatter
        "printf 'written in guest\\n' > vm-result.txt\n"
    )
    result, files = _probe(
        runner, repo, base_sha, task_id="task-chatter", script=script,
    )
    assert result.exit_code == 0
    assert files.get("vm-result.txt", b"") == b"written in guest\n"
    assert b"model loading" not in result.candidate_archive, (
        "command stdout must not mix into the candidate archive"
    )
    assert "model loading" in result.stderr, (
        "command stdout should be preserved as bounded log output"
    )
    _assert_guest_torn_down(tmp_path)


def test_guest_symlink_output_is_rejected(task_repo, tmp_path):
    """The guest cannot smuggle a symlink into the candidate archive."""
    runner = _runner(tmp_path)
    repo, base_sha = task_repo

    script = (
        "ln -s /etc/passwd vm-result.txt\n"
        "echo alive > alive.txt\n"
    )
    with pytest.raises(StudentVMBlocked, match="not a regular file"):
        _probe(runner, repo, base_sha, task_id="task-symlink", script=script)
    _assert_guest_torn_down(tmp_path)


def test_guest_oversized_output_is_rejected(task_repo, tmp_path):
    """Guest output beyond the configured cap blocks the task; nothing unbounded
    crosses back to the host."""
    runner = _runner(tmp_path)
    repo, base_sha = task_repo

    script = "dd if=/dev/zero of=big.bin bs=1024 count=512 2>/dev/null\n"
    with pytest.raises(StudentVMBlocked):
        _probe(
            runner, repo, base_sha, task_id="task-bigout", script=script,
            max_output_bytes=64 * 1024,
        )
    _assert_guest_torn_down(tmp_path)


def test_guest_has_no_network_egress(task_repo, tmp_path):
    runner = _runner(tmp_path)
    repo, base_sha = task_repo

    script = (
        "ls /sys/class/net > /workspace/interfaces.txt 2>&1\n"
        "cat /proc/net/route > /workspace/routes.txt 2>&1\n"
        "try() {\n"
        "  name=\"$1\"; shift\n"
        "  if \"$@\" > /workspace/egress-attempt.out 2>&1; then\n"
        "    echo \"$name OK\" >> /workspace/egress-report.txt\n"
        "  else\n"
        "    echo \"$name FAIL\" >> /workspace/egress-report.txt\n"
        "  fi\n"
        "}\n"
        ": > /workspace/egress-report.txt\n"
        "try nc nc -z -w 3 1.1.1.1 443\n"
        "try wget wget -T 5 -q -O /dev/null http://1.1.1.1\n"
        "try curl curl -m 5 -s -o /dev/null http://1.1.1.1\n"
        "echo alive > /workspace/alive.txt\n"
    )
    result, files = _probe(runner, repo, base_sha, task_id="task-egress", script=script)

    assert files.get("alive.txt", b"").strip() == b"alive", "probe script did not run"

    report = files.get("egress-report.txt", b"").decode("utf-8", "replace")
    assert "FAIL" in report, "no egress attempt was actually made"
    assert " OK" not in report, f"guest reached the network: {report!r}"

    # The pinned rootfs ships a routeless "dummy0" alongside "lo"; what matters
    # is that no real NIC (eth0/en0/...) appears and nothing routes off-host.
    interfaces = files.get("interfaces.txt", b"").decode("utf-8", "replace").split()
    unexpected = [name for name in interfaces if name not in {"lo", "dummy0"}]
    assert not unexpected, f"unexpected guest interfaces: {unexpected!r}"

    routes = files.get("routes.txt", b"").decode("utf-8", "replace").splitlines()
    destinations = [line.split()[1] for line in routes[1:] if len(line.split()) > 1]
    assert "00000000" not in destinations, f"guest has a default route: {routes!r}"

    _assert_guest_torn_down(tmp_path)
