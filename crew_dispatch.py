"""Standalone FirstMate -> Orca crew lifecycle for school-loop dispatch.

This module intentionally has no bridge dependency. U8 can consume the
``CrewResult`` contract while keeping direct-Orca fallback logic outside this
lifecycle wrapper.

The tracked registry is portable and deliberately omits Orca's path-bearing
worktree ID. Exact cleanup identity lives in persistent FM-local state on the
runner. If a later cycle runs on a different runner without that FM state, the
sweep retains the record and emits a warning rather than guessing a destructive
cleanup target; cross-runner cleanup handles belong to U8/U9 checkpoint design.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

log = logging.getLogger(__name__)

FM_HOME = Path(os.environ.get("FM_HOME", str(Path.home() / ".hermes" / "school-core-fm-config"))).expanduser()
FM_SPAWN = Path(os.environ.get("FM_SPAWN", str(Path.home() / ".local/share/firstmate/bin/fm-spawn.sh"))).expanduser()
STATE_DIR = Path(os.environ.get("FM_STATE", str(FM_HOME / "state"))).expanduser()
DATA_DIR = Path(os.environ.get("FM_DATA", str(FM_HOME / "data"))).expanduser()
CREW_RUNS_FILE = Path(
    os.environ.get("CREW_RUNS_FILE", str(Path(__file__).parent / "data/crew_runs.json"))
).expanduser()
DEFAULT_TIMEOUT = float(os.environ.get("CREW_TIMEOUT_SECONDS", "900"))
DEFAULT_POLL_INTERVAL = float(os.environ.get("CREW_POLL_INTERVAL_SECONDS", "15"))
DEFAULT_BLOCKED_GRACE = float(os.environ.get("CREW_BLOCKED_GRACE_SECONDS", "60"))
MAX_REPORT_BYTES = 256 * 1024
_ARTIFACT_FIELD_RE = re.compile(
    r"(?i)\b(branch|commit|base(?:[_ -](?:ref|commit))?)\s*[:=]\s*([^\s]+)"
)

_STATUS_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_-]*)(?:\s+\[[^\]]*\])?\s*:"
)
_META_RE = re.compile(r"^([^=]+)=(.*)$")


@dataclass(frozen=True)
class CrewResult:
    """Terminal result returned by :func:`dispatch_crew`."""

    crew_id: str
    status: str
    report_path: Optional[Path] = None
    fallback_reason: Optional[str] = None
    teardown_ok: bool = False
    orca_worktree_id: Optional[str] = None


class CrewUnavailableError(RuntimeError):
    """Raised when FirstMate cannot spawn a crew task."""


def _run(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Single subprocess seam for tests and controlled external commands."""

    return subprocess.run(list(args), capture_output=True, text=True, check=False, **kwargs)


def _crew_id(cycle_session_id: str, issue_number: int) -> str:
    return f"fm-{cycle_session_id}-{issue_number}"


def _task_dir(crew_id: str) -> Path:
    return DATA_DIR / crew_id


def _local_worktree_id(crew_id: str) -> Optional[str]:
    try:
        value = (_task_dir(crew_id) / "orca_worktree_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _status_path(crew_id: str) -> Path:
    return STATE_DIR / f"{crew_id}.status"


def _meta_path(crew_id: str) -> Path:
    return STATE_DIR / f"{crew_id}.meta"


def _read_status_detail(path: Path) -> tuple[Optional[str], str]:
    """Return the latest status verb and payload from a FirstMate status file."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None, ""
    for line in reversed(lines):
        match = _STATUS_RE.match(line)
        if match:
            return match.group(1).lower(), line[match.end():].strip()
    return None, ""


def _read_status(path: Path) -> Optional[str]:
    """Return the most recent status verb, or None before the first write."""
    return _read_status_detail(path)[0]


def _artifact_identity(text: str) -> Optional[dict[str, str]]:
    """Extract a complete branch/commit/base identity from bounded text."""
    fields = {
        match.group(1).lower().replace(" ", "_").replace("-", "_"): match.group(2).strip()
        for match in _ARTIFACT_FIELD_RE.finditer(text)
    }
    base = fields.get("base") or fields.get("base_ref") or fields.get("base_commit")
    identity = {
        "branch": fields.get("branch", ""),
        "commit": fields.get("commit", ""),
        "base": base or "",
    }
    return identity if all(identity.values()) else None


def _has_artifact_identity(report: str) -> bool:
    """Require non-empty branch, commit, and base evidence."""
    return _artifact_identity(report) is not None


def _read_meta(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return result
    for line in lines:
        match = _META_RE.match(line.strip())
        if match:
            result[match.group(1).strip()] = match.group(2).strip()
    return result


def _read_meta_until_available(
    crew_id: str,
    *,
    timeout: float,
    poll_interval: float,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> dict[str, str]:
    """Allow fm-spawn time to publish metadata before cleanup is attempted."""

    wait = poll_interval if poll_interval > 0 else min(max(0.0, timeout), 0.1)
    deadline = now_fn() + max(0.0, timeout)
    # The attempt bound protects callers that inject a clock/sleeper which does
    # not advance. A zero interval still gets one immediate retry, then the
    # bounded wait ends instead of spinning forever.
    max_attempts = max(1, int(max(0.0, timeout) / max(wait, 0.001)) + 1)
    attempts = 0
    while True:
        meta = _read_meta(_meta_path(crew_id))
        if meta:
            return meta
        attempts += 1
        if now_fn() >= deadline or attempts >= max_attempts:
            return {}
        sleep_fn(wait)


def _write_brief(crew_id: str, task_text: str, issue_number: int, project_dir: Path) -> Path:
    destination = _task_dir(crew_id)
    destination.mkdir(parents=True, exist_ok=True)
    brief = destination / "brief.md"
    brief.write_text(
        "# School crew task\n\n"
        f"Issue: #{issue_number}\n"
        # Keep the runtime brief useful without persisting the operator's
        # absolute home path into a durable artifact.
        f"Project: {project_dir.name}\n\n"
        "## Task\n\n"
        f"{task_text.rstrip()}\n\n"
        "## Delivery contract\n\n"
        "Work independently in the assigned Orca worktree and make the requested "
        "code changes there. Run the relevant checks, create a local commit, and "
        "write bounded evidence to `report.md` before marking the task done. The "
        "final `done:` status must name the branch, commit, and base identity.\n"
    )
    return brief


def _registry_path(path: Optional[Path] = None) -> Path:
    return path or CREW_RUNS_FILE


def _load_runs(path: Optional[Path] = None) -> list[dict]:
    target = _registry_path(path)
    try:
        raw = json.loads(target.read_text()) if target.exists() else []
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


@contextmanager
def _registry_lock(path: Optional[Path] = None) -> Iterator[None]:
    """Serialize registry read/modify/write operations for one checkout."""
    target = _registry_path(path)
    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows path
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                # A one-byte lock file may be empty on first use.
                lock.write("0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unsupported platform
            raise RuntimeError("no supported file-lock implementation")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows path
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _save_runs_unlocked(runs: list[dict], path: Optional[Path] = None) -> None:
    target = _registry_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A unique temporary path avoids concurrent writers ever sharing a .tmp
    # name; the registry lock serializes the replacement itself.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    ) as temp:
        temp.write(json.dumps(runs, indent=2) + "\n")
        temp_path = Path(temp.name)
    try:
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _save_runs(runs: list[dict], path: Optional[Path] = None) -> None:
    with _registry_lock(path):
        _save_runs_unlocked(runs, path)


def _record_run(entry: dict, path: Optional[Path] = None) -> None:
    with _registry_lock(path):
        runs = _load_runs(path)
        runs.append(_portable_entry(entry))
        _save_runs_unlocked(runs, path)


def _portable_entry(entry: dict) -> dict:
    """Return only checkpoint-safe registry fields.

    Worktree IDs are retained in FM-local metadata for cleanup, but the
    tracked registry must not carry Orca's absolute path-bearing identity.
    """
    portable = dict(entry)
    if portable.get("orca_worktree_id"):
        portable["orca_worktree_present"] = True
    # Never checkpoint the path-bearing Orca identity, including a null value.
    portable.pop("orca_worktree_id", None)
    return portable


def _update_run(crew_id: str, updates: dict, path: Optional[Path] = None) -> None:
    with _registry_lock(path):
        runs = _load_runs(path)
        for entry in reversed(runs):
            if entry.get("crew_id") == crew_id:
                entry.update(_portable_entry(updates))
                entry.pop("orca_worktree_id", None)
                break
        _save_runs_unlocked(runs, path)


def _orca_remove(worktree_id: str) -> bool:
    """Use Orca's native primitive; fm-teardown rejects Orca ids containing paths."""

    try:
        result = _run([
            "orca", "worktree", "rm", "--worktree", f"id:{worktree_id}",
            "--force", "--json",
        ], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("orca worktree cleanup failed for %s: %s", worktree_id, exc)
        return False
    if result.returncode != 0:
        log.warning("orca worktree cleanup failed for %s: %s", worktree_id, result.stderr.strip())
        return False
    return True


def teardown_worktree(worktree_id: Optional[str]) -> bool:
    if not worktree_id:
        return False
    return _orca_remove(worktree_id)


def _started_at(entry: dict) -> Optional[float]:
    try:
        return datetime.fromisoformat(entry["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        # Unknown age is not proof of staleness. Preserve the record for
        # operator inspection rather than deleting an unrelated worktree.
        return None


def sweep_stale_runs(
    *,
    now: Optional[float] = None,
    stale_after: float = DEFAULT_TIMEOUT,
    path: Optional[Path] = None,
) -> int:
    """Remove old running/blocked records and best-effort reclaim their Orca worktrees."""

    current = time.time() if now is None else now
    with _registry_lock(path):
        runs = _load_runs(path)
        kept: list[dict] = []
        removed = 0
        changed = False
        for entry in runs:
            started = _started_at(entry)
            age = current - started if started is not None else None
            if entry.get("status") in {"running", "blocked"} and age is not None and age > stale_after:
                worktree_id = entry.get("orca_worktree_id") or _local_worktree_id(
                    str(entry.get("crew_id", ""))
                )
                if not worktree_id:
                    log.warning(
                        "stale crew %s has no local Orca identity; retaining record for operator cleanup",
                        entry.get("crew_id", "unknown"),
                    )
                # Orca IDs are opaque; only sweep a non-empty recorded/local
                # ID. A malformed record without a usable identity must not
                # trigger a destructive cleanup command.
                cleanup_ok = (
                    teardown_worktree(worktree_id)
                    if isinstance(worktree_id, str) and worktree_id
                    else False
                )
                if cleanup_ok:
                    removed += 1
                    continue
                entry["cleanup_failed"] = True
                kept.append(entry)
                changed = True
                continue
            kept.append(entry)
        if removed or changed:
            _save_runs_unlocked(kept, path)
    return removed


def _spawn(crew_id: str, project_dir: Path) -> subprocess.CompletedProcess:
    # fm-spawn.sh requires BOTH --mode and --yolo on every ship (they are the
    # task's delivery contract). --yolo on = the crew's routine approvals are
    # granted so an unattended crewmate can complete work; the delivery
    # contract still forbids remote pushes / destructive actions (the school
    # works in its Orca worktree, commits locally, and reports). Omitting
    # --yolo made every spawn fail at the CLI gate and silently fall back to
    # the direct path (observed 2026-08-12, issue #46).
    #
    # Hermes is NOT in firstmate's verified adapter list (claude|codex|
    # opencode|pi|pi-signed|grok|kimi|muse), so the hermes-fm-wrapper must be
    # passed as a RAW LAUNCH COMMAND with template placeholders (single-quoted
    # in shell; here they are literal argv elements, which is equivalent).
    # fm-spawn.sh substitutes __BRIEF__/__OPINPUT__ itself; without --harness,
    # harness resolution returns 'unknown' on the runner (no agent env
    # markers) and the spawn aborts with 'no launch template' (observed
    # 2026-08-12, issue #48). Recipe: devops/agent-school-verification
    # skill (firstmate-orca-spawn-recipe.md).
    wrapper = os.environ.get(
        "FM_WRAPPER",
        f"{Path.home()}/.local/bin/hermes-fm-wrapper",
    )
    harness = f'{wrapper} "$($__OPINPUT__ encode launch-brief < $__BRIEF__)"'
    return _run([
        str(FM_SPAWN), crew_id, str(project_dir),
        "--mode", "local-only", "--yolo", "on", "--backend", "orca",
        "--harness", harness,
    ], timeout=30)


def _poll(
    crew_id: str,
    *,
    timeout: float,
    poll_interval: float,
    blocked_grace: float,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> tuple[str, Optional[str], str]:
    started = now_fn()
    blocked_at: Optional[float] = None
    poll_sleep = poll_interval if poll_interval > 0 else min(max(timeout, 0.1), 0.1)
    max_attempts = max(1, int(max(timeout, 0.0) / max(poll_sleep, 0.1)) + 2)
    attempts = 0
    while now_fn() - started <= timeout and attempts < max_attempts:
        attempts += 1
        status, detail = _read_status_detail(_status_path(crew_id))
        if status in {"done", "failed"}:
            return status, None, detail
        if status in {"blocked", "needs-decision"}:
            blocked_at = blocked_at if blocked_at is not None else now_fn()
            if now_fn() - blocked_at >= blocked_grace:
                return "blocked", "blocked", detail
        elif status == "resolved":
            blocked_at = None
        # working, paused, resolved, unknown, and absent status all remain live.
        sleep_fn(poll_sleep)
    return "timeout", "timeout", ""


def dispatch_crew(
    *,
    issue_number: int,
    task_text: str,
    project_dir: Path,
    cycle_session_id: str,
    timeout: float = DEFAULT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    blocked_grace: float = DEFAULT_BLOCKED_GRACE,
    metadata_timeout: float = 5.0,
    metadata_poll_interval: float = 0.1,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> CrewResult:
    """Spawn, poll, collect, and clean up one code-producing FirstMate task."""

    crew_id = _crew_id(cycle_session_id, issue_number)
    sweep_stale_runs(now=now_fn(), path=CREW_RUNS_FILE)
    _write_brief(crew_id, task_text, issue_number, Path(project_dir))
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = _spawn(crew_id, Path(project_dir))
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) or "fm-spawn could not be executed"
        _record_run({
            "crew_id": crew_id,
            "issue_number": issue_number,
            "status": "spawn_failed",
            "fallback_reason": "spawn_failure",
            "started_at": started_at,
        })
        raise CrewUnavailableError(reason) from exc
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "fm-spawn failed").strip()
        _record_run({
            "crew_id": crew_id,
            "issue_number": issue_number,
            "status": "spawn_failed",
            "fallback_reason": "spawn_failure",
            "started_at": started_at,
        })
        raise CrewUnavailableError(reason)

    meta = _read_meta_until_available(
        crew_id,
        timeout=metadata_timeout,
        poll_interval=metadata_poll_interval,
        now_fn=now_fn,
        sleep_fn=sleep_fn,
    )
    worktree_id = meta.get("orca_worktree_id") or meta.get("worktree-id") or meta.get("worktree_id")
    # Keep the raw identity in ignored FM-local state for Orca cleanup. The
    # tracked registry stores only `orca_worktree_present` via _portable_entry.
    local_meta = _task_dir(crew_id) / "orca_worktree_id"
    if worktree_id:
        local_meta.write_text(worktree_id + "\n")
    _record_run({
        "crew_id": crew_id,
        "issue_number": issue_number,
        "status": "running",
        "orca_worktree_id": worktree_id,
        "started_at": started_at,
    })

    terminal_status, fallback_reason, status_detail = _poll(
        crew_id,
        timeout=timeout,
        poll_interval=poll_interval,
        blocked_grace=blocked_grace,
        now_fn=now_fn,
        sleep_fn=sleep_fn,
    )
    report_path: Optional[Path] = None
    if terminal_status == "done":
        candidate = _task_dir(crew_id) / "report.md"
        if not worktree_id:
            terminal_status = "failed"
            fallback_reason = "artifact_identity_missing"
            log.warning("crew %s reached done without an Orca worktree identity", crew_id)
        elif _artifact_identity(status_detail) is None:
            terminal_status = "failed"
            fallback_reason = "artifact_status_evidence_missing"
            log.warning("crew %s done status lacks branch/commit/base evidence", crew_id)
        elif candidate.exists():
            try:
                if candidate.stat().st_size > MAX_REPORT_BYTES:
                    fallback_reason = "report_too_large"
                    log.warning("crew %s report.md exceeds %d bytes", crew_id, MAX_REPORT_BYTES)
                    terminal_status = "failed"
                else:
                    report_text = candidate.read_text(encoding="utf-8")
                    status_identity = _artifact_identity(status_detail)
                    report_identity = _artifact_identity(report_text)
                    if status_identity and report_identity and status_identity == report_identity:
                        report_path = candidate
                    elif report_text.strip() and report_identity:
                        fallback_reason = "artifact_identity_mismatch"
                        terminal_status = "failed"
                        log.warning(
                            "crew %s status/report artifact identities do not match",
                            crew_id,
                        )
                    elif report_text.strip():
                        fallback_reason = "artifact_evidence_missing"
                        terminal_status = "failed"
                        log.warning(
                            "crew %s report.md lacks branch/commit/base evidence",
                            crew_id,
                        )
                    else:
                        fallback_reason = "report_empty"
                        terminal_status = "failed"
                        log.warning("crew %s reached done with an empty report.md", crew_id)
            except OSError as exc:
                fallback_reason = "report_unreadable"
                terminal_status = "failed"
                log.warning("crew %s report.md could not be read: %s", crew_id, exc)
        else:
            fallback_reason = "report_missing"
            terminal_status = "failed"
            log.warning("crew %s reached done without report.md", crew_id)
    elif terminal_status == "failed":
        fallback_reason = "crew_failed"

    teardown_ok = teardown_worktree(worktree_id)
    _update_run(crew_id, {
        "status": terminal_status,
        "fallback_reason": fallback_reason,
        "teardown_ok": teardown_ok,
        # Keep the durable registry portable: callers still receive the
        # absolute runtime Path, but the checkpointable record stores only a
        # path relative to FM_DATA.
        "report_path": (
            str(report_path.relative_to(DATA_DIR))
            if report_path else None
        ),
    })
    return CrewResult(
        crew_id=crew_id,
        status=terminal_status,
        report_path=report_path,
        fallback_reason=fallback_reason,
        teardown_ok=teardown_ok,
        orca_worktree_id=worktree_id,
    )
