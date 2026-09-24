"""Candidate-bound PR publication boundary.

The provider is injected so publication can be tested without live GitHub
writes. The adapter refuses to call it unless the immutable candidate still
matches the local worktree and branch claim.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from candidate_manifest import CandidateManifest, CandidateManifestError, CandidateStore


class CandidatePublicationError(RuntimeError):
    """Publication is not safe for the supplied candidate."""


@dataclass(frozen=True)
class PublicationResult:
    candidate_id: str
    head_sha: str
    pr_url: str


class CandidatePublisher(Protocol):
    def publish(self, **request: Any) -> dict[str, Any]: ...


def publish_candidate_pr(
    *, repo_path: str | Path, store: CandidateStore, manifest: CandidateManifest,
    publisher: CandidatePublisher, title: str, body: str,
) -> PublicationResult:
    try:
        validation = store.validate(repo_path, manifest)
    except CandidateManifestError as exc:
        raise CandidatePublicationError(f"candidate malformed: {exc}") from exc
    if validation.status != "current":
        raise CandidatePublicationError(f"candidate is {validation.status}: {validation.reason}")
    if store.branch_owner(manifest.branch) != manifest.candidate_id:
        raise CandidatePublicationError("branch ownership is not held by this candidate")

    try:
        import subprocess
        diff = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--binary", f"{manifest.base_sha}..{manifest.head_sha}"],
            check=True, capture_output=True, timeout=10,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CandidatePublicationError(f"cannot extract candidate diff: {exc}") from exc
    if hashlib.sha256(diff).hexdigest() != manifest.diff_digest:
        raise CandidatePublicationError("candidate diff digest changed")
    if not diff:
        raise CandidatePublicationError("candidate diff is empty")

    request = {
        "repository": manifest.repository,
        "candidate_id": manifest.candidate_id,
        "head_sha": manifest.head_sha,
        "base_ref": manifest.base_ref,
        "base_sha": manifest.base_sha,
        "branch": manifest.branch,
        "title": title,
        "body": body,
        "diff": diff,
        "label": "school-artifact" if manifest.candidate_kind == "artifact" else "school-candidate",
    }
    try:
        response = publisher.publish(**request)
    except Exception as exc:
        raise CandidatePublicationError(f"provider publication failed: {exc}") from exc
    if not isinstance(response, dict):
        raise CandidatePublicationError("provider returned an invalid publication result")
    if response.get("candidate_id") != manifest.candidate_id:
        raise CandidatePublicationError("provider candidate_id does not match")
    if response.get("head_sha") != manifest.head_sha:
        raise CandidatePublicationError("provider head_sha does not match")
    pr_url = response.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.startswith("https://"):
        raise CandidatePublicationError("provider returned no valid PR URL")
    return PublicationResult(manifest.candidate_id, manifest.head_sha, pr_url)


__all__ = ["CandidatePublicationError", "PublicationResult", "publish_candidate_pr"]
