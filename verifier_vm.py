"""Trusted verification in a separate clean verifier VM.

Plan Phase 2 item 5: the school's trusted checks run in their own fresh,
disposable guest — never the student VM (whose root the student held) and never
the host — against the exact exported candidate. Verifier evidence binds task
ID, repository, base SHA, candidate ID/head SHA, the trusted-manifest digest,
and the checks actually run. A missing or unrunnable trusted gate is a failure,
never a pass and never a silent skip.

The verifier guest has its own namespace (``scv-``) so it can never collide
with, or be confused for, a student guest (``sc-``).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from candidate_manifest import CandidateStore
from student_vm_runner import (
    MaterializedCandidate,
    ProcessRunner,
    RelayCapability,
    StudentTaskRequest,
    StudentTaskResult,
    StudentVMBlocked,
    _bounded_process,
    _norm_repository,
    _validate_archive,
    dispatch_student_task,
    import_guest_candidate_archive,
    materialize_guest_candidate,
)

__all__ = [
    "SmolVmVerifier",
    "TrustedCheckManifest",
    "VerifierEvidence",
    "VerifierSeam",
    "VerifiedStudentFlow",
    "dispatch_and_verify_student_task",
]

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,99}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE_64 = re.compile(r"^[0-9a-f]{64}$")

_SMOLVM_STDERR_LIMIT = 256 * 1024
_SMOLVM_CONTROL_TIMEOUT = 120
_CHECK_MARKER_PREFIX = "SCV-CHECK "


class TrustedCheckManifest:
    """The school's trusted check policy for one task. Operator-controlled;
    repository configuration can never widen it."""

    def __init__(
        self,
        *,
        task_id: str,
        repository: str,
        base_sha: str,
        trusted_checks: Any,
    ) -> None:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is invalid: {task_id!r}")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError(f"repository is invalid: {repository!r}")
        if (
            repository != repository.strip()
            or " " in repository
            or repository.startswith("/")
            or repository.endswith("/")
        ):
            raise ValueError(f"repository is invalid: {repository!r}")
        if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
            raise ValueError(f"base_sha is invalid: {base_sha!r}")
        if not isinstance(trusted_checks, tuple) or not trusted_checks:
            raise ValueError("trusted_checks must be a non-empty tuple")
        checks: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in trusted_checks:
            if not isinstance(entry, dict):
                raise ValueError(f"trusted check must be a dict, got {entry!r}")
            name = entry.get("name")
            cmd = entry.get("cmd")
            cwd = entry.get("cwd", ".")
            if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
                raise ValueError(f"trusted check name is invalid: {name!r}")
            if name in seen:
                raise ValueError(f"trusted check name is duplicated: {name!r}")
            if not isinstance(cmd, str) or not cmd or "\x00" in cmd:
                raise ValueError(f"trusted check cmd is invalid: {cmd!r}")
            if not isinstance(cwd, str) or not cwd:
                raise ValueError(f"trusted check cwd is invalid: {cwd!r}")
            if cwd.startswith("/") or ".." in cwd.split("/"):
                raise ValueError(f"trusted check cwd is invalid: {cwd!r}")
            seen.add(name)
            checks.append({"name": name, "cmd": cmd, "cwd": cwd})
        self.task_id = task_id
        self.repository = repository
        self.base_sha = base_sha
        self.trusted_checks = tuple(checks)

    def digest(self) -> str:
        payload = json.dumps(
            {
                "task_id": self.task_id,
                "repository": self.repository,
                "base_sha": self.base_sha,
                "trusted_checks": [
                    {"name": c["name"], "cmd": c["cmd"], "cwd": c["cwd"]}
                    for c in self.trusted_checks
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()


class VerifierEvidence:
    """Immutable, candidate-bound verification evidence."""

    def __init__(
        self,
        *,
        task_id: str,
        repository: str,
        base_sha: str,
        candidate_id: str,
        head_sha: str,
        manifest_sha256: str,
        archive_sha256: str,
        disposition: str,
        checks_run: Any,
        guest_id: str,
    ) -> None:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is invalid: {task_id!r}")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError(f"repository is invalid: {repository!r}")
        if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
            raise ValueError(f"base_sha is invalid: {base_sha!r}")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError(f"candidate_id is invalid: {candidate_id!r}")
        if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
            raise ValueError(f"head_sha is invalid: {head_sha!r}")
        if not isinstance(manifest_sha256, str) or not _SHA_RE_64.fullmatch(manifest_sha256):
            raise ValueError(f"manifest_sha256 is invalid: {manifest_sha256!r}")
        if not isinstance(archive_sha256, str) or not _SHA_RE_64.fullmatch(archive_sha256):
            raise ValueError(f"archive_sha256 is invalid: {archive_sha256!r}")
        if disposition not in ("current", "failed"):
            raise ValueError(f"disposition is invalid: {disposition!r}")
        if not isinstance(checks_run, tuple) or not all(
            isinstance(name, str) and _NAME_RE.fullmatch(name) for name in checks_run
        ):
            raise ValueError(f"checks_run is invalid: {checks_run!r}")
        if not isinstance(guest_id, str) or not _TASK_ID_RE.fullmatch(guest_id):
            raise ValueError(f"guest_id is invalid: {guest_id!r}")
        if not guest_id.startswith("scv-"):
            raise ValueError(f"guest_id must be a verifier guest: {guest_id!r}")
        self.task_id = task_id
        self.repository = repository
        self.base_sha = base_sha
        self.candidate_id = candidate_id
        self.head_sha = head_sha
        self.manifest_sha256 = manifest_sha256
        self.archive_sha256 = archive_sha256
        self.disposition = disposition
        self.checks_run = checks_run
        self.guest_id = guest_id

    def matches(
        self,
        *,
        archive: bytes | None = None,
        candidate_id: str | None = None,
        head_sha: str | None = None,
    ) -> bool:
        """Check this evidence against the exact exported candidate."""
        if archive is not None and sha256(bytes(archive)).hexdigest() != self.archive_sha256:
            return False
        if candidate_id is not None and candidate_id != self.candidate_id:
            return False
        if head_sha is not None and head_sha != self.head_sha:
            return False
        return True

    def __repr__(self) -> str:
        return (
            f"VerifierEvidence(task_id={self.task_id!r}, repository={self.repository!r}, "
            f"candidate_id={self.candidate_id!r}, head_sha={self.head_sha!r}, "
            f"disposition={self.disposition!r}, checks_run={self.checks_run!r})"
        )


def _verifier_collection_script(manifest: TrustedCheckManifest) -> str:
    """Guest-side: materialize the exact candidate, run each trusted check with
    its own output on stderr, and emit one marker line per check on stdout.

    Check output goes to stderr so a chatty check cannot corrupt or spoof the
    marker stream; stdout carries markers only."""
    lines = [
        "set -e",
        "mkdir -p /workspace",
        "tar -xf /run/school-core-verify-input/candidate.tar -C /workspace",
    ]
    for check in manifest.trusted_checks:
        target = "/workspace" if check["cwd"] == "." else f"/workspace/{check['cwd']}"
        lines.append(
            f"rc=0; ( cd {shlex.quote(target)} && sh -c {shlex.quote(check['cmd'])} ) 1>&2 || rc=$?"
        )
        lines.append(f"echo '{_CHECK_MARKER_PREFIX}{check['name']} exit='$rc")
    return "\n".join(lines) + "\n"


def _parse_check_markers(
    stdout: bytes, manifest: TrustedCheckManifest
) -> tuple[tuple[str, ...], str]:
    order = [check["name"] for check in manifest.trusted_checks]
    seen: dict[str, int] = {}
    for line in stdout.decode("utf-8", "replace").splitlines():
        if not line.startswith(_CHECK_MARKER_PREFIX):
            raise StudentVMBlocked("verifier output is malformed: unexpected stdout content")
        parts = line.split()
        if len(parts) != 3 or not parts[2].startswith("exit="):
            raise StudentVMBlocked("verifier output is malformed: bad marker line")
        name = parts[1]
        if name not in order or name in seen:
            raise StudentVMBlocked("verifier output is malformed: unknown or duplicate check")
        try:
            code = int(parts[2][len("exit="):])
        except ValueError:
            raise StudentVMBlocked("verifier output is malformed: bad exit code") from None
        if code < 0 or code > 255:
            raise StudentVMBlocked("verifier output is malformed: exit code out of range")
        seen[name] = code
    checks_run = tuple(name for name in order if name in seen)
    current = len(checks_run) == len(order) and all(seen[name] == 0 for name in order)
    return checks_run, ("current" if current else "failed")


class VerifierSeam(Protocol):
    """Structural seam for the verification step, so the flow can be tested
    against, and later wired to, any verifier that honors the contract."""

    def verify(
        self,
        *,
        manifest: TrustedCheckManifest,
        candidate_archive: bytes,
        candidate_id: str,
        head_sha: str,
        timeout_seconds: int = ...,
    ) -> VerifierEvidence: ...


class VerifiedStudentFlow:
    """The end-to-end local proof: one student guest run, exact export, a
    materialized candidate, and verifier evidence bound to that candidate."""

    def __init__(
        self,
        *,
        task_result: StudentTaskResult,
        candidate: MaterializedCandidate,
        evidence: VerifierEvidence,
        trusted_manifest: TrustedCheckManifest,
    ) -> None:
        if not isinstance(task_result, StudentTaskResult):
            raise ValueError(f"task_result is invalid: {type(task_result).__name__!r}")
        if not isinstance(candidate, MaterializedCandidate):
            raise ValueError(f"candidate is invalid: {type(candidate).__name__!r}")
        if not isinstance(evidence, VerifierEvidence):
            raise ValueError(f"evidence is invalid: {type(evidence).__name__!r}")
        if not isinstance(trusted_manifest, TrustedCheckManifest):
            raise ValueError(
                f"trusted_manifest is invalid: {type(trusted_manifest).__name__!r}"
            )
        self.task_result = task_result
        self.candidate = candidate
        self.evidence = evidence
        self.trusted_manifest = trusted_manifest

    def __repr__(self) -> str:
        return (
            f"VerifiedStudentFlow(candidate={self.candidate.manifest.candidate_id!r}, "
            f"head_sha={self.candidate.manifest.head_sha!r}, "
            f"disposition={self.evidence.disposition!r})"
        )


def dispatch_and_verify_student_task(
    *,
    student_runner: Any,
    request: StudentTaskRequest,
    verifier: VerifierSeam,
    trusted_manifest: TrustedCheckManifest,
    repo_path: Path | str,
    destination: Path | str,
    candidate_store: CandidateStore,
    candidate_id: str,
    bead_id: str,
    issue_number: int,
    branch: str,
    relay: RelayCapability | None = None,
    max_archive_bytes: int = 128 * 1024 * 1024,
    max_file_bytes: int = 32 * 1024 * 1024,
    max_entries: int = 10_000,
    verifier_timeout_seconds: int = 300,
) -> VerifiedStudentFlow:
    """Run one task end to end: student guest -> exact export -> materialize ->
    clean verifier guest -> candidate-bound evidence.

    Every join is fail-closed: the trusted manifest must belong to the task,
    and the verifier evidence must bind the exact exported bytes and the
    materialized head, or the flow blocks."""
    if not isinstance(request, StudentTaskRequest):
        raise ValueError("request must be a StudentTaskRequest")
    if not isinstance(trusted_manifest, TrustedCheckManifest):
        raise ValueError("trusted_manifest must be a TrustedCheckManifest")
    if not callable(getattr(verifier, "verify", None)):
        raise ValueError("verifier must provide a verify() method")

    # Join 1: the trusted check policy belongs to this task, not another.
    if trusted_manifest.task_id != request.task_id:
        raise StudentVMBlocked(
            f"trusted manifest task_id does not match the task: "
            f"{trusted_manifest.task_id!r} != {request.task_id!r}"
        )
    if trusted_manifest.repository != _norm_repository(request.repository):
        raise StudentVMBlocked(
            f"trusted manifest repository does not match the task: "
            f"{trusted_manifest.repository!r} != {request.repository!r}"
        )
    if trusted_manifest.base_sha != request.base_sha:
        raise StudentVMBlocked(
            f"trusted manifest base_sha does not match the task: "
            f"{trusted_manifest.base_sha!r} != {request.base_sha!r}"
        )

    result = dispatch_student_task(student_runner, request, relay=relay)
    if (
        result.task_id,
        result.repository,
        result.base_sha,
    ) != (
        request.task_id,
        request.repository,
        request.base_sha,
    ):
        raise StudentVMBlocked("student VM result identity does not match the task")
    if not result.candidate_archive:
        raise StudentVMBlocked("student VM result has no candidate archive")

    destination_path = Path(destination).expanduser().absolute()
    if destination_path.exists():
        raise StudentVMBlocked("candidate materialization destination already exists")

    artifact = import_guest_candidate_archive(
        result.candidate_archive_bytes,
        destination_path,
        task_id=result.task_id,
        repository=result.repository,
        base_sha=result.base_sha,
        max_archive_bytes=max_archive_bytes,
        max_file_bytes=max_file_bytes,
        max_entries=max_entries,
    )

    materialized = materialize_guest_candidate(
        artifact,
        repo_path=Path(repo_path),
        destination=destination_path / "candidate",
        candidate_store=candidate_store,
        expected_task_id=request.task_id,
        candidate_id=candidate_id,
        bead_id=bead_id,
        issue_number=issue_number,
        expected_repository=request.repository,
        expected_base_sha=request.base_sha,
        branch=branch,
    )

    evidence = verifier.verify(
        manifest=trusted_manifest,
        candidate_archive=result.candidate_archive_bytes,
        candidate_id=materialized.manifest.candidate_id,
        head_sha=materialized.manifest.head_sha,
        timeout_seconds=verifier_timeout_seconds,
    )

    # Join 2: the evidence must bind the exact exported bytes and the
    # materialized head. A misbehaving verifier cannot vouch for another
    # candidate.
    if not evidence.matches(
        archive=result.candidate_archive_bytes,
        candidate_id=materialized.manifest.candidate_id,
        head_sha=materialized.manifest.head_sha,
    ):
        raise StudentVMBlocked(
            "verifier evidence does not bind the exported candidate and materialized head"
        )
    if evidence.manifest_sha256 != trusted_manifest.digest():
        raise StudentVMBlocked(
            "verifier evidence does not bind the trusted manifest"
        )

    return VerifiedStudentFlow(
        task_result=result,
        candidate=materialized,
        evidence=evidence,
        trusted_manifest=trusted_manifest,
    )


class SmolVmVerifier:
    """Runs the school's trusted checks in a fresh verifier guest.

    Every lifecycle failure blocks the verification; there is no host-side
    check fallback. The student guest is never reused — the verifier creates,
    uses, and destroys its own machine."""

    def __init__(
        self,
        *,
        smolvm: str,
        pack_path: Path | str,
        pack_sha256: str,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.smolvm = smolvm
        self.pack_path = Path(pack_path).expanduser()
        self.pack_sha256 = pack_sha256
        self.process_runner = process_runner if process_runner is not None else _bounded_process

    def verify(
        self,
        *,
        manifest: TrustedCheckManifest,
        candidate_archive: bytes,
        candidate_id: str,
        head_sha: str,
        timeout_seconds: int = 300,
        max_archive_bytes: int = 128 * 1024 * 1024,
    ) -> VerifierEvidence:
        if not isinstance(manifest, TrustedCheckManifest):
            raise ValueError("manifest must be a TrustedCheckManifest")
        if not isinstance(candidate_archive, (bytes, bytearray, memoryview)):
            raise ValueError("candidate_archive must be bytes-like")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be a positive int, got {timeout_seconds!r}")
        archive = bytes(candidate_archive)
        if not archive:
            raise StudentVMBlocked("candidate archive is empty")
        _validate_archive(archive, max_archive_bytes)

        if not self.pack_path.is_file():
            raise StudentVMBlocked(f"guest pack is missing: {self.pack_path!s}")
        if sha256(self.pack_path.read_bytes()).hexdigest() != self.pack_sha256:
            raise StudentVMBlocked("guest pack checksum mismatch")

        guest_id = f"scv-{manifest.task_id}-{uuid.uuid4().hex[:8]}"
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "LANG")
            if os.environ.get(key)
        }
        env.setdefault("PATH", "/usr/bin:/bin")

        def run(args: list[str], *, timeout: float) -> Any:
            return self.process_runner(
                args,
                timeout_seconds=timeout,
                output_limit_bytes=65536 + _SMOLVM_STDERR_LIMIT,
                env=env,
                stdout_limit_bytes=65536,
                stderr_limit_bytes=_SMOLVM_STDERR_LIMIT,
            )

        staging = Path(tempfile.mkdtemp(prefix="school-core-verify-input-"))
        pending: StudentVMBlocked | None = None
        checks_run: tuple[str, ...] = ()
        disposition = "failed"
        created = False
        try:
            (staging / "candidate.tar").write_bytes(archive)
            create_args = [
                self.smolvm, "machine", "create",
                "--name", guest_id,
                "--from", str(self.pack_path.resolve()),
                "--cpus", "2",
                "--mem", "2048",
                "--storage", "2",
                "--volume", f"{staging}:/run/school-core-verify-input:ro",
            ]
            start_args = [self.smolvm, "machine", "start", "--name", guest_id]
            exec_args = [
                self.smolvm, "machine", "exec",
                "--name", guest_id,
                "--timeout", f"{timeout_seconds}s",
                "--", "sh", "-c", _verifier_collection_script(manifest),
            ]
            delete_args = [self.smolvm, "machine", "delete", "--name", guest_id, "--force"]

            try:
                create_result = run(create_args, timeout=_SMOLVM_CONTROL_TIMEOUT)
                if create_result.returncode != 0:
                    raise StudentVMBlocked(
                        f"verifier create failed with exit status {create_result.returncode}"
                    )
                created = True
                start_result = run(start_args, timeout=_SMOLVM_CONTROL_TIMEOUT)
                if start_result.returncode != 0:
                    raise StudentVMBlocked(
                        f"verifier start failed with exit status {start_result.returncode}"
                    )
                exec_result = run(exec_args, timeout=float(timeout_seconds))
                if exec_result.returncode != 0:
                    raise StudentVMBlocked(
                        f"guest verification failed with exit status {exec_result.returncode}"
                    )
                checks_run, disposition = _parse_check_markers(exec_result.stdout, manifest)
            except StudentVMBlocked as exc:
                pending = exc
            except subprocess.TimeoutExpired:
                pending = StudentVMBlocked("verifier supervisor timed out")
            except OSError as exc:
                pending = StudentVMBlocked(
                    f"verifier process spawn failed: {type(exc).__name__}; "
                    "no host fallback is permitted"
                )

            if created:
                delete_failed = False
                try:
                    delete_result = run(delete_args, timeout=_SMOLVM_CONTROL_TIMEOUT)
                    delete_failed = delete_result.returncode != 0
                except (subprocess.TimeoutExpired, OSError):
                    delete_failed = True
                if delete_failed:
                    if pending is not None:
                        raise StudentVMBlocked(
                            f"cleanup_quarantined: verifier guest delete failed for {guest_id}"
                            f" (after: {pending})"
                        ) from pending
                    raise StudentVMBlocked(
                        f"cleanup_quarantined: verifier guest delete failed for {guest_id}"
                    )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if pending is not None:
            raise pending

        return VerifierEvidence(
            task_id=manifest.task_id,
            repository=manifest.repository,
            base_sha=manifest.base_sha,
            candidate_id=candidate_id,
            head_sha=head_sha,
            manifest_sha256=manifest.digest(),
            archive_sha256=sha256(archive).hexdigest(),
            disposition=disposition,
            checks_run=checks_run,
            guest_id=guest_id,
        )
