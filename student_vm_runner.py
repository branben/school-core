from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Generator, Protocol
from unittest.mock import MagicMock

from candidate_manifest import CandidateManifest, CandidateManifestError, CandidateStore, ValidationResult, create_candidate
from candidate_gate import GateEvidence, run_candidate_gate

__all__ = [
    "CandidateManifest",
    "CandidateManifestError",
    "CandidateStore",
    "DurableCandidateRecord",
    "GateEvidence",
    "GuestCandidateArtifact",
    "MaterializedCandidate",
    "SmolVmRunner",
    "StudentTaskRequest",
    "StudentTaskResult",
    "StudentVMBlocked",
    "StudentVMStartError",
    "ValidationResult",
    "VerifiedStudentCandidate",
    "create_candidate",
    "dispatch_student_task",
    "durable_record_from_verified_candidate",
    "import_guest_candidate_archive",
    "materialize_and_verify_student_result",
    "materialize_guest_candidate",
    "materialized_candidate_from_durable_record",
    "run_candidate_gate",
]


_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,99}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-\.]{0,199}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE_64 = re.compile(r"^[0-9a-f]{64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-./]{0,199}$")

# Fixed commit metadata so the same candidate tree rebuilt from a checkpoint
# lands on the identical recorded head SHA without rerunning the guest.
_FIXED_COMMIT_DATE = "2000-01-01T00:00:00 +0000"
_SMOLVM_STDERR_LIMIT = 256 * 1024
_SMOLVM_CONTROL_TIMEOUT = 120


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _tar_entry_names(archive_bytes: bytes) -> tuple[str, ...]:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
        names: list[str] = []
        for member in archive.getmembers():
            if not member.isreg() and not member.isdir():
                raise AssertionError(f"unexpected tar member type: {member.type!r}")
            names.append(member.name)
        return tuple(sorted(names))


def _guess_task_id_from_manifest_path(path: Path | str) -> str:
    match = re.search(
        r"/(?P<task_id>[a-z0-9][a-z0-9\-]{0,99})/candidates\.json$",
        str(path),
    )
    if match and _TASK_ID_RE.fullmatch(match.group("task_id")):
        return match.group("task_id")
    return f"task-{uuid.uuid4().hex[:8]}"


def _candidate_store_path(
    base: Path | str,
    *,
    task_id: str,
    repository: str,
) -> Path:
    safe_task = re.sub(r"[^a-z0-9\-]", "-", task_id.lower()).strip("-") or "task"
    safe_repo = re.sub(r"[^a-z0-9\-]", "-", repository.lower()).strip("-") or "project"
    return Path(base) / safe_task / safe_repo / "candidates.json"


def _norm_repository(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError(f"repository is empty: {value!r}")
    if "/" not in cleaned:
        if not _NAME_RE.fullmatch(cleaned):
            raise ValueError(f"repository is invalid: {value!r}")
    else:
        if not all(_NAME_RE.fullmatch(segment) for segment in cleaned.split("/")):
            raise ValueError(f"repository contains invalid segments: {value!r}")
    if cleaned.startswith(".") or cleaned.endswith("."):
        raise ValueError(f"repository should not start or end with a dot: {value!r}")
    if cleaned.startswith("/") or cleaned.endswith("/"):
        raise ValueError(f"repository should not start or end with a slash: {value!r}")
    return cleaned


def _norm_task_id(value: str) -> str:
    cleaned = value.strip().lower()
    if not _TASK_ID_RE.fullmatch(cleaned):
        raise ValueError(f"task id is invalid: {value!r}")
    return cleaned


class StudentVMBlocked(Exception):
    """Every unsafe or unexpected VM outcome is a hard stop, not a retry."""


class RelayCapability(Protocol):
    """Structural type for the task-scoped relay handle (model_relay.py).

    The runner only ever revokes it; it cannot call models itself."""

    def revoke(self, reason: str) -> None: ...


class StudentVMStartError(StudentVMBlocked):
    """The VM could not be created or was unusable before the student ran."""


@dataclass(frozen=True, eq=False)
class StudentTaskResult:
    task_id: str
    repository: str
    base_sha: str
    guest_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    bundle_sha256: str
    candidate_archive: bytes = b""
    _normalized: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError(f"task_id is invalid: {self.task_id!r}")
        if not isinstance(self.repository, str):
            raise ValueError(f"repository must be a str, got {type(self.repository).__name__!r}")
        if "/" not in self.repository:
            if not _NAME_RE.fullmatch(self.repository):
                raise ValueError(f"repository is invalid: {self.repository!r}")
        else:
            if not all(_NAME_RE.fullmatch(seg) for seg in self.repository.split("/")):
                raise ValueError(f"repository contains invalid segments: {self.repository!r}")
        if not isinstance(self.base_sha, str) or not _SHA_RE.fullmatch(self.base_sha):
            raise ValueError(f"base_sha is invalid: {self.base_sha!r}")
        if not isinstance(self.guest_id, str) or not _TASK_ID_RE.fullmatch(self.guest_id):
            raise ValueError(f"guest_id is invalid: {self.guest_id!r}")
        if not isinstance(self.exit_code, int) or self.exit_code < 0:
            raise ValueError(f"exit_code must be a non-negative int, got {self.exit_code!r}")
        if not isinstance(self.stdout, str):
            raise ValueError(f"stdout must be a str, got {type(self.stdout).__name__!r}")
        if not isinstance(self.stderr, str):
            raise ValueError(f"stderr must be a str, got {type(self.stderr).__name__!r}")
        if not isinstance(self.duration_ms, int) or self.duration_ms < 0:
            raise ValueError(f"duration_ms must be a non-negative int, got {self.duration_ms!r}")
        if not isinstance(self.bundle_sha256, str) or not _SHA_RE_64.fullmatch(self.bundle_sha256):
            raise ValueError(f"bundle_sha256 is invalid: {self.bundle_sha256!r}")
        if not isinstance(self.candidate_archive, (bytes, bytearray, memoryview)):
            raise ValueError(
                f"candidate_archive must be bytes-like, got {type(self.candidate_archive).__name__!r}"
            )

    @property
    def candidate_archive_bytes(self) -> bytes:
        return bytes(self.candidate_archive)


# GateEvidence is imported from candidate_gate so runner-produced evidence and
# gate-produced evidence are the same sealed type.


class DurableCandidateRecord:
    task_id: str
    repository: str
    base_sha: str
    candidate_id: str
    head_sha: str
    disposition: str
    checks: tuple[dict[str, Any], ...]
    runner: str
    archive_sha256: str

    def __init__(
        self,
        *,
        task_id: str,
        repository: str,
        base_sha: str,
        candidate_id: str,
        head_sha: str,
        disposition: str,
        checks: tuple[dict[str, Any], ...],
        runner: str,
        archive_sha256: str = "",
    ) -> None:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is invalid: {task_id!r}")
        if "/" not in repository:
            if not _NAME_RE.fullmatch(repository):
                raise ValueError(f"repository is invalid: {repository!r}")
        else:
            if not all(_NAME_RE.fullmatch(seg) for seg in repository.split("/")):
                raise ValueError(f"repository contains invalid segments: {repository!r}")
        if not _SHA_RE.fullmatch(base_sha):
            raise ValueError(f"base_sha is invalid: {base_sha!r}")
        if not _SHA_RE.fullmatch(candidate_id) and not candidate_id.startswith("candidate-"):
            raise ValueError(f"candidate_id must be a 40-char sha or a valid candidate id, got {candidate_id!r}")
        if not _SHA_RE.fullmatch(head_sha):
            raise ValueError(f"head_sha must be a 40-char sha, got {head_sha!r}")
        if disposition not in {"current", "superseded", "rejected"}:
            raise ValueError(f"disposition must be one of current, superseded, rejected, got {disposition!r}")
        if not isinstance(checks, tuple) or not all(isinstance(item, dict) for item in checks):
            raise ValueError(f"checks must be a tuple[dict, ...], got {type(checks).__name__!r}")
        if not isinstance(runner, str) or not runner:
            raise ValueError(f"runner must be a non-empty str, got {runner!r}")
        if not isinstance(archive_sha256, str) or (archive_sha256 and not _SHA_RE_64.fullmatch(archive_sha256)):
            raise ValueError(f"archive_sha256 must be empty or a 64-hex sha256, got {archive_sha256!r}")
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "base_sha", base_sha)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "head_sha", head_sha)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "runner", runner)
        object.__setattr__(self, "archive_sha256", archive_sha256)

    def __repr__(self) -> str:
        return (
            f"DurableCandidateRecord("
            f"task_id={self.task_id!r}, "
            f"repository={self.repository!r}, "
            f"base_sha={self.base_sha!r}, "
            f"candidate_id={self.candidate_id!r}, "
            f"head_sha={self.head_sha!r}, "
            f"disposition={self.disposition!r}, "
            f"checks={self.checks!r}, "
            f"runner={self.runner!r}, "
            f"archive_sha256={self.archive_sha256!r}"
            ")"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DurableCandidateRecord):
            return NotImplemented
        return (
            self.task_id == other.task_id
            and self.repository == other.repository
            and self.base_sha == other.base_sha
            and self.candidate_id == other.candidate_id
            and self.head_sha == other.head_sha
            and self.disposition == other.disposition
            and self.checks == other.checks
            and self.runner == other.runner
            and self.archive_sha256 == other.archive_sha256
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.task_id,
                self.repository,
                self.base_sha,
                self.candidate_id,
                self.head_sha,
                self.disposition,
                self.checks,
                self.runner,
                self.archive_sha256,
            )
        )


def durable_record_from_verified_candidate(verified: VerifiedStudentCandidate) -> DurableCandidateRecord:
    return DurableCandidateRecord(
        task_id=verified.task_result.task_id,
        repository=verified.task_result.repository,
        base_sha=verified.task_result.base_sha,
        candidate_id=verified.evidence.candidate_id,
        head_sha=verified.evidence.head_sha,
        disposition=verified.evidence.disposition,
        checks=verified.evidence.checks,
        runner=verified.evidence.toolchain.get("runner", "unknown"),
        archive_sha256=verified.candidate.artifact.archive_sha256,
    )


class GuestCandidateArtifact:
    task_id: str
    repository: str
    base_sha: str
    archive_sha256: str
    files: tuple[str, ...]
    file_sha256: tuple[tuple[str, str], ...]
    root: Path | None

    def __init__(
        self,
        *,
        task_id: str,
        repository: str,
        base_sha: str,
        archive_sha256: str,
        files: tuple[str, ...],
        file_sha256: tuple[tuple[str, str], ...],
        root: Path | None,
    ) -> None:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is invalid: {task_id!r}")
        if "/" not in repository:
            if not _NAME_RE.fullmatch(repository):
                raise ValueError(f"repository is invalid: {repository!r}")
        else:
            if not all(_NAME_RE.fullmatch(seg) for seg in repository.split("/")):
                raise ValueError(f"repository contains invalid segments: {repository!r}")
        if not _SHA_RE.fullmatch(base_sha):
            raise ValueError(f"base_sha is invalid: {base_sha!r}")
        if not _SHA_RE.fullmatch(archive_sha256) and not _SHA_RE_64.fullmatch(archive_sha256) and archive_sha256 != "":
            raise ValueError(f"archive_sha256 is invalid: {archive_sha256!r}")
        if not isinstance(files, tuple) or not all(isinstance(item, str) for item in files):
            raise ValueError(f"files must be a tuple[str, ...], got {type(files).__name__!r}")
        if not isinstance(file_sha256, tuple) or not all(
            isinstance(item, tuple) and len(item) == 2 and all(isinstance(part, str) for part in item)
            for item in file_sha256
        ):
            raise ValueError(f"file_sha256 must be a tuple[tuple[str, str], ...], got {type(file_sha256).__name__!r}")
        if root is not None and not isinstance(root, Path):
            raise ValueError(f"root must be None or Path, got {type(root).__name__!r}")
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "base_sha", base_sha)
        object.__setattr__(self, "archive_sha256", archive_sha256)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "file_sha256", file_sha256)
        object.__setattr__(self, "root", root)

    def __repr__(self) -> str:
        return (
            f"GuestCandidateArtifact("
            f"task_id={self.task_id!r}, "
            f"repository={self.repository!r}, "
            f"base_sha={self.base_sha!r}, "
            f"archive_sha256={self.archive_sha256!r}, "
            f"files={self.files!r}, "
            f"file_sha256={self.file_sha256!r}, "
            f"root={self.root!r}"
            ")"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GuestCandidateArtifact):
            return NotImplemented
        return (
            self.task_id == other.task_id
            and self.repository == other.repository
            and self.base_sha == other.base_sha
            and self.archive_sha256 == other.archive_sha256
            and self.files == other.files
            and self.file_sha256 == other.file_sha256
            and self.root == other.root
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.task_id,
                self.repository,
                self.base_sha,
                self.archive_sha256,
                self.files,
                self.file_sha256,
                self.root,
            )
        )


class MaterializedCandidate:
    artifact: GuestCandidateArtifact
    manifest: CandidateManifest
    repo_path: Path

    def __init__(
        self,
        *,
        artifact: GuestCandidateArtifact,
        manifest: CandidateManifest,
        repo_path: Path,
    ) -> None:
        if not isinstance(artifact, GuestCandidateArtifact):
            raise ValueError(f"artifact must be a GuestCandidateArtifact, got {type(artifact).__name__!r}")
        if not isinstance(manifest, CandidateManifest):
            raise ValueError(f"manifest must be a CandidateManifest, got {type(manifest).__name__!r}")
        if not isinstance(repo_path, Path):
            raise ValueError(f"repo_path must be a Path, got {type(repo_path).__name__!r}")
        object.__setattr__(self, "artifact", artifact)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "repo_path", repo_path)

    def __repr__(self) -> str:
        return f"MaterializedCandidate(artifact={self.artifact!r}, manifest={self.manifest!r}, repo_path={self.repo_path!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MaterializedCandidate):
            return NotImplemented
        return (
            self.artifact == other.artifact
            and self.manifest == other.manifest
            and self.repo_path == other.repo_path
        )

    def __hash__(self) -> int:
        return hash((self.artifact, self.manifest, self.repo_path))


class VerifiedStudentCandidate:
    task_result: StudentTaskResult
    candidate: MaterializedCandidate
    evidence: GateEvidence

    def __init__(
        self,
        *,
        task_result: StudentTaskResult,
        candidate: MaterializedCandidate,
        evidence: GateEvidence,
    ) -> None:
        if not isinstance(task_result, StudentTaskResult):
            raise ValueError(f"task_result must be a StudentTaskResult, got {type(task_result).__name__!r}")
        if not isinstance(candidate, MaterializedCandidate):
            raise ValueError(f"candidate must be a MaterializedCandidate, got {type(candidate).__name__!r}")
        if not isinstance(evidence, GateEvidence):
            raise ValueError(f"evidence must be a GateEvidence, got {type(evidence).__name__!r}")
        object.__setattr__(self, "task_result", task_result)
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "evidence", evidence)

    def __repr__(self) -> str:
        return f"VerifiedStudentCandidate(task_result={self.task_result!r}, candidate={self.candidate!r}, evidence={self.evidence!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VerifiedStudentCandidate):
            return NotImplemented
        return (
            self.task_result == other.task_result
            and self.candidate == other.candidate
            and self.evidence == other.evidence
        )

    def __hash__(self) -> int:
        return hash((self.task_result, self.candidate, self.evidence))


def import_guest_candidate_archive(
    archive_bytes: bytes,
    destination: Path,
    *,
    task_id: str,
    repository: str,
    base_sha: str,
    max_archive_bytes: int = 128 * 1024 * 1024,
    max_file_bytes: int = 32 * 1024 * 1024,
    max_entries: int = 10_000,
) -> GuestCandidateArtifact:
    if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
        raise StudentVMBlocked("archive_bytes must be bytes-like")
    if len(archive_bytes) > max_archive_bytes:
        raise StudentVMBlocked(f"archive too large: {len(archive_bytes)} > {max_archive_bytes}")
    if not archive_bytes:
        raise StudentVMBlocked("archive is empty")

    normalized_task_id = _norm_task_id(task_id)
    normalized_repository = _norm_repository(repository)
    if not _SHA_RE.fullmatch(base_sha):
        raise StudentVMBlocked(f"base_sha is invalid: {base_sha!r}")

    destination_path = destination.expanduser().absolute()
    if destination_path.exists() or destination_path.is_symlink():
        raise StudentVMBlocked("destination already exists")

    tmp_path = destination_path
    tmp_path.mkdir(parents=True, exist_ok=True)

    file_names: list[str] = []
    file_sha256: list[tuple[str, str]] = []
    extracted_root: Path | None = None

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.name or member.name.strip("/") == "":
                raise StudentVMBlocked("archive member has empty name")

            if member.name.startswith("/"):
                raise StudentVMBlocked(f"archive member has unsafe path: {member.name!r}")

            normalized_name = member.name.strip("/")
            while normalized_name.startswith("./"):
                normalized_name = normalized_name[2:]
            if not normalized_name:
                raise StudentVMBlocked("archive member has empty name after stripping")

            raw_parts = tuple(normalized_name.split("/"))
            if not raw_parts or any(part == "" for part in raw_parts):
                raise StudentVMBlocked(f"archive member has unsafe path: {member.name!r}")
            # ``..`` and ``.git`` are traversal/executable-VCS hazards and stay
            # blocked. Tracked dotfiles (``.gitignore``/``.gitattributes``/
            # ``.gitmodules``) are inert data: ``git archive`` of a real base
            # always emits them and the guest returns the whole extracted tree,
            # so rejecting them blocks every real repository. They are copied
            # back from content by ``git add --all`` and no submodule init is
            # ever run on the host, so admitting them is safe.
            if any(part == ".." or part == ".git" for part in raw_parts):
                raise StudentVMBlocked(f"archive member has unsafe path: {member.name!r}")

            relative_path = "/".join(raw_parts)

            if member.isdir():
                continue

            if not member.isreg():
                raise StudentVMBlocked(f"archive member is not a regular file: {member.name!r}")

            if member.size < 0:
                raise StudentVMBlocked(f"archive member has negative size: {member.name!r}")
            if member.size > max_file_bytes:
                raise StudentVMBlocked(f"archive member is too large: {member.name!r} ({member.size} bytes)")

            member_name = relative_path
            destination_file = tmp_path / member_name
            destination_file.parent.mkdir(parents=True, exist_ok=True)

            if destination_file.exists() or destination_file.is_symlink():
                raise StudentVMBlocked(f"archive member would overwrite: {member_name!r}")

            extracted = archive.extractfile(member)
            if extracted is None:
                raise StudentVMBlocked(f"archive member cannot be extracted: {member.name!r}")
            with extracted as source, destination_file.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)

            if destination_file.is_symlink() or not destination_file.is_file():
                raise StudentVMBlocked(f"extracted archive member is not a regular file: {member_name!r}")

            mode = member.mode
            if mode & 0o111:
                destination_file.chmod(0o755)
            else:
                destination_file.chmod(0o644)

            digest = sha256(destination_file.read_bytes()).hexdigest()
            file_names.append(member_name)
            file_sha256.append((member_name, digest))

            if extracted_root is None:
                extracted_root = tmp_path

    file_names_tuple = tuple(sorted(file_names))
    file_sha256_tuple = tuple(sorted(file_sha256))

    if not file_names_tuple:
        raise StudentVMBlocked("archive contains no files")

    if len(file_names_tuple) > max_entries:
        raise StudentVMBlocked(f"too many archive entries: {len(file_names_tuple)} > {max_entries}")

    archive_sha256 = sha256(archive_bytes).hexdigest()

    return GuestCandidateArtifact(
        task_id=normalized_task_id,
        repository=normalized_repository,
        base_sha=base_sha,
        archive_sha256=archive_sha256,
        files=file_names_tuple,
        file_sha256=file_sha256_tuple,
        root=extracted_root,
    )


def _norm_branch(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or not _BRANCH_RE.fullmatch(cleaned):
        raise StudentVMBlocked(f"branch is invalid: {value!r}")
    if ".." in cleaned or cleaned.endswith("/") or cleaned.endswith("."):
        raise StudentVMBlocked(f"branch is invalid: {value!r}")
    return cleaned


def _git_env() -> dict[str, str]:
    """Hermetic commit identity so identical trees always rebuild identical SHAs."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "School Core",
        "GIT_AUTHOR_EMAIL": "school-core@example.invalid",
        "GIT_COMMITTER_NAME": "School Core",
        "GIT_COMMITTER_EMAIL": "school-core@example.invalid",
        "GIT_AUTHOR_DATE": _FIXED_COMMIT_DATE,
        "GIT_COMMITTER_DATE": _FIXED_COMMIT_DATE,
    }


def _repo_git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return result.stdout.strip()


def _checkout_candidate_branch(cloned_repo: Path, branch: str, base_sha: str) -> None:
    _repo_git(cloned_repo, "checkout", "--detach", base_sha)
    if _repo_git(cloned_repo, "rev-parse", "HEAD") != base_sha:
        raise StudentVMBlocked("failed to detach at expected base_sha")
    _repo_git(cloned_repo, "branch", "-f", branch, base_sha)
    _repo_git(cloned_repo, "checkout", branch)
    if _repo_git(cloned_repo, "branch", "--show-current") != branch:
        raise StudentVMBlocked(f"failed to checkout branch: {branch!r}")


def _apply_artifact_and_commit(cloned_repo: Path, artifact: GuestCandidateArtifact, task_id: str) -> None:
    """Make the candidate tree exactly the artifact and commit it deterministically."""
    for child in cloned_repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    artifact_root = artifact.root
    if artifact_root is None:
        raise StudentVMBlocked("artifact has no extracted root to copy from")

    for file_name in artifact.files:
        source_path = artifact_root / file_name
        if not source_path.is_file():
            raise StudentVMBlocked(f"artifact file not found: {file_name!r}")
        destination_path = cloned_repo / file_name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        mode = source_path.stat().st_mode
        if mode & 0o111:
            destination_path.chmod(0o755)
        else:
            destination_path.chmod(0o644)

    _repo_git(cloned_repo, "add", "--all")
    staged_names = tuple(sorted(line for line in _repo_git(cloned_repo, "ls-files").splitlines() if line))
    if staged_names != tuple(sorted(artifact.files)):
        raise StudentVMBlocked("candidate worktree is not clean after copying artifact files")

    _repo_git(cloned_repo, "commit", "-m", f"School Core candidate {task_id}", env=_git_env())


def materialize_guest_candidate(
    artifact: GuestCandidateArtifact,
    *,
    repo_path: Path,
    destination: Path,
    candidate_store: CandidateStore,
    expected_task_id: str,
    candidate_id: str,
    bead_id: str,
    issue_number: int,
    expected_repository: str,
    expected_base_sha: str,
    branch: str,
) -> MaterializedCandidate:
    normalized_expected_task_id = _norm_task_id(expected_task_id)
    normalized_expected_repository = _norm_repository(expected_repository)
    if not isinstance(expected_base_sha, str) or not _SHA_RE.fullmatch(expected_base_sha):
        raise StudentVMBlocked(f"expected_base_sha is invalid: {expected_base_sha!r}")
    normalized_expected_base_sha = expected_base_sha

    if not _TASK_ID_RE.fullmatch(candidate_id):
        raise StudentVMBlocked(f"candidate_id is invalid: {candidate_id!r}")
    normalized_branch = _norm_branch(branch)

    if artifact.task_id != normalized_expected_task_id:
        raise StudentVMBlocked(
            f"artifact task_id does not match expected: {artifact.task_id!r} != {normalized_expected_task_id!r}"
        )
    if artifact.repository != normalized_expected_repository:
        raise StudentVMBlocked(
            f"artifact repository does not match expected: {artifact.repository!r} != {normalized_expected_repository!r}"
        )
    if artifact.base_sha != normalized_expected_base_sha:
        raise StudentVMBlocked(
            f"artifact base_sha does not match expected: {artifact.base_sha!r} != {normalized_expected_base_sha!r}"
        )

    source_repo_path = repo_path.expanduser().absolute()
    target_repo_path = destination.expanduser().absolute()

    if not source_repo_path.is_dir():
        raise StudentVMBlocked(f"source repo is not a directory: {source_repo_path!r}")
    if target_repo_path.exists() or target_repo_path.is_symlink():
        raise StudentVMBlocked("destination must be new")

    if _repo_git(source_repo_path, "rev-parse", "HEAD") != normalized_expected_base_sha:
        raise StudentVMBlocked("source repo HEAD does not match expected base_sha")
    if _repo_git(source_repo_path, "status", "--porcelain") != "":
        raise StudentVMBlocked("source repo is not clean")

    shutil.copytree(
        source_repo_path,
        target_repo_path,
        symlinks=False,
        ignore_dangling_symlinks=False,
        dirs_exist_ok=False,
    )
    cloned_repo = target_repo_path

    _checkout_candidate_branch(cloned_repo, normalized_branch, normalized_expected_base_sha)
    _apply_artifact_and_commit(cloned_repo, artifact, normalized_expected_task_id)

    manifest = create_candidate(
        store=candidate_store,
        repo_path=cloned_repo,
        candidate_id=candidate_id,
        bead_id=bead_id,
        issue_number=issue_number,
        repository=normalized_expected_repository,
        base_ref=normalized_expected_base_sha,
        branch=normalized_branch,
        owner="student-vm",
    )

    if not isinstance(manifest, CandidateManifest):
        raise StudentVMBlocked(f"create_candidate returned invalid manifest: {type(manifest).__name__!r}")

    return MaterializedCandidate(
        artifact=artifact,
        manifest=manifest,
        repo_path=cloned_repo,
    )


def materialize_and_verify_student_result(
    result: StudentTaskResult,
    *,
    repo_path: Path,
    destination: Path,
    candidate_store: CandidateStore,
    expected_task_id: str,
    expected_repository: str,
    expected_base_sha: str,
    candidate_id: str,
    bead_id: str,
    issue_number: int,
    branch: str,
    runner: Callable[..., dict[str, Any]],
    commands: list[dict[str, Any]],
    max_archive_bytes: int = 128 * 1024 * 1024,
    max_file_bytes: int = 32 * 1024 * 1024,
    max_entries: int = 10_000,
) -> VerifiedStudentCandidate:
    if result.exit_code != 0:
        raise StudentVMBlocked("student VM result is not successful")
    if (
        result.task_id,
        result.repository,
        result.base_sha,
    ) != (
        expected_task_id,
        expected_repository,
        expected_base_sha,
    ):
        raise StudentVMBlocked("student VM result identity does not match the task")
    if not result.candidate_archive:
        raise StudentVMBlocked("student VM result has no candidate archive")

    destination_path = destination.expanduser().absolute()
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
        repo_path=repo_path,
        destination=destination_path / "candidate",
        candidate_store=candidate_store,
        expected_task_id=expected_task_id,
        candidate_id=candidate_id,
        bead_id=bead_id,
        issue_number=issue_number,
        expected_repository=expected_repository,
        expected_base_sha=expected_base_sha,
        branch=branch,
    )

    evidence = run_candidate_gate(
        repo_path=materialized.repo_path,
        store=candidate_store,
        manifest=materialized.manifest,
        runner=runner,
        commands=commands,
    )

    return VerifiedStudentCandidate(
        task_result=result,
        candidate=materialized,
        evidence=evidence,
    )


def materialized_candidate_from_durable_record(
    record: DurableCandidateRecord,
    *,
    artifact: GuestCandidateArtifact,
    repo_path: Path,
    destination: Path,
    candidate_store: CandidateStore,
    bead_id: str,
    issue_number: int,
    branch: str,
) -> MaterializedCandidate:
    """Rebuild the exact recorded candidate head from the durable checkpoint.

    The checkpoint is the durable record plus the frozen guest artifact; the
    guest is never rerun. The rebuild uses fixed commit metadata, so the same
    artifact on the same base lands on the identical recorded head SHA and    gate evidence binds to that exact head.
    """
    if not isinstance(record, DurableCandidateRecord):
        raise StudentVMBlocked(f"record must be a DurableCandidateRecord, got {type(record).__name__!r}")
    if not isinstance(artifact, GuestCandidateArtifact):
        raise StudentVMBlocked(f"artifact must be a GuestCandidateArtifact, got {type(artifact).__name__!r}")
    if not _TASK_ID_RE.fullmatch(record.task_id):
        raise StudentVMBlocked(f"durable record task_id is invalid: {record.task_id!r}")
    if "/" not in record.repository:
        if not _NAME_RE.fullmatch(record.repository):
            raise StudentVMBlocked(f"durable record repository is invalid: {record.repository!r}")
    else:
        if not all(_NAME_RE.fullmatch(seg) for seg in record.repository.split("/")):
            raise StudentVMBlocked(f"durable record repository is invalid: {record.repository!r}")
    if not _SHA_RE.fullmatch(record.base_sha):
        raise StudentVMBlocked(f"durable record base_sha is invalid: {record.base_sha!r}")
    if not _SHA_RE.fullmatch(record.candidate_id) and not _TASK_ID_RE.fullmatch(record.candidate_id):
        raise StudentVMBlocked(f"durable record candidate_id is invalid: {record.candidate_id!r}")
    if not _SHA_RE.fullmatch(record.head_sha):
        raise StudentVMBlocked(f"durable record head_sha must be a 40-char sha, got {record.head_sha!r}")
    if record.disposition not in {"current", "superseded", "rejected"}:
        raise StudentVMBlocked(f"durable record disposition must be one of current, superseded, rejected, got {record.disposition!r}")
    if not isinstance(record.checks, tuple) or not all(isinstance(item, dict) for item in record.checks):
        raise StudentVMBlocked(f"durable record checks must be a tuple[dict, ...], got {type(record.checks).__name__!r}")
    if not isinstance(record.runner, str) or not record.runner:
        raise StudentVMBlocked(f"durable record runner must be a non-empty str, got {record.runner!r}")

    if artifact.task_id != record.task_id:
        raise StudentVMBlocked(f"artifact task_id does not match durable record: {artifact.task_id!r} != {record.task_id!r}")
    if artifact.repository != record.repository:
        raise StudentVMBlocked(f"artifact repository does not match durable record: {artifact.repository!r} != {record.repository!r}")
    if artifact.base_sha != record.base_sha:
        raise StudentVMBlocked(f"artifact base_sha does not match durable record: {artifact.base_sha!r} != {record.base_sha!r}")
    if record.archive_sha256 and record.archive_sha256 != artifact.archive_sha256:
        raise StudentVMBlocked(
            f"artifact archive_sha256 does not match durable record: {artifact.archive_sha256!r} != {record.archive_sha256!r}"
        )

    if candidate_store is None:
        raise StudentVMBlocked("candidate_store is required")

    normalized_branch = _norm_branch(branch)

    source_repo_path = repo_path.expanduser().absolute()
    target_repo_path = destination.expanduser().absolute()

    if not source_repo_path.is_dir():
        raise StudentVMBlocked(f"source repo is not a directory: {source_repo_path!r}")
    if target_repo_path.exists() or target_repo_path.is_symlink():
        raise StudentVMBlocked("destination must be new")

    if _repo_git(source_repo_path, "rev-parse", "HEAD") != record.base_sha:
        raise StudentVMBlocked("source repo HEAD does not match durable record base_sha")
    if _repo_git(source_repo_path, "status", "--porcelain") != "":
        raise StudentVMBlocked("source repo is not clean")

    shutil.copytree(
        source_repo_path,
        target_repo_path,
        symlinks=False,
        ignore_dangling_symlinks=False,
        dirs_exist_ok=False,
    )
    cloned_repo = target_repo_path

    _checkout_candidate_branch(cloned_repo, normalized_branch, record.base_sha)
    _apply_artifact_and_commit(cloned_repo, artifact, record.task_id)

    rebuilt_head = _repo_git(cloned_repo, "rev-parse", "HEAD")
    if rebuilt_head != record.head_sha:
        raise StudentVMBlocked(
            f"manifest head_sha {rebuilt_head!r} does not match durable record head_sha {record.head_sha!r}"
        )

    manifest = create_candidate(
        store=candidate_store,
        repo_path=cloned_repo,
        candidate_id=record.candidate_id,
        bead_id=bead_id,
        issue_number=issue_number,
        repository=record.repository,
        base_ref=record.base_sha,
        branch=normalized_branch,
        owner="student-vm",
    )

    if not isinstance(manifest, CandidateManifest):
        raise StudentVMBlocked(f"create_candidate returned invalid manifest: {type(manifest).__name__!r}")

    return MaterializedCandidate(
        artifact=artifact,
        manifest=manifest,
        repo_path=cloned_repo,
    )


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class StudentTaskRequest:
    """Provider-neutral, bounded task request for one disposable guest run.

    All fields are operator-controlled; nothing here is guest supplied.
    """

    def __init__(
        self,
        *,
        task_id: str,
        repository: str,
        repo_path: Path | str,
        base_sha: str,
        task: dict[str, Any],
        command: tuple[str, ...] | list[str],
        timeout_seconds: int = 900,
        cpus: int = 2,
        memory_mib: int = 4096,
        storage_gib: int = 8,
        max_output_bytes: int = 128 * 1024 * 1024,
        max_bundle_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"task_id is invalid: {task_id!r}")
        if not isinstance(repository, str):
            raise ValueError(f"repository is invalid: {repository!r}")
        normalized_repository = _norm_repository(repository)
        if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
            raise ValueError(f"base_sha is invalid: {base_sha!r}")
        if not isinstance(task, dict) or not isinstance(task.get("prompt", ""), str):
            raise ValueError(f"task must be a dict with a string prompt, got {task!r}")
        if not isinstance(command, (tuple, list)) or not command:
            raise ValueError("command is required")
        if not all(isinstance(part, str) and part and "\x00" not in part for part in command):
            raise ValueError(f"command entries must be non-empty strings without NUL bytes, got {command!r}")
        for name, value in (
            ("timeout_seconds", timeout_seconds),
            ("cpus", cpus),
            ("memory_mib", memory_mib),
            ("storage_gib", storage_gib),
            ("max_output_bytes", max_output_bytes),
            ("max_bundle_bytes", max_bundle_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive int, got {value!r}")
        self.task_id = task_id
        self.repository = normalized_repository
        self.repo_path = Path(repo_path).expanduser().absolute()
        self.base_sha = base_sha
        self.task = dict(task)
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.cpus = cpus
        self.memory_mib = memory_mib
        self.storage_gib = storage_gib
        self.max_output_bytes = max_output_bytes
        self.max_bundle_bytes = max_bundle_bytes


def _bounded_process(
    args: list[str],
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    env: dict[str, str],
    stdout_limit_bytes: int | None = None,
    stderr_limit_bytes: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a child with independent stdout/stderr caps; overflow kills it hard."""
    stdout_cap = stdout_limit_bytes if stdout_limit_bytes is not None else output_limit_bytes
    stderr_cap = stderr_limit_bytes if stderr_limit_bytes is not None else output_limit_bytes
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    collected: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    totals = {"stdout": 0, "stderr": 0}
    overflow = {"hit": False}
    lock = threading.Lock()

    def pump(name: str) -> None:
        stream = process.stdout if name == "stdout" else process.stderr
        if stream is None:
            return
        cap = stdout_cap if name == "stdout" else stderr_cap
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            with lock:
                if overflow["hit"]:
                    break
                totals[name] += len(chunk)
                collected[name].append(chunk)
                if totals[name] > cap or totals["stdout"] + totals["stderr"] > output_limit_bytes:
                    overflow["hit"] = True
                    _kill_process_group(process)
                    break

    threads = [threading.Thread(target=pump, args=(name,), daemon=True) for name in ("stdout", "stderr")]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        raise
    for thread in threads:
        thread.join(timeout=5)

    stdout_bytes = b"".join(collected["stdout"])[:stdout_cap]
    stderr_bytes = b"".join(collected["stderr"])[:stderr_cap]
    if overflow["hit"]:
        return subprocess.CompletedProcess(args, 125, stdout_bytes, b"output limit exceeded")
    return subprocess.CompletedProcess(args, returncode, stdout_bytes, stderr_bytes)


def _validate_archive(archive_bytes: bytes, limit: int) -> None:
    """Reject malformed, oversized, or path-traversal candidate archives."""
    if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
        raise StudentVMBlocked("malformed candidate archive")
    payload = bytes(archive_bytes)
    if len(payload) > limit:
        raise StudentVMBlocked(f"archive exceeds configured size limit: {len(payload)} > {limit}")
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                name = member.name
                if not name or name.strip("/") == "":
                    raise StudentVMBlocked("archive member has empty name")
                if name.startswith("/"):
                    raise StudentVMBlocked(f"archive member has unsafe path (traversal): {name!r}")
                normalized = name.strip("/")
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                if any(part == ".." for part in normalized.split("/")):
                    raise StudentVMBlocked(f"archive member has unsafe path (traversal): {name!r}")
                if not member.isreg() and not member.isdir():
                    raise StudentVMBlocked(f"archive member is not a regular file: {name!r}")
                if member.size < 0:
                    raise StudentVMBlocked(f"archive member has negative size: {name!r}")
    except tarfile.TarError as exc:
        raise StudentVMBlocked(f"malformed candidate archive: {exc}") from exc


def _guest_collection_script(command: tuple[str, ...]) -> str:
    """Guest-side wrapper: materialize the input bundle, run the student command,
    then emit the bounded candidate archive on stdout.

    The student command's stdout is redirected to stderr so that even benign
    progress chatter cannot corrupt the candidate tar stream; logs still cross
    the boundary as bounded stderr."""
    quoted = " ".join(shlex.quote(part) for part in command)
    return (
        "set -e\n"
        "mkdir -p /workspace\n"
        "tar -xf /run/school-core-input/repository.tar -C /workspace\n"
        "cp /run/school-core-input/task.json /workspace/task.json\n"
        "cd /workspace\n"
        f"{quoted} 1>&2\n"
        "rm -f /workspace/task.json\n"
        "tar -cf - -C /workspace .\n"
    )


class SmolVmRunner:
    """Local-only disposable-guest runner. Every failure blocks; there is no
    host, Orca, or direct-model coding fallback."""

    def __init__(
        self,
        *,
        smolvm: str,
        pack_path: Path | str,
        pack_sha256: str,
        quarantine_path: Path | str | None = None,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.smolvm = smolvm
        self.pack_path = Path(pack_path).expanduser()
        self.pack_sha256 = pack_sha256
        self.quarantine_path = Path(quarantine_path) if quarantine_path is not None else None
        self.process_runner = process_runner if process_runner is not None else _bounded_process

    def _quarantine(self, request: StudentTaskRequest, guest_id: str) -> None:
        if self.quarantine_path is None:
            return
        record = {
            "state": "cleanup_quarantined",
            "task_id": request.task_id,
            "repository": request.repository,
            "base_sha": request.base_sha,
            "guest_id": guest_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        self.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.quarantine_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def _stage_bundle(self, request: StudentTaskRequest, staging: Path) -> str:
        repository_tar = staging / "repository.tar"
        total = 0
        archive_process = subprocess.Popen(
            ["git", "-C", str(request.repo_path), "archive", "--format=tar", request.base_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        archive_stdout = archive_process.stdout
        if archive_stdout is None:
            raise StudentVMBlocked("git archive pipe was not created")
        try:
            with repository_tar.open("wb") as target:
                while True:
                    chunk = archive_stdout.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > request.max_bundle_bytes:
                        raise StudentVMBlocked(
                            f"archive exceeds configured size limit: {total} > {request.max_bundle_bytes}"
                        )
                    target.write(chunk)
        finally:
            if archive_process.poll() is None:
                archive_process.kill()
            archive_process.wait()
        if archive_process.returncode != 0:
            raise StudentVMBlocked("failed to build the repository archive")

        task_payload = {
            "task_id": request.task_id,
            "repository": request.repository,
            "base_sha": request.base_sha,
            "task": request.task,
            "command": list(request.command),
            "timeout_seconds": request.timeout_seconds,
        }
        (staging / "task.json").write_text(json.dumps(task_payload, separators=(",", ":")))

        digest = sha256()
        digest.update(repository_tar.read_bytes())
        digest.update((staging / "task.json").read_bytes())
        return digest.hexdigest()

    def execute(
        self,
        request: StudentTaskRequest,
        *,
        relay: RelayCapability | None = None,
    ) -> StudentTaskResult:
        """Run one task to a terminal state. Any attached relay capability is
        revoked on every exit path — success, blocked, or cleanup failure."""
        try:
            return self._execute(request)
        finally:
            if relay is not None:
                relay.revoke("task terminal")

    def _execute(self, request: StudentTaskRequest) -> StudentTaskResult:
        if not isinstance(request, StudentTaskRequest):
            raise StudentVMBlocked("request must be a StudentTaskRequest")
        started = time.monotonic()

        if not self.pack_path.is_file():
            raise StudentVMBlocked(f"guest pack is missing: {self.pack_path!s}")
        if sha256(self.pack_path.read_bytes()).hexdigest() != self.pack_sha256:
            raise StudentVMBlocked("guest pack checksum mismatch")

        if not request.repo_path.is_dir():
            raise StudentVMBlocked(f"target repo is not a directory: {request.repo_path!s}")
        try:
            head = _repo_git(request.repo_path, "rev-parse", "HEAD")
            dirty = _repo_git(request.repo_path, "status", "--porcelain")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise StudentVMBlocked(
                f"target repo pre-flight git failed: {type(exc).__name__}"
            ) from exc
        if head != request.base_sha:
            raise StudentVMBlocked("target repo HEAD does not match base_sha")
        if dirty != "":
            raise StudentVMBlocked("target repo is not clean")

        guest_id = f"sc-{request.task_id}-{uuid.uuid4().hex[:8]}"
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if os.environ.get(key)}
        env.setdefault("PATH", "/usr/bin:/bin")

        def run(args: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
            return self.process_runner(
                args,
                timeout_seconds=timeout_seconds,
                output_limit_bytes=request.max_output_bytes + _SMOLVM_STDERR_LIMIT,
                env=env,
                stdout_limit_bytes=request.max_output_bytes,
                stderr_limit_bytes=_SMOLVM_STDERR_LIMIT,
            )

        staging = Path(tempfile.mkdtemp(prefix="school-core-input-"))
        pending: StudentVMBlocked | None = None
        result: StudentTaskResult | None = None
        created = False
        try:
            try:
                bundle_sha = self._stage_bundle(request, staging)
            except OSError as exc:
                raise StudentVMBlocked(
                    f"repository archive step failed: {type(exc).__name__}"
                ) from exc
            create_args = [
                self.smolvm, "machine", "create",
                "--name", guest_id,
                "--from", str(self.pack_path.resolve()),
                "--cpus", str(request.cpus),
                "--mem", str(request.memory_mib),
                "--storage", str(request.storage_gib),
                "--volume", f"{staging}:/run/school-core-input:ro",
            ]
            start_args = [self.smolvm, "machine", "start", "--name", guest_id]
            exec_args = [
                self.smolvm, "machine", "exec",
                "--name", guest_id,
                "--timeout", f"{request.timeout_seconds}s",
                "--", "sh", "-c", _guest_collection_script(request.command),
            ]
            delete_args = [self.smolvm, "machine", "delete", "--name", guest_id, "--force"]

            try:
                create_result = run(create_args, timeout_seconds=_SMOLVM_CONTROL_TIMEOUT)
                if create_result.returncode != 0:
                    raise StudentVMBlocked(f"create failed with exit status {create_result.returncode}")
                created = True
                start_result = run(start_args, timeout_seconds=_SMOLVM_CONTROL_TIMEOUT)
                if start_result.returncode != 0:
                    raise StudentVMBlocked(f"start failed with exit status {start_result.returncode}")
                try:
                    exec_result = run(exec_args, timeout_seconds=request.timeout_seconds)
                except subprocess.TimeoutExpired:
                    raise StudentVMBlocked("supervisor timed out; guest command exceeded its deadline")
                if exec_result.returncode != 0:
                    raise StudentVMBlocked(f"guest execution failed with exit status {exec_result.returncode}")
                candidate_archive = bytes(exec_result.stdout)
                _validate_archive(candidate_archive, request.max_output_bytes)
                result = StudentTaskResult(
                    task_id=request.task_id,
                    repository=request.repository,
                    base_sha=request.base_sha,
                    guest_id=guest_id,
                    exit_code=exec_result.returncode,
                    stdout="",
                    stderr=exec_result.stderr.decode("utf-8", "replace"),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    bundle_sha256=bundle_sha,
                    candidate_archive=candidate_archive,
                )
            except StudentVMBlocked as exc:
                pending = exc
            except subprocess.TimeoutExpired:
                pending = StudentVMBlocked("supervisor timed out; guest command exceeded its deadline")
            except OSError as exc:
                # Missing smolvm binary, fork failure, etc.: a visible blocked
                # result, never a raw OS error and never a host fallback.
                pending = StudentVMBlocked(
                    f"process spawn failed: {type(exc).__name__}; no host fallback is permitted"
                )

            if created:
                delete_failed = False
                try:
                    delete_result = run(delete_args, timeout_seconds=_SMOLVM_CONTROL_TIMEOUT)
                    delete_failed = delete_result.returncode != 0
                except (subprocess.TimeoutExpired, OSError):
                    delete_failed = True
                if delete_failed:
                    self._quarantine(request, guest_id)
                    if pending is not None:
                        # Preserve the root cause: an operator recovering a
                        # quarantined guest must see why the task failed first.
                        raise StudentVMBlocked(
                            f"cleanup_quarantined: guest delete failed for {guest_id}"
                            f" (after: {pending})"
                        ) from pending
                    raise StudentVMBlocked(f"cleanup_quarantined: guest delete failed for {guest_id}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if pending is not None:
            raise pending
        if result is None:
            raise StudentVMBlocked("guest produced no result")
        return result


def dispatch_student_task(
    runner: SmolVmRunner,
    request: StudentTaskRequest,
    *,
    relay: RelayCapability | None = None,
) -> StudentTaskResult:
    """Run one task on the disposable guest; every failure blocks the task."""
    try:
        return runner.execute(request, relay=relay)
    except StudentVMBlocked:
        raise
    except subprocess.TimeoutExpired as exc:
        raise StudentVMBlocked("supervisor timed out; no host fallback is permitted") from exc
    except Exception as exc:
        raise StudentVMBlocked(
            f"dispatch failed and no host fallback is permitted: {type(exc).__name__}"
        ) from exc
