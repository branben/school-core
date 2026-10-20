"""Resilience guard primitives for the school-core scale architecture.

Implements the worst-day-ever error-boundary nodes (see
docs/school-core-worst-day-ever.md) as small, pure, independently-testable
functions. The dispatch-office / grading-queue topology (Option B) is not yet
built, so these are the *guard primitives* that the future queue wires in; the
ones that apply to existing code paths are wired at their call sites too.

Nodes covered here:
  N1.1 input sanitization (RTL / null / length)
  N1.3 numeric guard (NaN/Inf -> default)
  N2.1 idempotency key for grading dedup
  N2.3 grader idempotent scoring key
  N3.2 monotonic-clock lifecycle (immune to wall-clock skew)
  N4.1 worktree isolation pre-verify
  N4.3 force_agent capability allowlist
  N6.1 bounded grader pool sizing
  N6.2 worktree lease (claim/release under the locked registry)
  N7.1 retry budget cap
  N7.2 non-fatal GitHub label queue
  N7.3 fallback backpressure semaphore
  N8.2 resource-clean assertion helper

The already-landed CRITICAL nodes (N5.1/N5.2/N5.3) live in issue_bridge.py /
scoring.py and are not duplicated here.
"""

from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# ── N1.1: Input Boundary sanitization ────────────────────────────────────────

# Unicode RTL override / bidi controls that can hide injected instructions.
_RTL_OVERRIDE_RE = re.compile(
    "[\u202A\u202B\u202C\u202D\u202E\u2066\u2067\u2068\u2069]"
)
# Control / format chars that should never reach a crew brief or shell arg.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def sanitize_input_text(
    text: str,
    *,
    max_len: int = 4000,
    strip_null: bool = True,
    strip_rtl: bool = True,
) -> str:
    """Scrub text before it enters a crew brief, agent prompt, or bookbag.

    N1.1: removes null bytes and Unicode RTL/bidi overrides (which can hide a
    crafted instruction inside a reversed string) and caps length so a 2 MB
    issue body can't blow the brief. The shell-quoting contract (N1.2) is
    handled separately by dispatch_crew's list-args subprocess call.
    """
    if text is None:
        return ""
    out = str(text)
    if strip_null:
        out = _CONTROL_RE.sub("", out)
    if strip_rtl:
        out = _RTL_OVERRIDE_RE.sub("", out)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    if len(out) > max_len:
        out = out[:max_len]
    return out


# ── N1.3: Numeric guard ──────────────────────────────────────────────────────

def safe_float(value: object, default: float = 0.0) -> float:
    """Coerce to float, mapping NaN/Inf/-Inf to ``default`` (N1.3).

    A NaN or Inf leaking from a director override or fetched metric must never
    propagate into arithmetic (e.g. score EMA, budget math). ``math.isfinite``
    is the single gate.
    """
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(f):
        return float(default)
    return f


# ── N2.1 / N2.3: Idempotency + grader scoring keys ──────────────────────────

def grading_dedup_key(issue_number: object, crew_id: object) -> str:
    """Stable dedup key for a grading job (N2.1).

    Keyed by (issue_number, crew_id) so a re-delivered or re-dispatched job is
    deduplicated before enqueue; a re-dispatch of an in-flight issue is a no-op.
    """
    return f"{issue_number}:{crew_id}"


def grader_score_key(crew_id: object) -> str:
    """Idempotent scoring key (N2.3): keyed by crew_id so a replayed grade is a
    no-op, not a second EMA application."""
    return str(crew_id)


# ── N3.2: Monotonic-clock lifecycle ──────────────────────────────────────────

def monotonic_now() -> float:
    """Monotonic seconds since boot — immune to wall-clock skew (N3.2)."""
    return time.monotonic()


def is_stale_monotonic(
    start_monotonic: float,
    now_monotonic: float,
    stale_after: float,
) -> bool:
    """True when a crew started at ``start_monotonic`` has outlived
    ``stale_after`` seconds, measured on a monotonic clock.

    Wall-clock skew (Mac vs GitHub) can never make a live crew look stale, or a
    dead crew look alive, because neither endpoint depends on wall time.
    """
    age = now_monotonic - start_monotonic
    return age > float(stale_after)


# ── N4.1: Worktree isolation pre-verify ──────────────────────────────────────

_EXPECTED_SCAFFOLD_RE = re.compile(r"^(fm/|school-core-|agent-|student-)", re.IGNORECASE)


def verify_worktree_isolated(
    git_status_lines: list[str],
    *,
    issue_branch_prefix: str = "fm/",
) -> tuple[bool, list[str]]:
    """Pre-flight that a crew's worktree holds ONLY this issue's baseline.

    N4.1: a worktree handed to student A must not contain student B's uncommitted
    diff. A crew legitimately CREATES new files (untracked ``??`` / added ``A ``),
    so those are allowed. The hazard is a MODIFICATION/DELETION to an existing
    tracked file (``M ``, `` M``, ``D ``, ``R ``, ``U ``, etc.) left by a prior
    occupant. A branch line (``##``) must reference the expected crew branch.

    Returns (is_isolated, findings).
    """
    findings: list[str] = []
    for line in git_status_lines or []:
        if line.startswith("##"):
            # Branch line — allowed only if it references the crew's scaffold
            # branch (e.g. fm/<crew_id>), not a foreign branch.
            if issue_branch_prefix and issue_branch_prefix not in line:
                findings.append(f"unexpected branch state: {line!r}")
            continue
        if len(line) < 2:
            continue
        xy = line[0:2]
        if xy == "??":
            continue  # untracked new file — the crew's own work, allowed
        if xy[0] == "A" or xy[1] == "A":
            continue  # added file — the crew's own work, allowed
        if xy == "  ":
            continue  # clean tracked entry
        # Anything else (M/D/R/C/U in either column) is a change to an existing
        # tracked file — foreign diff from a prior occupant.
        findings.append(f"uncommitted change in worktree: {line!r}")
    return (len(findings) == 0, findings)


# ── N4.3: force_agent capability allowlist ──────────────────────────────────

def force_agent_allowed(
    requested: Optional[str],
    capability_profile: Optional[str],
    lora_twin: Optional[str] = None,
) -> bool:
    """N4.3: a forced agent must be the capability's own profile (no escalation
    to a higher-trust profile). ``None`` requested → always allowed (the
    capability resolver chooses). A requested role equal to the capability's own
    profile (or its lora twin, when supplied) is allowed; anything else is an
    escalation and denied (fail closed)."""
    if requested is None:
        return True
    if capability_profile is None:
        # No capability context: only allow if the caller is the dispatcher
        # itself; otherwise treat as escalation (fail closed).
        return False
    if requested == capability_profile:
        return True
    if lora_twin is not None and requested == lora_twin:
        return True
    return False


# ── N6.1: Bounded grader pool sizing ─────────────────────────────────────────

def bounded_grader_pool_size(
    *,
    desired: int,
    fleet_capacity: int,
    ledger_safe_max: int = 8,
) -> int:
    """N6.1: graders run in a bounded pool so concurrent ScoreStore writes never
    exceed what the lock-safe ledger can absorb. Caps at min(desired, fleet,
    ledger_safe_max), never below 1 when any work exists."""
    if desired <= 0:
        return 0
    return max(1, min(int(desired), int(fleet_capacity), int(ledger_safe_max)))


# ── N6.2: Worktree lease (claim/release under the locked registry) ──────────

@dataclass
class _Lease:
    worktree_id: str
    holder: str


def _read_leases(lease_file: Path) -> dict:
    if not lease_file.exists():
        return {}
    try:
        import json
        data = json.loads(lease_file.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_leases(lease_file: Path, leases: dict) -> None:
    import json
    import tempfile
    import os
    lease_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=lease_file.parent,
        prefix=f".{lease_file.name}.", suffix=".tmp", delete=False,
    ) as tmp:
        tmp.write(json.dumps(leases))
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, lease_file)


@contextmanager
def worktree_lease(
    lease_file: Path,
    worktree_id: str,
    holder: str,
) -> Iterator[bool]:
    """N6.2: claim a worktree lease; nested/parallel assignments to the same
    worktree see it held and get ``False`` (pick the next free one).

    The lease FILE (JSON) is the durable mutual-exclusion state. The fcntl lock
    only serializes the read-modify-write of that JSON — it is released before
    ``yield`` so a nested/reentrant lease on the same lock file from the same
    process does NOT deadlock (fcntl.flock is not recursive on macOS). The claim
    itself lives in the JSON, so a second holder (same or other process) reads
    it held and is denied.

    Yields True if the lease was acquired for ``holder``, False if already held
    (by anyone). Releases the claim on context exit.
    """
    import fcntl

    lock_path = lease_file.with_suffix(lease_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = f"wt:{worktree_id}"
    acquired_here = False

    # Phase 1 — claim under the fcntl lock, then RELEASE it before yielding.
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            leases = _read_leases(lease_file)
            current = leases.get(key)
            if current is not None and current != holder:
                yield False
                return
            leases[key] = holder
            _write_leases(lease_file, leases)
            acquired_here = True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    # Phase 2 — caller uses the lease. The JSON claim is the real guard; no
    # fcntl is held here, so nested leases on this file cannot deadlock.
    try:
        yield True
    finally:
        if acquired_here:
            # Phase 3 — release the claim under a fresh fcntl lock.
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    leases = _read_leases(lease_file)
                    if leases.get(key) == holder:
                        leases.pop(key, None)
                        _write_leases(lease_file, leases)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ── N7.1: Retry budget cap ──────────────────────────────────────────────────

@dataclass
class RetryBudget:
    limit: int

    def remaining(self, attempted: int) -> int:
        return max(0, self.limit - attempted)

    def allow(self, attempted: int) -> bool:
        """N7.1: a dead daemon / repeatedly-failing crew must not loop forever.
        Allow another attempt only while under the budget."""
        return attempted < self.limit


# ── N7.2: Non-fatal GitHub label queue ──────────────────────────────────────

class LabelWriteQueue:
    """N7.2: GitHub label writes are non-fatal. A failed write is queued for
    retry rather than blocking grading or crashing the pipeline. The real
    _mark_github_issue already 'Never raises'; this adds durable retry so a
    transient GitHub outage doesn't silently drop the label forever."""

    def __init__(self) -> None:
        self._pending: list[tuple[str, int, str]] = []

    def enqueue(self, repo: str, issue_number: int, label: str) -> None:
        self._pending.append((repo, issue_number, label))

    def pending(self) -> list[tuple[str, int, str]]:
        return list(self._pending)

    def drain(self, apply_fn) -> int:
        """Apply queued writes via ``apply_fn(repo, issue_number, label)``;
        keep failures queued. Returns count successfully applied."""
        kept: list[tuple[str, int, str]] = []
        applied = 0
        for item in self._pending:
            try:
                apply_fn(*item)
                applied += 1
            except Exception:
                kept.append(item)
        self._pending = kept
        return applied


# ── N7.3: Fallback backpressure semaphore ────────────────────────────────────

class BackpressureSemaphore:
    """N7.3: when FirstMate is down and crews fall back to the direct/OmniRoute
    path, cap concurrent fallbacks so 20 crews don't all hammer the gateway at
    once. A simple bounded counter (not a threading.Semaphore, so it works
    across the serial cycle too)."""

    def __init__(self, max_concurrent: int) -> None:
        self._max = max(1, int(max_concurrent))
        self._active = 0

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        if self._active >= self._max:
            yield False
            return
        self._active += 1
        try:
            yield True
        finally:
            self._active -= 1

    @property
    def active(self) -> int:
        return self._active


# ── N8.2: Resource-clean assertion ──────────────────────────────────────────

def assert_resources_clean(
    *,
    orca_worktrees: Optional[list[str]] = None,
    fm_local_state: Optional[list[str]] = None,
) -> tuple[bool, list[str]]:
    """N8.2: post-cycle assertion that Orca worktrees and FM-local state are
    clean so a student never leaves the daemon dirty for the next student
    (the fc7.3 acceptance criterion at fleet scale). Returns (clean, findings)."""
    findings: list[str] = []
    if orca_worktrees:
        for wt in orca_worktrees:
            findings.append(f"orphaned Orca worktree after cycle: {wt}")
    if fm_local_state:
        for st in fm_local_state:
            findings.append(f"leftover FM-local state after cycle: {st}")
    return (len(findings) == 0, findings)
