"""Candidate-bound PR publication state machine and provider adapter.

Wraps ``candidate_pr.publish_candidate_pr`` with a durable, candidate-bound
journal (pr_pending / pr_failed / pr_published) so publication is idempotent by
(candidate_id, head_sha) and retry is safe against duplicate PR creation.
Fail-closed rules live in docs/pr-provider-boundary.md — update that first for
any boundary change.

Ambiguity discipline: a provider write whose outcome is unknown is recorded as
``pr_pending`` and can only clear through a provider answer bound to
(candidate_id, head_sha). A guess is never a reconciliation.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import fcntl

from candidate_pr import (
    CandidatePublicationError,
    ProviderWriteError,
    PublicationResult,
    publish_candidate_pr,
)


class _FakePublisher:
    """Bridge-test seam: fake provider adapter with raise / override / reconcile.

    Imported by ``tests/test_issue_bridge.py`` as ``pr_provider._BoundFakePublisher``
    so the candidate-bound bridge tests can exercise the None / exception / ambiguous /
    reconcile-by-identity paths without a live GitHub write.
    """

    def __init__(self, *, raises=None, response_override=None, existing=None):
        self.publish_calls = []
        self.raises = raises
        self.response_override = response_override
        self.existing = dict(existing or {})

    def publish(self, **request):
        self.publish_calls.append(request)
        if self.raises is not None:
            raise self.raises
        if self.response_override is None:
            # Provider returned nothing usable — ambiguous post-write outcome.
            raise ProviderWriteError("provider returned no publication result")
        return self.response_override

    def find_pr(self, *, repository, candidate_id, head_sha, branch):
        return self.existing.get((candidate_id, head_sha))


_BoundFakePublisher = _FakePublisher  # backward-compat seam for the bridge tests
from candidate_manifest import CandidateManifest, CandidateStore

STATE_PENDING = "pr_pending"
STATE_FAILED = "pr_failed"
STATE_PUBLISHED = "pr_published"


class PrPublicationPending(RuntimeError):
    """Publication outcome is unknown; reconcile with the provider first.

    Carries no success semantics: the issue must stay open and unprocessed
    until a (candidate_id, head_sha)-bound reconciliation resolves the state.
    """


@dataclass(frozen=True)
class PrRecord:
    candidate_id: str
    state: str
    issue_number: int
    repository: str
    branch: str
    head_sha: str
    pr_url: str
    attempts: int
    error: str
    updated_at: str


class PrStateStore:
    """Durable publication journal keyed by candidate_id (inter-process lock)."""

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

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "publications": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrPublicationPending(f"PR state journal is unreadable: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise PrPublicationPending("PR state journal version is invalid")
        if not isinstance(value.get("publications"), dict):
            raise PrPublicationPending("PR state journal shape is invalid")
        return value

    def _write(self, value: dict[str, Any]) -> None:
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

    def get(self, candidate_id: str) -> PrRecord | None:
        with self._locked():
            raw = self._read()["publications"].get(candidate_id)
        if raw is None:
            return None
        return PrRecord(**raw)

    def _record(self, *, candidate_id: str, issue_number: int, repository: str,
                branch: str, head_sha: str, state: str, pr_url: str,
                error: str) -> None:
        with self._locked():
            value = self._read()
            prior = value["publications"].get(candidate_id) or {}
            attempts = int(prior.get("attempts", 0))
            if state == STATE_PENDING:
                attempts += 1
            value["publications"][candidate_id] = {
                "candidate_id": candidate_id,
                "state": state,
                "issue_number": int(issue_number),
                "repository": repository,
                "branch": branch,
                "head_sha": head_sha,
                "pr_url": pr_url,
                "attempts": attempts,
                "error": error[:2000],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write(value)

    def record_pending(self, *, candidate_id: str, issue_number: int,
                       repository: str, branch: str, head_sha: str,
                       error: str = "") -> None:
        self._record(
            candidate_id=candidate_id, issue_number=issue_number,
            repository=repository, branch=branch, head_sha=head_sha,
            state=STATE_PENDING, pr_url="", error=error,
        )

    def record_failed(self, *, candidate_id: str, issue_number: int,
                      repository: str, branch: str, head_sha: str,
                      error: str = "") -> None:
        self._record(
            candidate_id=candidate_id, issue_number=issue_number,
            repository=repository, branch=branch, head_sha=head_sha,
            state=STATE_FAILED, pr_url="", error=error,
        )

    def record_published(self, *, candidate_id: str, issue_number: int,
                         repository: str, branch: str, head_sha: str,
                         pr_url: str) -> None:
        self._record(
            candidate_id=candidate_id, issue_number=issue_number,
            repository=repository, branch=branch, head_sha=head_sha,
            state=STATE_PUBLISHED, pr_url=pr_url, error="",
        )


def _journal_kwargs(manifest: CandidateManifest) -> dict[str, Any]:
    return {
        "candidate_id": manifest.candidate_id,
        "issue_number": manifest.issue_number,
        "repository": manifest.repository,
        "branch": manifest.branch,
        "head_sha": manifest.head_sha,
    }


def publish_candidate_pr_idempotent(
    *, repo_path: str | Path, store: CandidateStore, manifest: CandidateManifest,
    publisher: Any, journal: PrStateStore, title: str, body: str,
) -> PublicationResult:
    """Publish the exact candidate once, with duplicate-safe retry.

    Fail-closed contract (docs/pr-provider-boundary.md):
    - ``pr_published`` + same head_sha -> return the recorded URL, no write.
    - ``pr_pending`` -> reconcile with the provider first. Found -> adopt.
      Definitively absent -> safe to write. Unknown -> raise without writing.
    - Pre-write refusals -> ``pr_failed`` (zero provider writes).
    - Provider-write ambiguity -> stays ``pr_pending``.
    """
    candidate_id = manifest.candidate_id
    head_sha = manifest.head_sha
    prior = journal.get(candidate_id)

    if prior is not None and prior.state == STATE_PUBLISHED:
        if prior.head_sha == head_sha and prior.pr_url.startswith("https://"):
            return PublicationResult(candidate_id, head_sha, prior.pr_url)
        raise PrPublicationPending(
            f"candidate {candidate_id} is recorded published at head "
            f"{prior.head_sha}, which does not bind to {head_sha}"
        )

    if prior is not None and prior.state == STATE_PENDING:
        if prior.head_sha != head_sha:
            raise PrPublicationPending(
                f"candidate {candidate_id} has an unresolved publication for head "
                f"{prior.head_sha}; refusing to publish head {head_sha}"
            )
        find_pr = getattr(publisher, "find_pr", None)
        if not callable(find_pr):
            raise PrPublicationPending(
                "publisher cannot reconcile an ambiguous publication; "
                "refusing a possible duplicate PR"
            )
        try:
            existing_url = find_pr(
                repository=manifest.repository, candidate_id=candidate_id,
                head_sha=head_sha, branch=manifest.branch,
            )
        except Exception as exc:
            raise PrPublicationPending(
                f"provider reconciliation failed: {exc}"
            ) from exc
        if isinstance(existing_url, str) and existing_url.startswith("https://"):
            journal.record_published(
                pr_url=existing_url, **_journal_kwargs(manifest),
            )
            return PublicationResult(candidate_id, head_sha, existing_url)
        # Provider confirms no PR exists for this (candidate_id, head_sha) —
        # a fresh write cannot duplicate anything.

    # Journal BEFORE the provider write: a crash can only leave pr_pending.
    journal.record_pending(**_journal_kwargs(manifest))
    try:
        result = publish_candidate_pr(
            repo_path=repo_path, store=store, manifest=manifest,
            publisher=publisher, title=title, body=body,
        )
    except ProviderWriteError as exc:
        journal.record_pending(error=str(exc), **_journal_kwargs(manifest))
        raise PrPublicationPending(
            f"publication outcome is ambiguous: {exc}"
        ) from exc
    except CandidatePublicationError as exc:
        journal.record_failed(error=str(exc), **_journal_kwargs(manifest))
        raise
    journal.record_published(pr_url=result.pr_url, **_journal_kwargs(manifest))
    return result


def _default_run(argv: list[str]) -> str:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=60,
    ).stdout


class GitHubCliPublisher:
    """Provider adapter over local git push + the gh CLI.

    ``find_pr`` returns a PR URL only when the provider-side PR head matches
    head_sha — the binding that makes reconcile-before-retry duplicate-safe.
    Live external writes only happen when this adapter is actually invoked;
    unit tests exercise it through a captured-argv runner only.
    """

    def __init__(self, repo_path: str | Path,
                 runner: Callable[[list[str]], str] | None = None):
        self.repo_path = Path(repo_path)
        self._run = runner or _default_run

    def publish(self, **request: Any) -> dict[str, Any]:
        repository = str(request["repository"])
        branch = str(request["branch"])
        self._run([
            "git", "-C", str(self.repo_path), "push", "-u", "origin", branch,
        ])
        out = self._run([
            "gh", "pr", "create", "--repo", repository,
            "--base", str(request["base_ref"]), "--head", branch,
            "--title", str(request["title"]), "--body", str(request["body"]),
            "--label", str(request["label"]),
        ])
        lines = (out or "").strip().splitlines()
        return {
            "candidate_id": request["candidate_id"],
            "head_sha": request["head_sha"],
            "pr_url": lines[-1].strip() if lines else "",
        }

    def find_pr(self, *, repository: str, candidate_id: str, head_sha: str,
                branch: str) -> str | None:
        out = self._run([
            "gh", "pr", "list", "--repo", repository, "--head", branch,
            "--state", "all", "--json", "url,headRefOid",
        ])
        items = json.loads(out or "[]")
        if not isinstance(items, list):
            raise RuntimeError("provider returned an invalid PR list")
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("headRefOid") == head_sha
                and str(item.get("url", "")).startswith("https://")
            ):
                return str(item["url"])
        return None


__all__ = [
    "GitHubCliPublisher", "PrPublicationPending", "PrRecord", "PrStateStore",
    "STATE_FAILED", "STATE_PENDING", "STATE_PUBLISHED",
    "publish_candidate_pr_idempotent",
]
