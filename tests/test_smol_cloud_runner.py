"""Hermetic tests for the SmolMachines Cloud adapter.

Every test drives SmolCloudRunner through a fake transport. No test opens a
socket, reads a real credential, or creates a billable remote machine.
"""

import hashlib
import io
import json
import stat
import subprocess
import tarfile
import traceback

import pytest

from smol_cloud_runner import SmolCloudResponse, SmolCloudRunner
from student_vm_runner import StudentTaskRequest, StudentVMBlocked

IMAGE = "registry.example.invalid/school/student@sha256:" + "a" * 64
TOKEN = "smk_" + "b" * 32  # realistic shape so leak assertions are meaningful


# ---------------------------------------------------------------------------
# fixtures and fakes
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def task_repo(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "cloud-test@example.invalid")
    _git(repo, "config", "user.name", "Cloud Test")
    (repo / "README.md").write_text("trusted base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _candidate_tar(content=b"candidate source\n"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        member = tarfile.TarInfo("solution.py")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    return stream.getvalue()


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


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Scripted SmolMachines Cloud API. Records every call, never opens a socket."""

    def __init__(self, machine_id="m-abc123", *, candidate=None, exec_body=None):
        self.machine_id = machine_id
        self.calls = []
        self.failures = {}
        if exec_body is None:
            exec_body = {"exitCode": 0, "stdout": "", "stderr": "guest log\n", "durationMs": 12}
        self.create_response = SmolCloudResponse(
            201, json.dumps({"id": machine_id, "state": "created"}).encode()
        )
        self.start_response = SmolCloudResponse(200, b"{}")
        self.state_response = SmolCloudResponse(
            200,
            json.dumps({"id": machine_id, "state": "started", "ready": True}).encode(),
        )
        self.upload_response = SmolCloudResponse(204, b"")
        self.exec_response = SmolCloudResponse(200, json.dumps(exec_body).encode())
        self.download_response = SmolCloudResponse(
            200, candidate if candidate is not None else _candidate_tar()
        )
        self.delete_response = SmolCloudResponse(204, b"")

    def route(self, method, path):
        base = f"/v1/machines/{self.machine_id}"
        if method == "POST" and path == "/v1/machines":
            return "create"
        if method == "POST" and path == base + "/start":
            return "start"
        if method == "GET" and path == base:
            return "state"
        if method == "PUT":
            return "upload"
        if method == "POST" and "/exec" in path:
            return "exec"
        if method == "GET" and "/files/" in path:
            return "download"
        if method == "DELETE":
            return "delete"
        raise AssertionError(f"unexpected request: {method} {path}")

    def request(self, method, path, *, headers, body, timeout_seconds, response_limit_bytes):
        route = self.route(method, path)
        self.calls.append({
            "route": route,
            "method": method,
            "path": path,
            "headers": dict(headers),
            "body": body,
            "timeout_seconds": timeout_seconds,
            "response_limit_bytes": response_limit_bytes,
        })
        if route in self.failures:
            failure = self.failures[route]
            if isinstance(failure, Exception):
                raise failure
            return failure
        response = getattr(self, route + "_response")
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else response[0]
        return response

    def calls_for(self, route):
        return [call for call in self.calls if call["route"] == route]

    def routes(self):
        return [call["route"] for call in self.calls]


def _runner(tmp_path, transport, fake_clock, **overrides):
    kwargs = dict(
        image_reference=IMAGE,
        api_key=TOKEN,
        transport=transport,
        quarantine_path=tmp_path / "quarantine.jsonl",
        sleep=fake_clock.sleep,
        clock=fake_clock.clock,
    )
    kwargs.update(overrides)
    return SmolCloudRunner(**kwargs)


def _quarantine_records(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _rendered(error):
    """Exactly what a traceback printer would show (respects from-None)."""
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def _chain_texts(error):
    """Walk the rendered chain: __cause__ and unsuppressed __context__."""
    texts = []
    seen = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts.append(str(current))
        stack.append(getattr(current, "__cause__", None))
        if not getattr(current, "__suppress_context__", False):
            stack.append(getattr(current, "__context__", None))
    return "\n".join(texts)


class FakeRelay:
    def __init__(self):
        self.revocations = []

    def revoke(self, reason):
        self.revocations.append(reason)


# ---------------------------------------------------------------------------
# construction policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reference", [
    "registry.example.invalid/school/student:latest",
    "registry.example.invalid/school/student@sha256:" + "g" * 64,
    "registry.example.invalid/school/student@sha256:" + "a" * 63,
    "registry.example.invalid/school/student@sha256:" + "A" * 64,
    "",
    123,
])
def test_image_reference_must_be_digest_pinned(reference):
    with pytest.raises(ValueError, match="digest"):
        SmolCloudRunner(image_reference=reference, transport=FakeTransport())


def test_ceilings_must_be_positive_integers():
    for bad in (0, -1, True, 2.5, "3"):
        with pytest.raises(ValueError, match="positive integer"):
            SmolCloudRunner(
                image_reference=IMAGE, transport=FakeTransport(), max_cpus=bad
            )


@pytest.mark.parametrize("bad", ["", "oci", "IMAGE", "smol_machine", None, 123])
def test_source_type_must_be_a_known_delivery_kind(bad):
    with pytest.raises(ValueError, match="source_type"):
        SmolCloudRunner(
            image_reference=IMAGE, transport=FakeTransport(), source_type=bad
        )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_runs_full_lifecycle_and_always_deletes(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    result = runner.execute(_request(repo, base_sha, timeout_seconds=60))

    assert result.task_id == "task-1"
    assert result.repository == "example/project"
    assert result.base_sha == base_sha
    assert result.exit_code == 0
    assert result.guest_id.startswith("sc-task-1-")
    assert result.duration_ms >= 0
    assert result.candidate_archive_bytes == _candidate_tar()

    routes = transport.routes()
    # create -> start -> ready -> upload x2 -> exec -> download -> delete
    assert routes[0] == "create"
    assert routes[1] == "start"
    assert routes[2] == "state"
    assert routes[3] == "upload"
    assert routes[4] == "upload"
    assert routes[5] == "exec"
    assert routes[6] == "download"
    assert routes[7] == "delete"
    assert len(routes) == 8

    assert len(transport.calls_for("delete")) == 1
    delete_call = transport.calls_for("delete")[0]
    assert delete_call["path"].endswith("?includeUsage=true")


def test_create_payload_pins_blocked_network_ttl_ephemeral_and_resources(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    runner.execute(_request(repo, base_sha, timeout_seconds=60, cpus=2, memory_mib=1024, storage_gib=4))

    create_call = transport.calls_for("create")[0]
    payload = json.loads(create_call["body"])
    # The provider defaults network to OPEN when this is omitted; it must be
    # present and closed on every create, with no caller-controlled override.
    assert payload["network"] == {"mode": "blocked"}
    assert payload["ephemeral"] is True
    assert payload["ttlSeconds"] == 60 + 300
    assert payload["source"] == {"type": "image", "reference": IMAGE}
    assert "image" not in payload
    assert payload["resources"] == {"cpus": 2, "memoryMb": 1024, "diskGb": 4}
    assert payload["name"].startswith("sc-task-1-")


def test_smolmachine_source_is_delivered_without_a_guest_pull(task_repo, tmp_path):
    # A registry-backed OCI image cannot boot under blocked egress; the provider
    # must resolve a pre-packed smolmachine instead. The digest pin is preserved.
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock, source_type="smolmachine")

    runner.execute(_request(repo, base_sha))

    payload = json.loads(transport.calls_for("create")[0]["body"])
    assert payload["source"] == {"type": "smolmachine", "reference": IMAGE}
    assert payload["network"] == {"mode": "blocked"}


def test_bundle_sha256_binds_uploaded_repository_tar_and_task_json(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    result = runner.execute(_request(repo, base_sha))

    uploads = transport.calls_for("upload")
    repository_tar = uploads[0]["body"]
    task_json = uploads[1]["body"]
    with tarfile.open(fileobj=io.BytesIO(repository_tar), mode="r:") as archive:
        names = archive.getnames()
    assert "README.md" in names
    metadata = json.loads(task_json)
    assert metadata["task_id"] == "task-1"
    assert metadata["base_sha"] == base_sha
    assert metadata["command"] == ["student-agent", "--task-file", "/workspace/task.json"]
    expected = hashlib.sha256(repository_tar + task_json).hexdigest()
    assert result.bundle_sha256 == expected


def test_file_api_uses_path_suffix_routes_and_never_the_query_form(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    runner.execute(_request(repo, base_sha))

    file_calls = [c for c in transport.calls if c["route"] in ("upload", "download")]
    assert file_calls
    for call in file_calls:
        assert "/files/" in call["path"]
        assert "?" not in call["path"]
        assert "path=" not in call["path"]
    assert transport.calls_for("upload")[0]["path"] == (
        f"/v1/machines/{transport.machine_id}/files/tmp/school-core-input/repository.tar"
    )
    assert transport.calls_for("upload")[1]["path"] == (
        f"/v1/machines/{transport.machine_id}/files/tmp/school-core-input/task.json"
    )
    assert transport.calls_for("download")[0]["path"] == (
        f"/v1/machines/{transport.machine_id}/files/tmp/school-core-candidate.tar"
    )


def test_guest_script_shape_and_exec_payload(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    # tarfile pads every archive to 10240 bytes (RECORDSIZE), so the budget
    # must clear that floor even for a one-file candidate.
    runner.execute(_request(repo, base_sha, timeout_seconds=60, max_output_bytes=65536))

    exec_call = transport.calls_for("exec")[0]
    assert exec_call["path"].endswith("?output=text")
    payload = json.loads(exec_call["body"])
    assert payload["command"][:2] == ["sh", "-c"]
    assert payload["timeoutSeconds"] == 60
    script = payload["command"][2]
    assert script.startswith("set -eu")
    assert "tar -xf /tmp/school-core-input/repository.tar -C /workspace" in script
    assert "cp /tmp/school-core-input/task.json /workspace/task.json" in script
    assert "student-agent --task-file /workspace/task.json 1>&2" in script
    assert "rm -f /workspace/task.json" in script
    assert "head -c 65537 > /tmp/school-core-candidate.tar" in script
    assert 'test "$(wc -c < /tmp/school-core-candidate.tar)" -le 65536' in script


def test_guest_command_output_lands_in_stderr_not_stdout(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(exec_body={
        "exitCode": 0, "stdout": "", "stderr": "student chatter\n", "durationMs": 5,
    })
    runner = _runner(tmp_path, transport, fake_clock)

    result = runner.execute(_request(repo, base_sha))

    assert result.stdout == ""
    assert result.stderr == "student chatter\n"


# ---------------------------------------------------------------------------
# the 200-is-not-a-result trap
# ---------------------------------------------------------------------------


def test_exec_nonzero_exit_code_on_http_200_blocks_and_still_deletes(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(exec_body={"exitCode": 3, "stdout": "", "stderr": "boom\n"})
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    assert "exit status 3" in str(excinfo.value)
    assert TOKEN not in _rendered(excinfo.value)
    assert len(transport.calls_for("delete")) == 1


@pytest.mark.parametrize("body", [
    {"stdout": "", "stderr": ""},                       # exitCode missing
    {"exitCode": "0", "stdout": "", "stderr": ""},      # wrong type
    {"exitCode": True, "stdout": "", "stderr": ""},     # bool is not an int here
    {"exitCode": None, "stdout": "", "stderr": ""},
])
def test_exec_unusable_exit_code_blocks(task_repo, tmp_path, body):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(exec_body=body)
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="invalid exit code"):
        runner.execute(_request(repo, base_sha))

    assert len(transport.calls_for("delete")) == 1


# ---------------------------------------------------------------------------
# create outcomes: definitive rejection vs unknown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_definitive_create_rejection_quarantines_nothing(task_repo, tmp_path, status):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["create"] = SmolCloudResponse(status, b"{}")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    assert f"HTTP {status}" in str(excinfo.value)
    assert _quarantine_records(tmp_path) == []
    assert transport.calls_for("delete") == []


@pytest.mark.parametrize("status", [500, 503, 429])
def test_ambiguous_create_failure_records_unknown_outcome(task_repo, tmp_path, status):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["create"] = SmolCloudResponse(status, b"{}")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha))

    records = _quarantine_records(tmp_path)
    assert len(records) == 1
    assert records[0]["state"] == "cleanup_quarantined"
    assert records[0]["provider"] == "smol-cloud"
    assert records[0]["machine_id"] is None
    assert "create outcome unknown" in records[0]["reason"]
    assert transport.calls_for("delete") == []


def test_transport_exception_during_create_records_unknown_outcome(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["create"] = TimeoutError(f"urlopen timed out for {TOKEN}")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    assert "TimeoutError" in str(excinfo.value)
    assert TOKEN not in _rendered(excinfo.value)
    assert len(_quarantine_records(tmp_path)) == 1
    assert transport.calls_for("delete") == []


def test_create_with_invalid_machine_id_quarantines_exactly_once(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.create_response = SmolCloudResponse(201, json.dumps({"id": "bad id!!"}).encode())
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="invalid machine id"):
        runner.execute(_request(repo, base_sha))

    records = _quarantine_records(tmp_path)
    assert len(records) == 1
    assert "invalid id" in records[0]["reason"]
    assert transport.calls_for("delete") == []


# ---------------------------------------------------------------------------
# teardown is part of the contract
# ---------------------------------------------------------------------------


def test_delete_failure_quarantines_even_after_a_successful_task(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["delete"] = SmolCloudResponse(500, b"boom")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    # A successful result must not escape when teardown is unknown.
    assert "cleanup_quarantined" in str(excinfo.value)
    assert "delete returned HTTP 500" in _chain_texts(excinfo.value)
    records = _quarantine_records(tmp_path)
    assert len(records) == 1
    assert records[0]["machine_id"] == transport.machine_id


def test_delete_failure_after_exec_failure_keeps_the_root_cause_in_the_chain(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(exec_body={"exitCode": 7, "stdout": "", "stderr": "no\n"})
    transport.failures["delete"] = SmolCloudResponse(500, b"boom")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    chain = _chain_texts(excinfo.value)
    assert "cleanup_quarantined" in chain
    assert "exit status 7" in chain
    assert len(_quarantine_records(tmp_path)) == 1


def test_delete_settled_usage_is_validated(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.delete_response = SmolCloudResponse(200, json.dumps({"totalMicros": -5}).encode())
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    assert "cleanup_quarantined" in str(excinfo.value)
    assert "invalid settled usage" in _chain_texts(excinfo.value)


def test_foreign_delete_exception_is_quarantined_without_raw_text(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["delete"] = TimeoutError(f"urlopen timed out for {TOKEN}")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    assert "cleanup_quarantined" in str(excinfo.value)
    assert TOKEN not in _rendered(excinfo.value)
    records = _quarantine_records(tmp_path)
    assert len(records) == 1
    assert records[0]["reason"] == "delete failed: TimeoutError"


def test_valid_settled_usage_is_accepted(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.delete_response = SmolCloudResponse(
        200, json.dumps({"totalMicros": 2500, "totalUptimeSeconds": 12}).encode()
    )
    runner = _runner(tmp_path, transport, fake_clock)

    result = runner.execute(_request(repo, base_sha))

    assert result.exit_code == 0
    assert _quarantine_records(tmp_path) == []


def test_transport_exception_during_exec_still_deletes(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["exec"] = TimeoutError(f"timed out reaching {TOKEN}")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha))

    assert TOKEN not in _rendered(excinfo.value)
    assert len(transport.calls_for("delete")) == 1


# ---------------------------------------------------------------------------
# credentials and limits
# ---------------------------------------------------------------------------


def test_missing_credential_blocks_before_any_request(task_repo, tmp_path, monkeypatch):
    monkeypatch.delenv("SMOL_CLOUD_TOKEN", raising=False)
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock, api_key=None)

    with pytest.raises(StudentVMBlocked, match="SMOL_CLOUD_TOKEN"):
        runner.execute(_request(repo, base_sha))

    assert transport.calls == []


def test_env_credential_is_read_at_execute_time(task_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SMOL_CLOUD_TOKEN", TOKEN)
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock, api_key=None)

    runner.execute(_request(repo, base_sha))

    create_call = transport.calls_for("create")[0]
    assert create_call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_credential_with_control_characters_is_rejected(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock, api_key="smk_bad\nkey")

    with pytest.raises(StudentVMBlocked, match="invalid"):
        runner.execute(_request(repo, base_sha))

    assert transport.calls == []


def test_request_exceeding_runner_policy_blocks_before_any_request(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock, max_cpus=2)

    with pytest.raises(StudentVMBlocked, match="exceeds the runner policy limit"):
        runner.execute(_request(repo, base_sha, cpus=8))

    assert transport.calls == []


def test_oversized_bundle_blocks_before_create(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="archive exceeds its configured size limit"):
        runner.execute(_request(repo, base_sha, max_bundle_bytes=16))

    assert transport.calls == []
    assert _quarantine_records(tmp_path) == []


def test_oversized_task_metadata_blocks_before_create(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    request = _request(repo, base_sha, task={"prompt": "x" * (300 * 1024)})
    with pytest.raises(StudentVMBlocked, match="metadata exceeds its configured size limit"):
        runner.execute(request)

    assert transport.calls == []


def test_dirty_repo_blocks_before_create(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    (repo / "dirty.txt").write_text("uncommitted\n")
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="not clean"):
        runner.execute(_request(repo, base_sha))

    assert transport.calls == []


def test_head_mismatch_blocks_before_create(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, _base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="HEAD does not match base_sha"):
        runner.execute(_request(repo, "0" * 40))

    assert transport.calls == []


def test_oversized_guest_stdout_blocks(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(exec_body={
        "exitCode": 0, "stdout": "x" * (300 * 1024), "stderr": "",
    })
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="guest output exceeded"):
        runner.execute(_request(repo, base_sha))

    assert len(transport.calls_for("delete")) == 1


def test_oversized_candidate_download_blocks(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(candidate=_candidate_tar(b"x" * 4096))
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="candidate archive exceeded"):
        runner.execute(_request(repo, base_sha, max_output_bytes=64))

    assert len(transport.calls_for("delete")) == 1


def test_malformed_candidate_archive_blocks(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(candidate=b"not a tar archive at all")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="malformed candidate archive"):
        runner.execute(_request(repo, base_sha))

    assert len(transport.calls_for("delete")) == 1


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def test_readiness_polling_waits_for_ready(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    not_ready = SmolCloudResponse(
        200, json.dumps({"id": transport.machine_id, "state": "started", "ready": False}).encode()
    )
    transport.state_response = [not_ready, transport.state_response]
    runner = _runner(tmp_path, transport, fake_clock)

    result = runner.execute(_request(repo, base_sha))

    assert result.exit_code == 0
    assert len(transport.calls_for("state")) == 2
    assert fake_clock.sleeps == [1.0]


def test_readiness_timeout_blocks_and_deletes(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.state_response = SmolCloudResponse(
        200, json.dumps({"id": transport.machine_id, "state": "started", "ready": False}).encode()
    )
    runner = _runner(tmp_path, transport, fake_clock, readiness_timeout_seconds=3)

    with pytest.raises(StudentVMBlocked, match="did not become ready"):
        runner.execute(_request(repo, base_sha))

    assert len(transport.calls_for("state")) == 4
    assert fake_clock.sleeps == [1.0, 1.0, 1.0]
    assert len(transport.calls_for("delete")) == 1


def test_error_state_before_readiness_blocks_and_deletes(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.state_response = SmolCloudResponse(
        200, json.dumps({"id": transport.machine_id, "state": "error", "ready": False}).encode()
    )
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked, match="error state"):
        runner.execute(_request(repo, base_sha))

    assert len(transport.calls_for("delete")) == 1


# ---------------------------------------------------------------------------
# relay revocation and no-host-fallback
# ---------------------------------------------------------------------------


def test_relay_is_revoked_on_success(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)
    relay = FakeRelay()

    runner.execute(_request(repo, base_sha), relay=relay)

    assert relay.revocations == ["task terminal"]


def test_relay_is_revoked_on_failure_and_on_quarantine(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport(exec_body={"exitCode": 9, "stdout": "", "stderr": ""})
    transport.failures["delete"] = SmolCloudResponse(500, b"boom")
    runner = _runner(tmp_path, transport, fake_clock)
    relay = FakeRelay()

    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha), relay=relay)

    assert relay.revocations == ["task terminal"]


def test_relay_is_revoked_even_when_the_request_is_unusable(tmp_path):
    fake_clock = FakeClock()
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)
    relay = FakeRelay()

    with pytest.raises(StudentVMBlocked, match="StudentTaskRequest"):
        runner.execute("not a request", relay=relay)

    assert relay.revocations == ["task terminal"]
    assert transport.calls == []


def test_student_command_never_executes_on_the_host(task_repo, tmp_path, monkeypatch):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, fake_clock)

    real_popen = subprocess.Popen
    local_commands = []

    def recording_popen(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        local_commands.append(list(argv))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    runner.execute(_request(repo, base_sha))

    # Positive control: the student command really is in the guest script.
    exec_call = transport.calls_for("exec")[0]
    assert "student-agent" in json.loads(exec_call["body"])["command"][2]
    # Every local child is git (staging only); the student command is not one.
    assert local_commands
    for argv in local_commands:
        assert argv[0] == "git"
        assert "student-agent" not in " ".join(argv)


# ---------------------------------------------------------------------------
# quarantine record hygiene
# ---------------------------------------------------------------------------


def test_quarantine_record_is_private_parseable_and_secret_free(task_repo, tmp_path):
    fake_clock = FakeClock()
    repo, base_sha = task_repo
    transport = FakeTransport()
    transport.failures["delete"] = SmolCloudResponse(500, b"boom")
    runner = _runner(tmp_path, transport, fake_clock)

    with pytest.raises(StudentVMBlocked):
        runner.execute(_request(repo, base_sha))

    path = tmp_path / "quarantine.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert TOKEN not in text
    records = _quarantine_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["state"] == "cleanup_quarantined"
    assert record["provider"] == "smol-cloud"
    assert record["machine_name"].startswith("sc-task-1-")
    assert record["machine_id"] == transport.machine_id
    assert record["reason"].startswith("delete failed:")
    assert record["recorded_at"]
