"""SmolMachines Cloud adapter for the provider-neutral student VM contract.

This adapter is intentionally not wired into production dispatch. Cloud calls
require an explicit operator credential and are not made by the unit tests.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

from student_vm_runner import (
    RelayCapability,
    StudentTaskRequest,
    StudentTaskResult,
    StudentVMBlocked,
    _repo_git,
    _validate_archive,
)

__all__ = ["SmolCloudRunner", "SmolCloudResponse", "SmolCloudTransport"]

API_BASE_URL = "https://api.smolmachines.com"
_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONTROL_TIMEOUT_SECONDS = 120
_JSON_RESPONSE_LIMIT = 1024 * 1024
_STDERR_LIMIT = 256 * 1024
_TASK_METADATA_LIMIT = 256 * 1024
_TTL_SETUP_GRACE_SECONDS = 300


@dataclass(frozen=True)
class SmolCloudResponse:
    status: int
    body: bytes


class _CloudApiStatusError(StudentVMBlocked):
    """Unaccepted API status; carries the status so callers can classify it."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class SmolCloudTransport(Protocol):
    """Narrow HTTP seam; production transport can only address the pinned API."""

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        response_limit_bytes: int,
    ) -> SmolCloudResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _UrllibSmolCloudTransport:
    """Stdlib HTTPS transport that rejects redirects and bounds response bytes."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        response_limit_bytes: int,
    ) -> SmolCloudResponse:
        request = urllib.request.Request(
            API_BASE_URL + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as error:
            try:
                payload = error.read(response_limit_bytes + 1)
            finally:
                error.close()
            return SmolCloudResponse(error.code, payload)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # Error details can contain URLs or request state. Keep only the
            # class, and suppress the chain so a traceback render cannot leak it.
            raise StudentVMBlocked(
                f"SmolMachines Cloud transport failed: {type(error).__name__}"
            ) from None
        try:
            payload = response.read(response_limit_bytes + 1)
            return SmolCloudResponse(response.status, payload)
        finally:
            response.close()


class SmolCloudRunner:
    """Runs one task in a remote disposable guest; every failure blocks.

    Egress is fixed to ``blocked``. There is no setting that lets task or repo
    data widen it. The constructor requires an operator-pinned OCI digest and
    bounded resource ceilings; provider credentials are read only at execute.
    """

    def __init__(
        self,
        *,
        image_reference: str,
        api_key: str | None = None,
        transport: SmolCloudTransport | None = None,
        quarantine_path: Path | str | None = None,
        max_cpus: int = 2,
        max_memory_mib: int = 4096,
        max_storage_gib: int = 8,
        max_timeout_seconds: int = 900,
        readiness_timeout_seconds: int = 120,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(image_reference, str) or not _IMAGE_DIGEST_RE.fullmatch(image_reference):
            raise ValueError("image_reference must be an OCI reference pinned by sha256 digest")
        for name, value in (
            ("max_cpus", max_cpus),
            ("max_memory_mib", max_memory_mib),
            ("max_storage_gib", max_storage_gib),
            ("max_timeout_seconds", max_timeout_seconds),
            ("readiness_timeout_seconds", readiness_timeout_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.image_reference = image_reference
        self._api_key = api_key
        self.transport = transport if transport is not None else _UrllibSmolCloudTransport()
        self.quarantine_path = Path(quarantine_path) if quarantine_path is not None else None
        self.max_cpus = max_cpus
        self.max_memory_mib = max_memory_mib
        self.max_storage_gib = max_storage_gib
        self.max_timeout_seconds = max_timeout_seconds
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._sleep = sleep
        self._clock = clock

    def execute(
        self,
        request: StudentTaskRequest,
        *,
        relay: RelayCapability | None = None,
    ) -> StudentTaskResult:
        """Run and tear down one task, revoking any relay on all terminal paths."""
        try:
            return self._execute(request)
        finally:
            if relay is not None:
                relay.revoke("task terminal")

    def _execute(self, request: StudentTaskRequest) -> StudentTaskResult:
        if not isinstance(request, StudentTaskRequest):
            raise StudentVMBlocked("request must be a StudentTaskRequest")
        self._validate_limits(request)
        token = self._credential()

        machine_name = f"sc-{request.task_id[:72]}-{uuid.uuid4().hex[:8]}"
        machine_id: str | None = None
        create_attempted = False
        created_at = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="school-core-smol-cloud-") as raw_stage:
                staging = Path(raw_stage)
                bundle_sha256, task_json = self._stage_bundle(request, staging)
                repository_tar = (staging / "repository.tar").read_bytes()
                headers = {"Authorization": f"Bearer {token}"}
                create_payload = {
                    "name": machine_name,
                    "source": {"type": "image", "reference": self.image_reference},
                    "resources": {
                        "cpus": request.cpus,
                        "memoryMb": request.memory_mib,
                        "diskGb": request.storage_gib,
                    },
                    "network": {"mode": "blocked"},
                    "ttlSeconds": request.timeout_seconds + _TTL_SETUP_GRACE_SECONDS,
                    "ephemeral": True,
                }

                create_attempted = True
                try:
                    created = self._json_request(
                        "POST",
                        "/v1/machines",
                        headers=headers,
                        payload=create_payload,
                        accepted_statuses=(201,),
                        timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
                    )
                except _CloudApiStatusError as error:
                    if error.status < 500 and error.status != 429:
                        # A complete 4xx rejection proves no machine exists to
                        # reconcile. Only unknown outcomes get a record.
                        create_attempted = False
                    raise
                raw_id = created.get("id")
                if not isinstance(raw_id, str) or not _MACHINE_ID_RE.fullmatch(raw_id):
                    self._quarantine(machine_name, None, "created machine returned an invalid id")
                    create_attempted = False
                    raise StudentVMBlocked("cloud create returned an invalid machine id")
                machine_id = raw_id

                self._json_request(
                    "POST",
                    f"/v1/machines/{quote(machine_id, safe='')}/start",
                    headers=headers,
                    payload={},
                    accepted_statuses=(200,),
                    timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
                )
                self._wait_until_ready(machine_id, headers)

                input_root = f"/v1/machines/{quote(machine_id, safe='')}/files"
                self._upload(
                    input_root + "/tmp/school-core-input/repository.tar",
                    repository_tar,
                    headers=headers,
                    accepted_statuses=(200, 204),
                )
                self._upload(
                    input_root + "/tmp/school-core-input/task.json",
                    task_json,
                    headers=headers,
                    accepted_statuses=(200, 204),
                )

                archive_path = "/tmp/school-core-candidate.tar"
                command = self._guest_script(request, archive_path)
                exec_result = self._json_request(
                    "POST",
                    f"/v1/machines/{quote(machine_id, safe='')}/exec?output=text",
                    headers=headers,
                    payload={
                        "command": ["sh", "-c", command],
                        "timeoutSeconds": request.timeout_seconds,
                    },
                    accepted_statuses=(200,),
                    timeout_seconds=request.timeout_seconds + _CONTROL_TIMEOUT_SECONDS,
                )
                exit_code = exec_result.get("exitCode")
                if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                    raise StudentVMBlocked("cloud exec returned an invalid exit code")
                if exit_code != 0:
                    raise StudentVMBlocked(f"cloud guest execution failed with exit status {exit_code}")
                stderr = exec_result.get("stderr", "")
                if not isinstance(stderr, str) or len(stderr.encode("utf-8")) > _STDERR_LIMIT:
                    raise StudentVMBlocked("cloud guest diagnostics exceeded the configured limit")
                stdout = exec_result.get("stdout", "")
                if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > _STDERR_LIMIT:
                    raise StudentVMBlocked("cloud guest output exceeded the configured limit")

                downloaded = self._download(
                    input_root + "/tmp/school-core-candidate.tar",
                    headers=headers,
                    max_bytes=request.max_output_bytes,
                )
                _validate_archive(downloaded, request.max_output_bytes)
                return StudentTaskResult(
                    task_id=request.task_id,
                    repository=request.repository,
                    base_sha=request.base_sha,
                    guest_id=machine_name,
                    exit_code=0,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=int((time.monotonic() - created_at) * 1000),
                    bundle_sha256=bundle_sha256,
                    candidate_archive=downloaded,
                )
        except StudentVMBlocked:
            raise
        except Exception as error:
            # Do not expose transport exception text: it can contain request
            # context. Preserve only its type and fail closed; suppress the
            # chain so a traceback render cannot leak it either.
            raise StudentVMBlocked(
                f"SmolMachines Cloud task blocked: {type(error).__name__}"
            ) from None
        finally:
            if machine_id is not None:
                try:
                    self._delete(machine_id, token)
                except Exception as error:
                    self._quarantine(machine_name, machine_id, f"delete failed: {type(error).__name__}")
                    # A successful task result must not escape when teardown is
                    # unknown. Keep cleanup failure as the visible terminal
                    # state. Chain only our own sanitized errors; a foreign
                    # exception's text can carry transport request state.
                    cause = error if isinstance(error, StudentVMBlocked) else None
                    raise StudentVMBlocked(
                        f"cleanup_quarantined: cloud machine delete failed for {machine_name}"
                    ) from cause
            elif create_attempted:
                # A transport timeout during create may have left a billable
                # remote machine; ttlSeconds/ephemeral are the provider-side
                # safety net, while this record enables operator reconciliation.
                self._quarantine(machine_name, None, "create outcome unknown; provider TTL is the fallback")

    def _validate_limits(self, request: StudentTaskRequest) -> None:
        limits = (
            ("cpus", request.cpus, self.max_cpus),
            ("memory_mib", request.memory_mib, self.max_memory_mib),
            ("storage_gib", request.storage_gib, self.max_storage_gib),
            ("timeout_seconds", request.timeout_seconds, self.max_timeout_seconds),
        )
        for name, actual, maximum in limits:
            if actual > maximum:
                raise StudentVMBlocked(f"requested {name} exceeds the runner policy limit")

    def _credential(self) -> str:
        token = self._api_key if self._api_key is not None else os.environ.get("SMOL_CLOUD_TOKEN")
        if not token:
            raise StudentVMBlocked("SMOL_CLOUD_TOKEN is required for hosted VM execution")
        if not isinstance(token, str) or "\r" in token or "\n" in token:
            raise StudentVMBlocked("SMOL_CLOUD_TOKEN is invalid")
        return token

    def _stage_bundle(self, request: StudentTaskRequest, staging: Path) -> tuple[str, bytes]:
        try:
            head = _repo_git(request.repo_path, "rev-parse", "HEAD")
            dirty = _repo_git(request.repo_path, "status", "--porcelain")
        except (OSError, subprocess.CalledProcessError) as error:
            raise StudentVMBlocked(f"target repo pre-flight failed: {type(error).__name__}") from error
        if head != request.base_sha:
            raise StudentVMBlocked("target repo HEAD does not match base_sha")
        if dirty:
            raise StudentVMBlocked("target repo is not clean")

        archive_path = staging / "repository.tar"
        process = subprocess.Popen(
            ["git", "-C", str(request.repo_path), "archive", "--format=tar", request.base_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise StudentVMBlocked("git archive pipe was not created")
        total = 0
        try:
            with archive_path.open("wb") as target:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > request.max_bundle_bytes:
                        process.kill()
                        raise StudentVMBlocked("repository archive exceeds its configured size limit")
                    target.write(chunk)
            try:
                return_code = process.wait(timeout=60)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise StudentVMBlocked("repository archive creation timed out") from error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        if return_code != 0:
            raise StudentVMBlocked("failed to build repository archive")

        metadata = {
            "task_id": request.task_id,
            "repository": request.repository,
            "base_sha": request.base_sha,
            "task": request.task,
            "command": list(request.command),
            "timeout_seconds": request.timeout_seconds,
        }
        try:
            task_json = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise StudentVMBlocked("task metadata is not valid bounded JSON data") from error
        if len(task_json) > _TASK_METADATA_LIMIT:
            raise StudentVMBlocked("task metadata exceeds its configured size limit")
        digest = sha256()
        with archive_path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(task_json)
        return digest.hexdigest(), task_json

    def _guest_script(self, request: StudentTaskRequest, archive_path: str) -> str:
        command = " ".join(shlex.quote(part) for part in request.command)
        input_dir = "/tmp/school-core-input"
        max_bytes = request.max_output_bytes
        # head stores at most max_bytes + 1, allowing a reliable oversize test
        # without filling guest disk with a maliciously large candidate tree.
        return "\n".join(
            (
                "set -eu",
                "mkdir -p /workspace",
                f"tar -xf {shlex.quote(input_dir + '/repository.tar')} -C /workspace",
                f"cp {shlex.quote(input_dir + '/task.json')} /workspace/task.json",
                "cd /workspace",
                f"{command} 1>&2",
                "rm -f /workspace/task.json",
                f"tar -cf - -C /workspace . | head -c {max_bytes + 1} > {shlex.quote(archive_path)}",
                f"test \"$(wc -c < {shlex.quote(archive_path)})\" -le {max_bytes}",
            )
        )

    def _wait_until_ready(self, machine_id: str, headers: dict[str, str]) -> None:
        deadline = self._clock() + self.readiness_timeout_seconds
        machine_path = f"/v1/machines/{quote(machine_id, safe='')}"
        while True:
            state = self._json_request(
                "GET",
                machine_path,
                headers=headers,
                payload=None,
                accepted_statuses=(200,),
                timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            )
            if state.get("state") == "error":
                raise StudentVMBlocked("cloud machine entered an error state before readiness")
            if state.get("ready") is True:
                return
            if self._clock() >= deadline:
                raise StudentVMBlocked("cloud machine did not become ready before the deadline")
            self._sleep(min(1.0, max(0.0, deadline - self._clock())))

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        accepted_statuses: tuple[int, ...],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request_headers = dict(headers)
        request_headers["Accept"] = "application/json"
        body = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self.transport.request(
            method,
            path,
            headers=request_headers,
            body=body,
            timeout_seconds=timeout_seconds,
            response_limit_bytes=_JSON_RESPONSE_LIMIT,
        )
        if len(response.body) > _JSON_RESPONSE_LIMIT:
            raise StudentVMBlocked("cloud API JSON response exceeded its configured size limit")
        if response.status not in accepted_statuses:
            raise _CloudApiStatusError(
                f"cloud API returned HTTP {response.status} for {method} {path.split('?')[0]}",
                response.status,
            )
        if not response.body:
            return {}
        try:
            parsed = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StudentVMBlocked("cloud API returned malformed JSON") from error
        if not isinstance(parsed, dict):
            raise StudentVMBlocked("cloud API returned an unexpected JSON shape")
        return parsed

    def _upload(
        self,
        path: str,
        data: bytes,
        *,
        headers: dict[str, str],
        accepted_statuses: tuple[int, ...],
    ) -> None:
        request_headers = dict(headers)
        request_headers["Content-Type"] = "application/octet-stream"
        response = self.transport.request(
            "PUT",
            path,
            headers=request_headers,
            body=data,
            timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            response_limit_bytes=4096,
        )
        if response.status not in accepted_statuses:
            raise StudentVMBlocked(f"cloud file upload returned HTTP {response.status}")
        if len(response.body) > 4096:
            raise StudentVMBlocked("cloud file upload returned an oversized response")

    def _download(self, path: str, *, headers: dict[str, str], max_bytes: int) -> bytes:
        response = self.transport.request(
            "GET",
            path,
            headers={**headers, "Accept": "application/octet-stream"},
            body=None,
            timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            response_limit_bytes=max_bytes,
        )
        if response.status != 200:
            raise StudentVMBlocked(f"cloud candidate download returned HTTP {response.status}")
        if len(response.body) > max_bytes:
            raise StudentVMBlocked("cloud candidate archive exceeded its configured size limit")
        return response.body

    def _delete(self, machine_id: str, token: str) -> None:
        response = self.transport.request(
            "DELETE",
            f"/v1/machines/{quote(machine_id, safe='')}?includeUsage=true",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            body=None,
            timeout_seconds=_CONTROL_TIMEOUT_SECONDS,
            response_limit_bytes=_JSON_RESPONSE_LIMIT,
        )
        if response.status not in (200, 204):
            raise StudentVMBlocked(f"cloud machine delete returned HTTP {response.status}")
        if len(response.body) > _JSON_RESPONSE_LIMIT:
            raise StudentVMBlocked("cloud machine delete response exceeded its configured size limit")
        # When enabled, the API returns the settled bill here. Validate a
        # returned amount, but allow deployments that still return 204.
        if response.body:
            try:
                usage = json.loads(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise StudentVMBlocked("cloud machine delete returned malformed usage JSON") from error
            if not isinstance(usage, dict):
                raise StudentVMBlocked("cloud machine delete returned an unexpected usage shape")
            amount = usage.get("totalMicros")
            if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool) or amount < 0):
                raise StudentVMBlocked("cloud machine delete returned invalid settled usage")

    def _quarantine(self, machine_name: str, machine_id: str | None, reason: str) -> None:
        if self.quarantine_path is None:
            return
        self.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "state": "cleanup_quarantined",
            "provider": "smol-cloud",
            "machine_name": machine_name,
            "machine_id": machine_id,
            "reason": reason,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        fd = os.open(self.quarantine_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
