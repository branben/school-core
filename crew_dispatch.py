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

from capabilities import CapabilityBundle

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
# FirstMate can spend longer than a normal subprocess startup while Orca
# provisions a worktree/terminal. Keep this separate from the crew's total
# work timeout so slow provisioning does not masquerade as a spawn failure.
SPAWN_TIMEOUT_SECONDS = float(os.environ.get("CREW_SPAWN_TIMEOUT_SECONDS", "120"))
DEFAULT_POLL_INTERVAL = float(os.environ.get("CREW_POLL_INTERVAL_SECONDS", "15"))
DEFAULT_BLOCKED_GRACE = float(os.environ.get("CREW_BLOCKED_GRACE_SECONDS", "60"))
MAX_REPORT_BYTES = 256 * 1024
MAX_SPAWN_ERROR_CHARS = 1000
_TOKEN_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{10,}|Bearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"(?:OMNIROUTE_API_KEY|AGENTMAIL_API_KEY|GH_TOKEN)=\S+)"
)
_HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^\s'\\\"`]+")
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
    capability: Optional[dict] = None


class CrewUnavailableError(RuntimeError):
    """Raised when FirstMate cannot spawn a crew task."""


def _run(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Single subprocess seam for tests and controlled external commands."""

    return subprocess.run(list(args), capture_output=True, text=True, check=False, **kwargs)


def _safe_spawn_error(error: object, *, returncode: Optional[int] = None) -> str:
    """Create bounded, non-sensitive diagnostics for a failed crew spawn.

    Do not persist subprocess arguments: they can contain absolute paths or
    launch details. Keep only stderr/stdout text, redact token-shaped values
    and home paths, normalize whitespace, and cap the result for the tracked
    registry.
    """

    if isinstance(error, subprocess.TimeoutExpired):
        raw = "spawn subprocess timed out"
        kind = "TimeoutExpired"
    elif isinstance(error, BaseException):
        raw = str(getattr(error, "stderr", None) or getattr(error, "stdout", None) or error)
        kind = type(error).__name__
    else:
        raw = str(error)
        kind = "SpawnError"

    redacted = _TOKEN_RE.sub("<redacted-token>", raw)
    redacted = redacted.replace(str(Path.home()), "<home>")
    redacted = _HOME_PATH_RE.sub("<absolute-home-path>", redacted)
    redacted = " ".join(redacted.split())
    if returncode is not None:
        redacted = f"returncode={returncode}: {redacted}"
    return f"{kind}: {redacted}"[:MAX_SPAWN_ERROR_CHARS]


def _crew_id(cycle_session_id: str, issue_number: int) -> str:
    return f"fm-{cycle_session_id}-{issue_number}"


def _task_dir(crew_id: str) -> Path:
    return DATA_DIR / crew_id


def _capability_path(crew_id: str) -> Path:
    return _task_dir(crew_id) / "capability.json"


def _capability_payload(capability: Optional[CapabilityBundle]) -> Optional[dict]:
    """Return bounded, JSON-safe capability metadata for one crew task."""
    if capability is None:
        return None
    payload = capability.to_dict()
    payload["schema_version"] = 1
    return payload


def _write_capability_file(crew_id: str, capability: Optional[CapabilityBundle]) -> Optional[Path]:
    payload = _capability_payload(capability)
    if payload is None:
        return None
    destination = _capability_path(crew_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return destination


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
    """Extract a complete branch/commit/base identity from bounded text.

    Accept both the machine-readable status form (``branch=...``) and the
    Markdown report form produced by Hermes (``## Branch`` followed by a
    backtick-wrapped bullet). In a ``## Base`` section, the nested commit is
    the base identity; the nested branch is descriptive and must not replace
    the task branch.
    """
    fields: dict[str, str] = {}
    section: Optional[str] = None

    def clean(value: str) -> str:
        return value.strip().strip("`*_[](){}<>.,;:")

    for line in text.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{1,6}\s+(branch|commit|base)\b", stripped, re.IGNORECASE)
        if heading:
            section = heading.group(1).lower()
            continue

        matches = list(_ARTIFACT_FIELD_RE.finditer(stripped))
        for match in matches:
            key = match.group(1).lower().replace(" ", "_").replace("-", "_")
            value = clean(match.group(2))
            if section == "base" and key in {"commit", "base", "base_ref", "base_commit"}:
                fields["base"] = value
            elif key in {"branch", "commit"} and section != "base":
                fields[key] = value
            elif key in {"base", "base_ref", "base_commit"}:
                fields["base"] = value

        # Markdown section values often appear as a bare bullet, e.g.
        # ``## Branch`` followed by ``- `fm/task-1````.
        if section in {"branch", "commit"} and not matches:
            bullet = re.match(r"^-?\s*`([^`]+)`\s*$", stripped)
            if bullet:
                fields[section] = clean(bullet.group(1))

    identity = {
        "branch": fields.get("branch", ""),
        "commit": fields.get("commit", ""),
        "base": fields.get("base", ""),
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


def _write_brief(
    crew_id: str,
    task_text: str,
    issue_number: int,
    project_dir: Path,
    capability: Optional[CapabilityBundle] = None,
) -> Path:
    destination = _task_dir(crew_id)
    destination.mkdir(parents=True, exist_ok=True)
    brief = destination / "brief.md"
    # Handoff protocol: the crewmate must be told the EXACT status-file and
    # report paths, or it cannot append the terminal `done:` line the poller
    # waits on. Observed 2026-08-12 (issue #50): the agent committed its work
    # and wrote the report into the disposable worktree (a commit) because the
    # brief never named the status file or the report path — the poll then
    # timed out and the bridge fell back to the direct path. These paths live
    # in FM-local runtime state (outside the repo), so absolute paths are safe
    # here and are exactly what fm-brief.sh embeds for native firstmate tasks.
    status_file = _status_path(crew_id)
    report_path = destination / "report.md"
    brief.write_text(
        "You are a crewmate: an autonomous worker agent managed by firstmate. "
        "Work on your own; do not wait for a human.\n\n"
        "# Task\n\n"
        f"Issue: #{issue_number}\n"
        # Keep the runtime brief useful without persisting the operator's
        # absolute home path into a durable artifact.
        f"Project: {project_dir.name}\n\n"
        f"{task_text.rstrip()}\n\n"
        "## Worktree\n\n"
        "You are in a disposable Orca git worktree of the project. Verify "
        "isolation first: run `pwd -P` and `git rev-parse --show-toplevel`; "
        "both must resolve to this disposable worktree, not a primary "
        "checkout. Create your branch with `git checkout -b "
        f"fm/{crew_id}` and work there. Never push to any remote and never "
        "open a PR.\n\n"
        "## Status file\n\n"
        "Report progress by appending one short line to the status file:\n\n"
        f"    {status_file}\n\n"
        "Use exactly these verbs: `working:`, `blocked:`, `needs-decision:`,"
        " `resolved:`, `done:`, `failed:`. Append `blocked:` when you are stuck "
        "and stop. Append `needs-decision:` only for human decisions and stop. "
        "Each append wakes the supervisor, so report sparingly: only phase "
        "changes and the terminal states.\n\n"
        "## Report\n\n"
        "Write your delivery report (what you changed, the checks you ran, and "
        "the evidence) to:\n\n"
        f"    {report_path}\n\n"
        "The report is the only artifact that survives teardown; the worktree "
        "is discarded.\n\n"
        "## Definition of done\n\n"
        "1. The requested code change is implemented and committed on your "
        "branch with a local commit.\n"
        "2. `report.md` exists at the report path above and names the branch, "
        "commit, and base identity.\n"
        "3. You append the final `done:` status naming the branch, commit, and "
        "base identity in this exact form:\n\n"
        "    done: branch=<branch> commit=<commit> base=<base>\n\n"
        "Then stop.\n"
    )
    if capability is not None:
        brief.write_text(
            brief.read_text(encoding="utf-8")
            + "\n## Capability contract\n\n"
            + "This is the school-selected launch policy. It is evidence of what "
            + "was requested, not proof that every tool was used.\n\n"
            + f"- Task role: {capability.task_role}\n"
            + f"- Hermes profile: {capability.profile}\n"
            + f"- School skill anchors: {', '.join(capability.skills) or '(none)'}\n"
            + f"- School tools: {', '.join(capability.allowed_tools)}\n"
            + f"- Hermes toolsets: {', '.join(capability.hermes_toolsets)}\n",
            encoding="utf-8",
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


def _spawn(
    crew_id: str,
    project_dir: Path,
    capability: Optional[CapabilityBundle] = None,
) -> subprocess.CompletedProcess:
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
    repo_wrapper = Path(__file__).resolve().parent / "scripts" / "hermes-fm-wrapper"
    default_wrapper = (
        repo_wrapper
        if repo_wrapper.is_file()
        else Path.home() / ".local/bin/hermes-fm-wrapper"
    )
    wrapper = os.environ.get("FM_WRAPPER", str(default_wrapper))
    harness = f'{wrapper} "$($__OPINPUT__ encode launch-brief < $__BRIEF__)"'
    # Export FM_HOME (and its state/data subdirs) so fm-spawn resolves the
    # same config/data/state directories this module writes briefs into and
    # polls for status. fm-spawn falls back to its OWN clone root when
    # FM_HOME is unset, so without this the spawn looks for the brief in
    # ~/.local/share/firstmate/data while _write_brief put it in FM_HOME/data
    # — 'no brief at .../firstmate/data/<id>/brief.md' (observed 2026-08-12,
    # issue #49).
    env = dict(os.environ)
    env["FM_HOME"] = str(FM_HOME)
    env["FM_STATE_OVERRIDE"] = str(STATE_DIR)
    env["FM_DATA_OVERRIDE"] = str(DATA_DIR)
    payload = _capability_payload(capability)
    if payload is not None:
        env.update({
            "FM_AGENT_CAPABILITY_VERSION": str(payload["schema_version"]),
            "FM_AGENT_TASK_ROLE": capability.task_role,
            "FM_AGENT_PROFILE": capability.profile,
            "FM_AGENT_SKILL_ANCHORS": ",".join(capability.skills),
            "FM_AGENT_ALLOWED_TOOLS": ",".join(capability.allowed_tools),
            "FM_AGENT_TOOLSETS": ",".join(capability.hermes_toolsets),
        })
    return _run([
        str(FM_SPAWN), crew_id, str(project_dir),
        "--mode", "local-only", "--yolo", "on", "--backend", "orca",
        "--harness", harness,
    ], timeout=SPAWN_TIMEOUT_SECONDS, env=env)


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
    capability: Optional[CapabilityBundle] = None,
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
    project_dir = Path(project_dir)
    _write_brief(crew_id, task_text, issue_number, project_dir, capability)
    _write_capability_file(crew_id, capability)
    started_at = datetime.now(timezone.utc).isoformat()
    capability_record = _capability_payload(capability)
    try:
        if capability is not None and not capability.hermes_toolsets:
            raise CrewUnavailableError("capability policy has no Hermes toolsets")
        result = _spawn(crew_id, project_dir, capability)
    except (CrewUnavailableError, OSError, subprocess.TimeoutExpired) as exc:
        spawn_error = _safe_spawn_error(exc)
        _record_run({
            "crew_id": crew_id,
            "issue_number": issue_number,
            "status": "spawn_failed",
            "fallback_reason": "spawn_failure",
            "spawn_error": spawn_error,
            "capability": capability_record,
            "started_at": started_at,
        })
        raise CrewUnavailableError(spawn_error) from exc
    if result.returncode != 0:
        spawn_error = _safe_spawn_error(
            (result.stderr or result.stdout or "fm-spawn failed").strip(),
            returncode=result.returncode,
        )
        _record_run({
            "crew_id": crew_id,
            "issue_number": issue_number,
            "status": "spawn_failed",
            "fallback_reason": "spawn_failure",
            "spawn_error": spawn_error,
            "capability": capability_record,
            "started_at": started_at,
        })
        raise CrewUnavailableError(spawn_error)

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
        "capability": capability_record,
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
        "capability": capability_record,
    })
    return CrewResult(
        crew_id=crew_id,
        status=terminal_status,
        report_path=report_path,
        fallback_reason=fallback_reason,
        teardown_ok=teardown_ok,
        orca_worktree_id=worktree_id,
        capability=capability_record,
    )
