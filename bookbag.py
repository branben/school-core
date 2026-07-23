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

# Default handoff timeout and poll interval (configurable via env vars)
HANDOFF_TIMEOUT = int(os.environ.get("HANDOFF_TIMEOUT", "120"))
HANDOFF_POLL_INTERVAL = float(os.environ.get("HANDOFF_POLL_INTERVAL", "5.0"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bead_path(bead: str) -> Path:
    """Resolve the path for a bead's bookbag file."""
    BOOKBAG_DIR.mkdir(parents=True, exist_ok=True)
    return BOOKBAG_DIR / f"{bead}.json"


def exists(bead: str) -> bool:
    """Check if a bookbag already exists for this bead."""
    return bead_path(bead).exists()


def write_bookbag(
    bead: str,
    *,
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
    path = bead_path(bead)
    path.write_text(json.dumps(bag, indent=2, ensure_ascii=False))
    return bag


def read_bookbag(bead: str) -> Optional[dict]:
    """Read a bookbag from disk. Returns None if not found or unparseable."""
    path = bead_path(bead)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_bookbag(bead: str, **kwargs) -> Optional[dict]:
    """Read, update, and write a bookbag. Returns updated dict or None."""
    bag = read_bookbag(bead)
    if bag is None:
        return None
    bag.update(kwargs)
    path = bead_path(bead)
    path.write_text(json.dumps(bag, indent=2, ensure_ascii=False))
    return bag


def validate_bookbag(bead: str) -> tuple[bool, list[str]]:
    """Validate a bookbag against the expected schema.

    Returns (is_valid, list_of_issues).
    """
    bag = read_bookbag(bead)
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


def list_bookbags() -> list[str]:
    """List all bead IDs with bookbags on disk."""
    if not BOOKBAG_DIR.exists():
        return []
    return sorted(
        p.stem for p in BOOKBAG_DIR.glob("*.json")
        if not p.name.startswith(".") and p.stem not in ("board", "index")
    )


# ── Handoff Protocol (U4a) ───────────────────────────────────────────────────


class HandoffTimeoutError(TimeoutError):
    """Raised when a handoff poll exceeds the configured timeout."""
    pass


# ── File-lock Protocol (prevents write contention between CTO/COO) ──────────


def _lock_path(bead: str) -> Path:
    """Resolve the lock file path for a bead."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR / f"{bead}.lock"


def acquire_lock(bead: str, timeout: float = 10.0) -> bool:
    """Acquire a file lock for a bead. Blocks until acquired or timeout.

    Uses filesystem atomicity: O_EXCL on the open() call fails if the
    lock file already exists. Polls every 0.5s until acquired or timeout.

    Writes the PID to the lock file for stale-lock detection: if a
    teacher crashes while holding the lock, the next acquirer can check
    whether the PID is still alive before assuming the lock is stale.

    Args:
        bead: Unique task identifier.
        timeout: Maximum seconds to wait for the lock.

    Returns:
        True if lock was acquired. False if timeout expired.
    """
    lock_file = _lock_path(bead)
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


def release_lock(bead: str) -> None:
    """Release a file lock for a bead. Best-effort."""
    lock_file = _lock_path(bead)
    try:
        lock_file.unlink(missing_ok=True)
    except OSError:
        pass


def locked_update_bookbag(bead: str, lock_timeout: float = 10.0, **kwargs) -> Optional[dict]:
    """Update a bookbag with lock protection.

    Acquires a file lock before updating the bookbag, preventing
    concurrent write contention between CTO and COO when they update
    the same bookbag file.

    Args:
        bead: Unique task identifier.
        lock_timeout: Maximum seconds to wait for the lock.
        **kwargs: Fields to update in the bookbag.

    Returns:
        Updated bookbag dict, or None if lock could not be acquired
        or bookbag doesn't exist.
    """
    if not acquire_lock(bead, timeout=lock_timeout):
        return None
    try:
        return update_bookbag(bead, **kwargs)
    finally:
        release_lock(bead)


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

    def __init__(self, bead: str):
        self.bead = bead
        self._ready_path = SIGNAL_DIR / f"{bead}.ready"

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
    signal = BookbagSignal(bead)

    while time.monotonic() < deadline:
        # Read the bookbag once per poll cycle
        bag = read_bookbag(bead)
        if bag is not None:
            if all(bag.get(f, "") for f in required_fields):
                return bag
            # All required fields exist but aren't filled yet.
            # Signal file indicates the bookbag was written — if verdicts
            # aren't ready, the teachers haven't finished reviewing yet.

        time.sleep(interval)

    # Timeout — give descriptive error about what was found vs expected
    bag = read_bookbag(bead)
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
    timeout: float = HANDOFF_TIMEOUT,
    interval: float = HANDOFF_POLL_INTERVAL,
) -> tuple[str, str]:
    """Wait for both CTO and COO verdicts on a bookbag.

    Convenience wrapper around wait_for_bookbag() that returns just the
    verdict strings.

    Args:
        bead: Unique task identifier.
        timeout: Maximum seconds to wait.
        interval: Seconds between poll attempts.

    Returns:
        (cto_verdict, coo_verdict) — both are non-empty strings.

    Raises:
        HandoffTimeoutError: If timeout expires before both verdicts are filled.
    """
    bag = wait_for_bookbag(bead, timeout=timeout, interval=interval)
    return bag.get("cto_verdict", ""), bag.get("coo_verdict", "")
