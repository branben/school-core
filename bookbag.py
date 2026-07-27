#!/usr/bin/env python3
"""
bookbag.py — The student's notebook. Output contract for Agent School tasks.

Each task produces a bookbag at ~/.hermes/bookbag/<bead>.json.
The bookbag is the ground-truth artifact — teachers review the bookbag,
not the student's raw output. Write to disk, verify on disk, don't trust
the report.

Schema (proven against live data in ~/.hermes/bookbag/):
    bead: str           — unique task identifier
    task: str | None    — task description
    student: str        — which role handled it (coder, searcher, etc.)
    domain: str         — task domain (python-coding, code-review, etc.)
    difficulty: str     — easy / medium / hard / blocker
    output: str         — student's raw response
    lens: str           — review lens applied ("cto" / "coo")
    cto_verdict: str    — PASS or FAIL (technical correctness)
    coo_verdict: str    — PASS or FAIL (completeness / acceptance criteria)
    findings: list      — combined review findings
    ac_met: list[str]   — acceptance criteria satisfied
    files_changed: list[str]
    verification: str   — build/test output
    summary: str        — human-readable summary
    blockers: list[str] — anything blocking completion
    accepted: bool      — True if both CTO and COO passed
    timestamp: str      — ISO-8601 UTC
"""

from __future__ import annotations

import errno
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BOOKBAG_DIR = Path(
    os.environ.get("BOOKBAG_DIR", os.path.expanduser("~/.hermes/bookbag"))
)
SIGNAL_DIR = Path(os.environ.get("SIGNAL_DIR", os.path.expanduser("~/.hermes/signals")))
LOCK_DIR = Path(os.environ.get("LOCK_DIR", os.path.expanduser("~/.hermes/locks")))

# Repo namespace used when the school runs against a single repo (no per-repo
# teacher pairs). Keeps the flat layout backward-compatible: legacy bookbags
# written before namespacing still resolve under this namespace.
REPO_GLOBAL = "__global__"

# Default handoff timeout and poll interval (configurable via env vars)
HANDOFF_TIMEOUT = int(os.environ.get("HANDOFF_TIMEOUT", "120"))
HANDOFF_POLL_INTERVAL = float(os.environ.get("HANDOFF_POLL_INTERVAL", "5.0"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_dir(repo: str) -> Path:
    """Resolve the per-repo bookbag directory (nested under BOOKBAG_DIR)."""
    safe = repo.replace("/", "__")  # filesystem-safe namespace
    d = BOOKBAG_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def bead_path(bead: str, repo: str = REPO_GLOBAL) -> Path:
    """Resolve the path for a bead's bookbag file within a repo namespace."""
    return _repo_dir(repo) / f"{bead}.json"


def exists(bead: str, repo: str = REPO_GLOBAL) -> bool:
    """Check if a bookbag already exists for this bead (within a repo namespace)."""
    return bead_path(bead, repo).exists()


def write_bookbag(
    bead: str,
    *,
    repo: str = REPO_GLOBAL,
    student: str = "",
    domain: str = "",
    difficulty: str = "",
    task: Optional[str] = None,
    output: str = "",
    lens: str = "",
    cto_verdict: str = "",
    coo_verdict: str = "",
    findings: Optional[list] = None,
    ac_met: Optional[list[str]] = None,
    files_changed: Optional[list[str]] = None,
    verification: str = "",
    summary: str = "",
    blockers: Optional[list[str]] = None,
    accepted: bool = False,
) -> dict:
    """Write a bookbag to disk. Returns the dict that was written.

    All fields are written as-provided. Callers should set verdicts and
    accepted flag after review is complete.
    """
    bag = {
        "bead": bead,
        "repo": repo,
        "task": task,
        "student": student,
        "domain": domain,
        "difficulty": difficulty,
        "output": output,
        "lens": lens,
        "cto_verdict": cto_verdict,
        "coo_verdict": coo_verdict,
        "findings": findings or [],
        "ac_met": ac_met or [],
        "files_changed": files_changed or [],
        "verification": verification,
        "summary": summary,
        "blockers": blockers or [],
        "accepted": accepted,
        "timestamp": _now_iso(),
    }
    path = bead_path(bead, repo)
    path.write_text(json.dumps(bag, indent=2, ensure_ascii=False))
    return bag


def read_bookbag(bead: str, repo: str = REPO_GLOBAL) -> Optional[dict]:
    """Read a bookbag from disk. Returns None if not found or unparseable."""
    path = bead_path(bead, repo)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_bookbag(bead: str, repo: str = REPO_GLOBAL, **kwargs) -> Optional[dict]:
    """Read, update, and write a bookbag. Returns updated dict or None."""
    bag = read_bookbag(bead, repo)
    if bag is None:
        return None
    bag.update(kwargs)
    path = bead_path(bead, repo)
    path.write_text(json.dumps(bag, indent=2, ensure_ascii=False))
    return bag


def validate_bookbag(bead: str, repo: str = REPO_GLOBAL) -> tuple[bool, list[str]]:
    """Validate a bookbag against the expected schema.

    Returns (is_valid, list_of_issues).
    """
    bag = read_bookbag(bead, repo)
    if bag is None:
        return False, ["bookbag not found"]

    issues = []
    required = ["bead", "student", "domain", "output", "timestamp"]
    for key in required:
        if not bag.get(key):
            issues.append(f"missing required field: {key}")

    if bag.get("accepted") and not (bag.get("cto_verdict") == "PASS" and bag.get("coo_verdict") == "PASS"):
        issues.append("accepted=true but CTO/COO verdicts are not both PASS")

    if bag.get("cto_verdict") == "FAIL" and bag.get("accepted"):
        issues.append("CTO verdict is FAIL but bookbag is marked accepted")

    if bag.get("coo_verdict") == "FAIL" and bag.get("accepted"):
        issues.append("COO verdict is FAIL but bookbag is marked accepted")

    return len(issues) == 0, issues


# ── Verdict-record contract (post-refactor, Gap B) ──────────────────────────
#
# The bookbag is the DURABLE VERDICT RECORD only. Task lifecycle (status,
# claim, dispatch, files_changed, verification) lives in `bd` (beads) — the
# repo-mandated tracker. The bead id IS the bookbag id. This validator checks
# the two-judge contract the teachers write; it does NOT reimplement task
# tracking (that is `bd`'s job, per CLAUDE.md/AGENTS.md).
VERDICT_REQUIRED = ["bead", "cto_verdict", "coo_verdict", "accepted", "timestamp"]


def validate_verdict_record(bead: str, repo: str = REPO_GLOBAL) -> tuple[bool, list[str]]:
    """Validate the verdict-record contract (Gap B).

    The bookbag after the refactor holds ONLY the two-judge output:
        bead, cto_verdict, coo_verdict, accepted, findings, score, timestamp

    Returns (is_valid, list_of_issues).
    """
    bag = read_bookbag(bead, repo)
    if bag is None:
        return False, ["verdict record not found"]

    issues = []
    for key in VERDICT_REQUIRED:
        if key not in bag:
            issues.append(f"missing verdict field: {key}")

    cv = bag.get("cto_verdict")
    cov = bag.get("coo_verdict")
    accepted = bag.get("accepted")
    if accepted is True and not (cv == "PASS" and cov == "PASS"):
        issues.append("accepted=true but CTO/COO verdicts are not both PASS")
    if cv == "FAIL" and accepted is True:
        issues.append("CTO verdict FAIL but accepted=true")
    if cov == "FAIL" and accepted is True:
        issues.append("COO verdict FAIL but accepted=true")

    return len(issues) == 0, issues


def list_bookbags_full() -> list[tuple[str, str]]:
    """List (repo, bead_id) pairs across all namespaces (incl. legacy global).

    Unlike list_bookbags() (which returns bare ids), this preserves the repo
    namespace so callers can resolve each bookbag with read_bookbag(bead, repo).
    """
    pairs: list[tuple[str, str]] = []
    # Legacy flat (global) namespace.
    if BOOKBAG_DIR.exists():
        for p in BOOKBAG_DIR.glob("*.json"):
            if not p.name.startswith(".") and p.stem not in ("board", "index"):
                pairs.append((REPO_GLOBAL, p.stem))
    # Per-repo subdirs.
    for sub in sorted(BOOKBAG_DIR.iterdir()):
        if sub.is_dir() and not sub.name.startswith("."):
            repo = sub.name.replace("__", "/")
            for p in sub.glob("*.json"):
                if not p.name.startswith(".") and p.stem not in ("board", "index"):
                    pairs.append((repo, p.stem))
    # de-dup (legacy + repo shouldn't collide, but be safe), stable order
    seen = set()
    out = []
    for repo, bead in pairs:
        key = (repo, bead)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def list_bookbags(repo: Optional[str] = None) -> list[str]:
    """List bead IDs with bookbags on disk.

    If ``repo`` is given, list only that namespace. Otherwise list all
    namespaces (the legacy flat dir under BOOKBAG_DIR plus every per-repo
    subdir). Bare bead ids are returned; callers must pass the same ``repo``
    to read_bookbag to resolve them.
    """
    if repo is not None:
        d = _repo_dir(repo)
        if not d.exists():
            return []
        return sorted(
            p.stem for p in d.glob("*.json")
            if not p.name.startswith(".") and p.stem not in ("board", "index")
        )
    # All repos: legacy flat dir + every per-repo subdir.
    ids: list[str] = []
    if BOOKBAG_DIR.exists():
        ids += sorted(
            p.stem for p in BOOKBAG_DIR.glob("*.json")
            if not p.name.startswith(".") and p.stem not in ("board", "index")
        )
    for sub in sorted(BOOKBAG_DIR.iterdir()):
        if sub.is_dir() and not sub.name.startswith("."):
            ids += sorted(
                p.stem for p in sub.glob("*.json")
                if not p.name.startswith(".") and p.stem not in ("board", "index")
            )
    # de-dup, keep stable order
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ── Handoff Protocol (U4a) ───────────────────────────────────────────────────


class HandoffTimeoutError(TimeoutError):
    """Raised when a handoff poll exceeds the configured timeout."""
    pass


# ── File-lock Protocol (prevents write contention between CTO/COO) ──────────


def _lock_path(bead: str, repo: str = REPO_GLOBAL) -> Path:
    """Resolve the lock file path for a bead (per-repo namespace)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    safe = repo.replace("/", "__")
    return LOCK_DIR / f"{safe}__{bead}.lock"


def acquire_lock(bead: str, repo: str = REPO_GLOBAL, timeout: float = 10.0) -> bool:
    """Acquire a file lock for a bead. Blocks until acquired or timeout.

    Uses filesystem atomicity: O_EXCL on the open() call fails if the
    lock file already exists. Polls every 0.5s until acquired or timeout.

    Writes the PID to the lock file for stale-lock detection: if a
    teacher crashes while holding the lock, the next acquirer can check
    whether the PID is still alive before assuming the lock is stale.

    Args:
        bead: Unique task identifier.
        repo: Repo namespace (default global).
        timeout: Maximum seconds to wait for the lock.

    Returns:
        True if lock was acquired. False if timeout expired.
    """
    lock_file = _lock_path(bead, repo)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Check if the existing lock is stale (holder crashed)
            try:
                pid_str = lock_file.read_text().strip()
                if pid_str:
                    pid = int(pid_str)
                    # kill(pid, 0) checks if the PID exists in the process
                    # table without sending a signal. ESRCH means no such process.
                    try:
                        os.kill(pid, 0)
                    except OSError as e:
                        if e.errno == errno.ESRCH:
                            # Process is gone — steal the lock
                            lock_file.unlink(missing_ok=True)
                            continue
            except (ValueError, OSError, PermissionError):
                pass
            time.sleep(0.5)
    return False


def release_lock(bead: str, repo: str = REPO_GLOBAL) -> None:
    """Release a file lock for a bead (per-repo namespace). Best-effort."""
    lock_file = _lock_path(bead, repo)
    try:
        lock_file.unlink(missing_ok=True)
    except OSError:
        pass


def locked_update_bookbag(
    bead: str, repo: str = REPO_GLOBAL, lock_timeout: float = 10.0, **kwargs
) -> Optional[dict]:
    """Update a bookbag with lock protection (per-repo namespace).

    Acquires a file lock before updating the bookbag, preventing
    concurrent write contention between CTO and COO when they update
    the same bookbag file.

    Args:
        bead: Unique task identifier.
        repo: Repo namespace (default global).
        lock_timeout: Maximum seconds to wait for the lock.
        **kwargs: Fields to update in the bookbag.

    Returns:
        Updated bookbag dict, or None if lock could not be acquired
        or bookbag doesn't exist.
    """
    if not acquire_lock(bead, repo, timeout=lock_timeout):
        return None
    try:
        return update_bookbag(bead, repo, **kwargs)
    finally:
        release_lock(bead, repo)


# ── Signal Protocol (fast flag-based handoff notification) ──────────────────


class BookbagSignal:
    """Fast flag-file signaling for bookbag handoff.

    Instead of polling the bookbag JSON file (which requires parsing JSON
    on every poll), use a lightweight `.ready` flag file. The leaf creates
    `.hermes/signals/{bead}.ready` after writing the bookbag. Teachers poll
    for the flag file instead of parsing the bookbag.

    Usage:
        # Leaf (producer):
        signal = BookbagSignal(bead)
        signal.ready()  # writes .ready flag

        # Teacher (consumer):
        signal = BookbagSignal(bead)
        if signal.check():
            bookbag = read_bookbag(bead)
    """

    def __init__(self, bead: str, repo: str = REPO_GLOBAL):
        self.bead = bead
        self.repo = repo
        safe = repo.replace("/", "__")
        self._ready_path = SIGNAL_DIR / f"{safe}__{bead}.ready"

    def ready(self) -> None:
        """Signal that this bead's bookbag is ready for review."""
        SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
        self._ready_path.write_text("ready\n")

    def check(self) -> bool:
        """Check if the ready signal exists."""
        return self._ready_path.exists()

    def clear(self) -> None:
        """Clear the ready signal after handoff is complete."""
        try:
            self._ready_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── Polling Helpers ─────────────────────────────────────────────────────────


def wait_for_bookbag(
    bead: str,
    repo: str = REPO_GLOBAL,
    required_fields: Optional[list[str]] = None,
    timeout: float = HANDOFF_TIMEOUT,
    interval: float = HANDOFF_POLL_INTERVAL,
) -> dict:
    """Poll a bookbag until all required fields are non-empty.

    Uses flag-file signaling for fast detection (avoids parsing JSON
    on every poll). Falls back to bookbag parsing when no signal file
    exists (e.g., legacy bookbags without signals).

    Args:
        bead: Unique task identifier to wait for.
        repo: Repo namespace (default global).
        required_fields: Fields that must be non-empty (default: both verdicts).
        timeout: Maximum seconds to wait before raising HandoffTimeoutError.
        interval: Seconds between poll attempts.

    Returns:
        The bookbag dict with all required fields filled.

    Raises:
        HandoffTimeoutError: If timeout expires before all fields are filled.
    """
    if required_fields is None:
        required_fields = ["cto_verdict", "coo_verdict"]

    deadline = time.monotonic() + timeout
    signal = BookbagSignal(bead, repo)

    while time.monotonic() < deadline:
        # Read the bookbag once per poll cycle
        bag = read_bookbag(bead, repo)
        if bag is not None:
            if all(bag.get(f, "") for f in required_fields):
                return bag
            # All required fields exist but aren't filled yet.
            # Signal file indicates the bookbag was written — if verdicts
            # aren't ready, the teachers haven't finished reviewing yet.

        time.sleep(interval)

    # Timeout — give descriptive error about what was found vs expected
    bag = read_bookbag(bead, repo)
    field_states = {}
    if bag:
        for f in required_fields:
            val = bag.get(f, "")
            field_states[f] = repr(val) if val else "(empty)"
    else:
        field_states["_bookbag"] = "not found"

    raise HandoffTimeoutError(
        f"Handoff timeout after {timeout}s for bead '{bead}'. "
        f"Fields: {field_states}"
    )


def wait_for_verdicts(
    bead: str,
    repo: str = REPO_GLOBAL,
    timeout: float = HANDOFF_TIMEOUT,
    interval: float = HANDOFF_POLL_INTERVAL,
) -> tuple[str, str]:
    """Wait for both CTO and COO verdicts on a bookbag.

    Convenience wrapper around wait_for_bookbag() that returns just the
    verdict strings.

    Args:
        bead: Unique task identifier.
        repo: Repo namespace (default global).
        timeout: Maximum seconds to wait.
        interval: Seconds between poll attempts.

    Returns:
        (cto_verdict, coo_verdict) — both are non-empty strings.

    Raises:
        HandoffTimeoutError: If timeout expires before both verdicts are filled.
    """
    bag = wait_for_bookbag(bead, repo, timeout=timeout, interval=interval)
    return bag.get("cto_verdict", ""), bag.get("coo_verdict", "")
