"""Immutable candidate identity records for the School Core merge spine.

The store is deliberately provider-independent. It records local Git identity
and owns branch claims, but it does not publish, approve, merge, or close.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import fcntl


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CandidateManifestError(ValueError):
    """Raised when a candidate cannot be safely created or interpreted."""


@dataclass(frozen=True)
class CandidateManifest:
    candidate_id: str
    bead_id: str
    issue_number: int
    repository: str
    candidate_kind: str
    worktree: str
    branch: str
    base_ref: str
    base_sha: str
    head_sha: str
    diff_digest: str
    dirty_tree: bool
    owner: str
    created_at: str

    @classmethod
    def from_dict(cls, value: dict) -> "CandidateManifest":
        required = {
            "candidate_id", "bead_id", "issue_number", "repository",
            "candidate_kind", "worktree", "branch", "base_ref", "base_sha",
            "head_sha", "diff_digest", "dirty_tree", "owner", "created_at",
        }
        if not isinstance(value, dict) or set(value) != required:
            missing = sorted(required - set(value or {}))
            extra = sorted(set(value or {}) - required)
            raise CandidateManifestError(f"candidate fields malformed; missing={missing}, extra={extra}")
        if not all(str(value[key]).strip() for key in required - {"dirty_tree", "issue_number"}):
            raise CandidateManifestError("candidate fields must not be blank")
        if not isinstance(value["issue_number"], int) or value["issue_number"] < 1:
            raise CandidateManifestError("issue_number must be a positive integer")
        if not isinstance(value["dirty_tree"], bool):
            raise CandidateManifestError("dirty_tree must be boolean")
        if not _SHA_RE.fullmatch(str(value["base_sha"])) or not _SHA_RE.fullmatch(str(value["head_sha"])):
            raise CandidateManifestError("base_sha and head_sha must be full Git SHAs")
        return cls(**value)


@dataclass(frozen=True)
class ValidationResult:
    status: str
    reason: str = ""


class CandidateStore:
    """Durable candidate records protected by an inter-process file lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "candidates": {}, "branches": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CandidateManifestError("candidate store is unreadable") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise CandidateManifestError("candidate store version is invalid")
        if not isinstance(value.get("candidates"), dict) or not isinstance(value.get("branches"), dict):
            raise CandidateManifestError("candidate store shape is invalid")
        return value

    def _write(self, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, candidate_id: str) -> CandidateManifest:
        with self._locked():
            raw = self._read()["candidates"].get(candidate_id)
        if raw is None:
            raise CandidateManifestError(f"unknown candidate: {candidate_id}")
        return CandidateManifest.from_dict(raw)

    def branch_owner(self, branch: str) -> str | None:
        with self._locked():
            return self._read()["branches"].get(branch)

    def create(self, manifest: CandidateManifest) -> CandidateManifest:
        manifest = CandidateManifest.from_dict(asdict(manifest))
        with self._locked():
            value = self._read()
            if manifest.candidate_id in value["candidates"]:
                raise CandidateManifestError("candidate_id already exists; records are immutable")
            owner = value["branches"].get(manifest.branch)
            if owner and owner != manifest.candidate_id:
                raise CandidateManifestError(
                    f"branch {manifest.branch!r} is owned by {owner!r}"
                )
            value["candidates"][manifest.candidate_id] = asdict(manifest)
            value["branches"][manifest.branch] = manifest.candidate_id
            self._write(value)
        return manifest

    def validate(self, repo_path: str | Path, manifest: CandidateManifest) -> ValidationResult:
        try:
            CandidateManifest.from_dict(asdict(manifest))
        except CandidateManifestError as exc:
            return ValidationResult("malformed", str(exc))
        try:
            current_branch = _git(repo_path, "branch", "--show-current")
            current_head = _git(repo_path, "rev-parse", "HEAD")
            current_base = _git(repo_path, "rev-parse", manifest.base_ref)
            status = _git(repo_path, "status", "--porcelain")
            diff = _git_bytes(repo_path, "diff", "--binary", f"{manifest.base_sha}..{manifest.head_sha}")
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return ValidationResult("malformed", f"cannot read Git state: {exc}")
        digest = hashlib.sha256(diff).hexdigest()
        if current_branch != manifest.branch:
            return ValidationResult("stale", "branch changed")
        if status.strip():
            return ValidationResult("stale", "dirty tree")
        if current_head != manifest.head_sha:
            return ValidationResult("stale", "head_sha changed")
        if current_base != manifest.base_sha:
            return ValidationResult("stale", "base_sha changed")
        if digest != manifest.diff_digest:
            return ValidationResult("stale", "diff_digest changed")
        return ValidationResult("current")


def _git(repo_path: str | Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()


def _git_bytes(repo_path: str | Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True, capture_output=True, timeout=10,
    ).stdout


def create_candidate(
    *, store: CandidateStore, repo_path: str | Path, candidate_id: str,
    bead_id: str, issue_number: int, repository: str, base_ref: str,
    branch: str, owner: str, candidate_kind: str = "code",
) -> CandidateManifest:
    try:
        current_branch = _git(repo_path, "branch", "--show-current")
        base_sha = _git(repo_path, "rev-parse", base_ref)
        head_sha = _git(repo_path, "rev-parse", "HEAD")
        diff = _git_bytes(repo_path, "diff", "--binary", f"{base_sha}..{head_sha}")
        dirty = bool(_git(repo_path, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CandidateManifestError(f"cannot capture candidate Git state: {exc}") from exc
    if current_branch != branch:
        raise CandidateManifestError(f"checked out branch {current_branch!r}, expected {branch!r}")
    manifest = CandidateManifest(
        candidate_id=candidate_id, bead_id=bead_id, issue_number=issue_number,
        repository=repository, candidate_kind=candidate_kind,
        worktree=str(Path(repo_path).resolve()), branch=branch, base_ref=base_ref,
        base_sha=base_sha, head_sha=head_sha,
        diff_digest=hashlib.sha256(diff).hexdigest(), dirty_tree=dirty,
        owner=owner, created_at=datetime.now(timezone.utc).isoformat(),
    )
    return store.create(manifest)


__all__ = [
    "CandidateManifest", "CandidateManifestError", "CandidateStore",
    "ValidationResult", "create_candidate",
]
