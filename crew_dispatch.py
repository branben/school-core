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
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from dotenv import load_dotenv

# Load .env file from project root
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Also load from ~/.omniroute/.env if it exists (for OmniRoute keys)
OMNIRoute_ENV = Path.home() / ".omniroute" / ".env"
if OMNIRoute_ENV.exists():
    load_dotenv(OMNIRoute_ENV)

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
# Startup deadline for a crew that has produced NO recognised status verb yet.
# Spawn + worktree create + first agent append is seconds, not minutes, so 120s
# is generous. Deliberately far below DEFAULT_TIMEOUT: this only bounds SILENCE,
# never work. See _poll's docstring — every difficulty (diploma = 8 turns x 90s
# = 720s) still gets the full timeout once the crew speaks once.
DEFAULT_STARTUP_GRACE = float(os.environ.get("CREW_STARTUP_GRACE_SECONDS", "120"))
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
    r"(?i)\b(branch|commit|base(?:\s+identity)?(?:\s*\([^)]*\))?(?:[_ -](?:ref|commit))?)\s*[:=]\s*"
    r"[*_]*\s*([^\s]+)"
)

# Status-file verbs. Restricted to the SIX verbs the crew prompt documents
# (crew_dispatch.py:393) and _poll consumes (crew_dispatch.py:937-944):
#   working | blocked | needs-decision | resolved | done | failed
# Anything else is NOT a status verb. The previous pattern
#   ^\s*([A-Za-z][A-Za-z0-9_-]*)(?:\s+\[[^\]]*\])?\s*:
# matched ANY identifier before a colon, which caused two bugs:
#   (1) a real `done:`/`working:` line followed by a cleanup line such as
#       `original_done: done:` was read as verb "original_done" — never
#       terminal — so finished crews kept getting polled; and
#   (2) any stray `note: x` line was mistaken for a live verb instead of
#       being ignored.
# NOTE: `spawn_silent` and `timeout` are _poll RETURN CODES (lines 949/952),
# not status-file verbs — they are intentionally absent here. A file never
# contains them, and matching them would wrongly drop `needs-decision` /
# `resolved` crews into the "no recognised verb" path.
# A leading timestamp (full ISO-8601 or a bare HH:MM:SS[Z]) is optional and is
# NOT the verb: crews (or a wrapping supervisor) may prefix each line with one.
_STATUS_RE = re.compile(
    r"^\s*"
    r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+|"
    r"\d{2}:\d{2}:\d{2}Z?\s+)?"
    r"(?P<verb>working|blocked|needs-decision|resolved|done|failed)"
    r"(?:\s+\[[^\]]*\])?\s*:",
    re.I,
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
    artifact_identity: Optional[dict[str, object]] = None
    verification: Optional[dict] = None
    entire_review: Optional[dict] = None
    # B8 Phase 2 (bead school-core-3um): the crew's real diff, captured before
    # worktree teardown as text in the task dir (alongside report.md). The commit
    # itself cannot survive the disposable clone, so this patch is the only
    # durable record of what the crew actually changed. Carried here so the
    # bridge can forward it into the PR body instead of re-reading the ledger.
    patch_path: Optional[Path] = None


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
            return match.group("verb").lower(), line[match.end():].strip()
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
            if section == "base" and key in {"commit", "base", "base_ref", "base_commit", "base_identity"}:
                fields["base"] = value
            elif key in {"branch", "commit"} and section != "base":
                fields[key] = value
            elif key == "base" or key.startswith("base_"):
                fields["base"] = value

        # Markdown section values often appear as a bare bullet, e.g.
        # ``## Branch`` followed by ``- `fm/task-1````, or ``## Base``
        # followed by ``- `origin/main```.
        if section in {"branch", "commit", "base"} and not matches:
            bullet = re.match(r"^-?\s*`([^`]+)`\s*$", stripped)
            if bullet:
                fields[section] = clean(bullet.group(1))

    identity = {
        "branch": fields.get("branch", ""),
        "commit": fields.get("commit", ""),
        "base": fields.get("base", ""),
    }
    return identity if all(identity.values()) else None


def _revision_forms_match(left: str, right: str) -> bool:
    """Compare Git revisions while allowing short hashes and ref prefixes."""
    left = left.strip().strip("`*_[](){}<>").lower()
    right = right.strip().strip("`*_[](){}<>").lower()
    if left == right:
        return True

    # Reports commonly call the branch point ``main@<sha>`` while the status
    # line records only the resolved SHA (or vice versa).
    def revision_part(value: str) -> str:
        value = value.rsplit("@", 1)[-1]
        if value.startswith("refs/heads/"):
            value = value[len("refs/heads/"):]
        if value.startswith("origin/"):
            value = value[len("origin/"):]
        return value

    left_revision = revision_part(left)
    right_revision = revision_part(right)
    if re.fullmatch(r"[0-9a-f]{7,40}", left_revision) and re.fullmatch(
        r"[0-9a-f]{7,40}", right_revision
    ):
        return left_revision.startswith(right_revision) or right_revision.startswith(left_revision)

    # ``main`` and ``origin/main`` are the same logical base ref in a local
    # Orca worktree; keep this alias narrow rather than accepting arbitrary
    # substrings.
    def ref_name(value: str) -> str:
        value = value.removeprefix("refs/heads/")
        return value.removeprefix("origin/")

    return ref_name(left) == ref_name(right)


def _artifact_identities_match(left: Optional[dict[str, str]], right: Optional[dict[str, str]]) -> bool:
    """Return whether two complete artifact identities describe one commit.

    FirstMate status and Hermes reports are produced by different writers. One
    may use a full SHA while the other uses a short SHA or an explicit local
    remote ref. Branch names remain exact after harmless ``refs/heads/``
    normalization; only Git revision representation is relaxed.
    """
    if not left or not right:
        return False
    left_branch = left.get("branch", "").removeprefix("refs/heads/")
    right_branch = right.get("branch", "").removeprefix("refs/heads/")
    return (
        bool(left_branch and right_branch and left_branch == right_branch)
        and _revision_forms_match(left.get("commit", ""), right.get("commit", ""))
        and _revision_forms_match(left.get("base", ""), right.get("base", ""))
    )


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
        "## Safety guard\n\n"
        "NEVER run `git stash`, `git checkout` (outside your own branch), "
        "`git reset`, or `git clean` outside this worktree. If you need to "
        "inspect another branch or PR, use `git fetch origin <ref>` and "
        "read files with `git show` instead of checking out. Violating this "
        "will corrupt the live repository.\n\n"
        "## Status file\n\n"
        "Report progress by appending one short line to the status file:\n\n"
        f"    {status_file}\n\n"
        "Use exactly these verbs: `working:`, `blocked:`, `needs-decision:`, "
        "`resolved:`, `done:`, `failed:`. Append `blocked:` when you are stuck "
        "and stop. Append `needs-decision:` only for human decisions and stop. "
        "Write `working:` at each major phase change (branch created, "
        "implementation complete, tests run) — the supervisor uses these "
        "to distinguish active work from silent failure.\n\n"
        "## Report\n\n"
        "Write your delivery report (what you changed, the checks you ran, and "
        "the evidence) to:\n\n"
        f"    {report_path}\n\n"
        "The report is the only artifact that survives teardown; the worktree "
        "is discarded.\n\n"
        "## Definition of done\n\n"
        "1. The requested code change is implemented and committed on your "
        "branch with a local commit.\n"
        "2. `report.md` exists at the report path above and ENDS with an "
        "identity block in exactly this form (the supervisor parses it; prose "
        "descriptions of the branch/commit/base are NOT accepted):\n\n"
        "    ## Identity\n"
        "    - branch: `<branch>`\n"
        "    - commit: `<commit>`\n"
        "    - base: `<base>`\n\n"
        "   All three lines are required. Use the SAME branch and commit values "
        "you report in the `done:` status line below — the supervisor compares "
        "the two and rejects the task if they disagree. Do not nest the task "
        "branch under a `## Base` heading; anything under `## Base` is read as "
        "the base identity only.\n"
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
    # N3.2 (worst-day-ever): stamp a monotonic start so stale-sweep is immune to
    # wall-clock skew. The human-readable ISO ``started_at`` is kept for ops; the
    # monotonic value drives all age math.
    if entry.get("status") in {"running", "blocked"} and "started_monotonic" not in entry:
        entry = dict(entry)
        entry["started_monotonic"] = time.monotonic()
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


# Upper bound on a captured crew patch. A runaway diff must not be held in
# memory unbounded nor committed to a PR. Matches the spirit of
# MAX_REPORT_BYTES: an oversized artifact is a failure, not a large success.
MAX_PATCH_BYTES = 2_000_000

# Refs come from model-authored status text, so they are untrusted input and must
# never reach a git argument unvalidated. Conservative allowlist: hex shas,
# branch/tag names, and `owner/branch@sha` as emitted in `base=` lines.
_SAFE_REF_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/@-]{0,200}$")


def capture_crew_patch(
    worktree_path: Path,
    base: str,
    destination: Path,
) -> Optional[Path]:
    """Capture the crew's diff as a patch file, or return None.

    THE PROBLEM THIS SOLVES: 54 crews emitted valid ``done: commit=<sha>`` lines
    and every one of those commits is now unreachable. The crew works inside a
    disposable clone that is reset between runs and a worktree that is deleted on
    teardown, so the branch ref vanishes and the commit is orphaned. The work was
    real and good; nothing preserved it.

    MUST be called BEFORE teardown_worktree.

    SUPERVISOR-SIDE BY DESIGN. The reviewed alternative was to instruct the agent
    to write its own patch. This runs ``git diff`` from the supervisor instead,
    because agent compliance is exactly the failure mode this system already has:
    the artifact handshake asks agents to emit a parseable identity block and
    #342 emitted nothing at all. A supervisor-side capture has no compliance
    dependency — if the worktree has changes, the patch exists.

    A PATCH, NOT A BRANCH HANDOFF. Two premises were refuted in review: the crew
    worktree does NOT share an object store with any persistent clone
    (``issue_bridge`` calls ``repo_reader.clone_repo``, a separate clone), and
    that clone is ``git clone --depth 1`` and verified shallow, so fetching from
    it can graft history and the ``base=`` sha may not exist there. A patch is
    text and has neither dependency.

    EMPTY IS A FAILURE, NOT A SUCCESS. Returns None — and writes nothing — when
    the diff is empty. ``pr_creator``'s only emptiness guard catches blob-creation
    failure, not an empty diff, so a silently-empty capture would produce a PR
    with no diff that nothing vetoes. That would be worse than today's honest
    loss, so an empty capture must be loud and must leave no 0-byte file behind
    for a later reader to mistake for preserved work.
    """
    if not _SAFE_REF_RE.match(base or ""):
        log.warning("refusing unsafe base ref for patch capture: %r", base)
        return None
    if not worktree_path.is_dir():
        log.warning("worktree missing, cannot capture patch: %s", worktree_path)
        return None
    try:
        proc = _run(
            [
                "git", "-C", str(worktree_path),
                "diff", "--binary", f"{base}...HEAD",
            ],
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("patch capture failed for %s: %s", worktree_path, exc)
        return None
    if proc.returncode != 0:
        log.warning(
            "git diff failed in %s (base=%s): %s",
            worktree_path, base, (proc.stderr or "").strip()[:200],
        )
        return None

    patch = proc.stdout or ""
    if not patch.strip():
        # Loud, and no file written. An empty patch that looks like an artifact
        # is how a green PR with no diff gets shipped.
        log.warning(
            "crew produced NO diff against %s — nothing to preserve. Not writing "
            "an empty patch; an empty artifact must never read as success.",
            base,
        )
        return None
    if len(patch.encode("utf-8", "replace")) > MAX_PATCH_BYTES:
        log.warning(
            "captured patch exceeds MAX_PATCH_BYTES (%d) — refusing to preserve "
            "an unbounded diff", MAX_PATCH_BYTES,
        )
        return None

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(patch, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write patch to %s: %s", destination, exc)
        return None
    log.info("captured crew patch (%d bytes) -> %s", len(patch), destination)
    return destination


def _record_artifact_reachability(
    crew_id: str,
    artifact_identity: Optional[dict],
    worktree_id: Optional[str],
) -> Optional[bool]:
    """Record whether the crew's cited commit actually resolves.

    MUST be called BEFORE teardown_worktree. The commit lives in the disposable
    worktree's clone, which is reset between runs and deleted on teardown — so
    probing afterwards would report every commit unreachable and prove nothing
    about whether the work was real.

    ADDITIVE ONLY. This never writes ``status`` or ``fallback_reason``: U10
    (see the checkpoint below) makes the terminal outcome authoritative, and an
    unreachable commit must not silently downgrade a crew that genuinely
    finished. The reachability answer is its own field so a reader can see both
    "the crew completed" and "its commit no longer resolves" — which is the true
    state of every one of the 54 done-crews found on disk.

    Returns the tri-state so callers can log it; None means "not determined"
    (no identity cited, no worktree, or git could not look). Never raises:
    recording the outcome matters more than probing it.
    """
    reachable: Optional[bool] = None
    try:
        commit = (artifact_identity or {}).get("commit")
        if commit and worktree_id and "::" in worktree_id:
            worktree_path = Path(worktree_id.split("::", 1)[1])
            reachable = commit_is_reachable(str(commit), worktree_path)
        if reachable is False:
            log.warning(
                "%s: cited commit %s does NOT resolve in its own worktree "
                "clone — the work is not preserved anywhere. Recording the "
                "outcome as-is; the record must not claim evidence it cannot "
                "produce.",
                crew_id,
                commit,
            )
        _update_run(crew_id, {"commit_reachable": reachable})
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "%s: reachability probe failed (%s); outcome record is unaffected",
            crew_id,
            exc,
        )
    return reachable


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
    now_monotonic: Optional[float] = None,
    stale_after: float = DEFAULT_TIMEOUT,
    path: Optional[Path] = None,
) -> int:
    """Remove old running/blocked records and best-effort reclaim their Orca worktrees.

    N3.2 (worst-day-ever): when ``now_monotonic`` is supplied, age is measured on
    the monotonic clock (immune to wall-clock skew between the Mac and GitHub).
    Falls back to the wall-clock ``now`` path (legacy) when monotonic is absent.
    """

    with _registry_lock(path):
        runs = _load_runs(path)
        kept: list[dict] = []
        removed = 0
        changed = False
        for entry in runs:
            # Prefer monotonic age when both endpoints are monotonic.
            if now_monotonic is not None and entry.get("started_monotonic") is not None:
                age = now_monotonic - float(entry["started_monotonic"])
            else:
                started = _started_at(entry)
                current = time.time() if now is None else now
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


def _read_dotenv_value(path: Path, name: str) -> str:
    """Best-effort read of a ``NAME=VALUE`` line from a dotenv file.

    Dependency-free (the project does not use python-dotenv). Skips comments
    and blank lines; strips optional surrounding quotes.
    """
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def _read_yaml_omniroute_key(path: Path) -> str:
    """Best-effort extraction of an OmniRoute key from a Hermes config.yaml.

    Hermes persists provider keys under ~/.hermes/config.yaml. We do NOT pull
    in a YAML parser; a couple of tolerant regex scans cover the documented
    shapes (a top-level ``OMNIROUTE_API_KEY:`` and a nested
    ``openrouter:``/``api_key:`` block — the latter because some users store
    the key there under the openrouter provider block). Returns "" when nothing matches.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"^OMNIROUTE_API_KEY:\s*\"?([^\"\n]+)\"?", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    m = re.search(r"openrouter:.*?api_key:\s*\"?([^\"\n]+)\"?", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _omniroute_api_key() -> str:
    """Resolve ``OMNIROUTE_API_KEY`` for the spawned crew agent.

    The bridge process carries the key in its environment, but it is lost by
    the time the spawned Hermes starts (the spawn crosses a process boundary
    and/or fm-spawn.sh rebuilds the child env). We forward it explicitly so
    the crew can reach OmniRoute at localhost:20128.

    Source order: live ``os.environ`` -> the repo ``.env`` (the project's
    secrets store) -> Hermes ``config.yaml`` if the key was persisted there.
    Returns "" when nowhere found.
    """
    key = (os.environ.get("OMNIROUTE_API_KEY") or "").strip()
    if key:
        return key
    key = _read_dotenv_value(
        Path(__file__).resolve().parent / ".env", "OMNIROUTE_API_KEY"
    )
    if key:
        return key
    cfg = Path.home() / ".hermes" / "config.yaml"
    if cfg.is_file():
        key = _read_yaml_omniroute_key(cfg)
        if key:
            return key
    return ""


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
    # fm-spawn.sh substitutes the bare tokens __BRIEF__/__OPINPUT__ itself
    # (replacing them with the real brief path and opinput binary). The
    # placeholders MUST stay bare — a leading '$' makes bash try to expand
    # "$__BRIEF__" as an undefined variable, which resolves to the empty
    # string and leaves the wrapper with no brief (observed 2026-08-18).
    harness = f'{wrapper} "$(__OPINPUT__ encode launch-brief < __BRIEF__)"'

    # --- API-key injection ----------------------------------------------------
    # fm-spawn.sh forwards only FM_* variables into the pane; everything else
    # from the bridge's os.environ (including OPENROUTER_API_KEY) is dropped
    # at the pane boundary. The pane is a fresh shell, so without the key
    # Hermes has no credential for its first model call and the crew sits
    # silent until startup_grace fires. Prepend the key to the launch command
    # as an export; shlex.quote keeps it shell-safe. The key is only ever set
    # in the bridge's environment, never committed.
    #
    # Source the key from the bridge's live os.environ, then fall back to the
    # repo .env and Hermes config.yaml (_omniroute_api_key). Sourcing ONLY
    # from os.environ silently no-ops whenever school-core runs as a separate
    # process that did not inherit the bridge's environment — which is exactly
    # the "present in the bridge, lost by spawn time" failure. The multi-source
    # resolver restores it.
    # --- Supervision-path injection (same pane-boundary problem as the key) ---
    # FM_STATUS_FILE / FM_REPORT_FILE are set in the `env=` dict below, which
    # reaches fm-spawn.sh's PROCESS environment — but NOT the crew's pane.
    # fm-spawn.sh hands the launch command to the pane as literal typed text
    # (fm-spawn.sh:2638 spawn_send_literal) and forwards only the FM_* vars in
    # its own hand-built prefix at fm-spawn.sh:2591; FM_STATUS_FILE is absent
    # from that list, and the string appears nowhere in firstmate's bin/ (it is
    # school-core's own convention). So the wrapper saw it EMPTY and skipped its
    # entire status-write block, which is gated on
    # `[[ -n "${FM_STATUS_FILE:-}" ]]` (hermes-fm-wrapper:97).
    #
    # EVIDENCE: 0 of 366 dispatched crews ever produced the wrapper's
    # `failed: hermes-exit-<code>-no-terminal-status` post-mortem line, while a
    # direct wrapper invocation with the variable set produces it immediately.
    # The 122 crews that DID write status files read the path out of the brief
    # prose instead (brief.md names it; fm-brief.sh:179 templates it in) — which
    # is why the split was 122/244 rather than all-or-nothing: it tracked whether
    # the model followed a path buried in text, not any transport property.
    #
    # This runs BEFORE the key injection below so the key export stays the
    # OUTERMOST prefix: test_spawn_harness_exports_omniroute_key_when_resolvable
    # asserts harness.startswith("export OMNIROUTE_API_KEY="), and prepending
    # after it would turn that guard red for a reason unrelated to the key.
    harness = (
        f"export FM_STATUS_FILE={shlex.quote(str(_status_path(crew_id)))} "
        f"FM_REPORT_FILE={shlex.quote(str(_task_dir(crew_id) / 'report.md'))}; "
        f"{harness}"
    )

    key = _omniroute_api_key()
    if key:
        harness = f"export OMNIROUTE_API_KEY={shlex.quote(key)}; {harness}"

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
    # U10 deterministic handshake: hand the wrapper the EXACT supervised
    # artifact/status paths so it can append a bounded terminal status line
    # (never a passing report) when Hermes exits without one. The supervisor
    # polls these same paths, so the launch contract and the poll contract
    # cannot drift.
    env["FM_STATUS_FILE"] = str(_status_path(crew_id))
    env["FM_REPORT_FILE"] = str(_task_dir(crew_id) / "report.md")
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


def commit_is_reachable(sha: str, repo_path: Path) -> Optional[bool]:
    """Does ``sha`` resolve to a real commit object in ``repo_path``?

    Returns True / False / None, where None means "could not determine" — the
    repo is missing, not a repo, or git failed. Collapsing None into False would
    let a tooling failure read as evidence the crew's work was lost, the same
    UNKNOWN-as-verdict trap fixed in the review gates.

    WHY THIS EXISTS: 54 crews emitted ``done: ... commit=<sha>`` lines whose
    objects do not exist anywhere — not in either primary clone, not in the
    crew's own clone. The crew's disposable clone is reset between runs (its
    reflog shows repeated ``reset: moving to b0075d74…``) and the worktree is
    deleted, so the branch ref vanishes and the commit is orphaned. The SHA was
    real when written and unreachable minutes later.

    A record asserting an unverifiable hash is worse than one admitting it has
    no evidence, because a reader will trust the hash.
    """
    candidate = (sha or "").strip()
    # Reject anything that is not a plausible hex object name BEFORE shelling
    # out: `sha` arrives from model-authored status text, so it is untrusted
    # input and must never reach a subprocess argument unvalidated.
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", candidate):
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "cat-file", "-t", candidate],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return proc.stdout.strip() == "commit"
    # Distinguish "git ran and said no" from "git could not look".
    # Exact wordings observed on macOS git:
    #   missing dir  -> "fatal: cannot change to '<path>': No such file or directory"
    #   not a repo   -> "fatal: not a git repository (or any of the parent directories)"
    stderr = (proc.stderr or "").lower()
    if (
        "cannot change to" in stderr
        or "not a git repository" in stderr
        or "does not exist" in stderr
    ):
        return None
    return False


def _poll(
    crew_id: str,
    *,
    timeout: float,
    poll_interval: float,
    blocked_grace: float,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
    startup_grace: float = DEFAULT_STARTUP_GRACE,
) -> tuple[str, Optional[str], str]:
    """Wait for a crew's terminal status.

    STARTUP GRACE (``spawn_silent``): a crew that has not produced ANY recognised
    status verb within ``startup_grace`` seconds is declared dead immediately.

    Why this is needed: ``_read_status_detail`` returns ``None`` both when the
    status file is missing and when it holds no recognised verb, and this loop
    otherwise treats ``None`` exactly like ``working`` — so a crew whose spawn
    silently failed, or that died before its first append, is indistinguishable
    from one doing useful work. Both consume the full timeout. Issue #342 burned
    ~16 minutes that way on the first crew admission that ever succeeded.

    The grace window is deliberately NARROWER than ``timeout`` and only applies
    BEFORE the first sign of life. Once the crew speaks even once, the full
    deadline governs, so long legitimate work is unaffected — a diploma task
    needs 8 turns x 90s = 720s and must still be allowed to finish.

    Deliberately NOT solved by lowering CREW_TIMEOUT_SECONDS: every difficulty
    already fits inside 900s (diploma ~750s with spawn overhead), so the ceiling
    is not the problem. Cutting it to catch silent failures would forbid the
    school's hardest difficulty. Fail fast on silence; stay patient with work.
    """
    started = now_fn()
    blocked_at: Optional[float] = None
    spoke = False
    mid_work = False
    poll_sleep = poll_interval if poll_interval > 0 else min(max(timeout, 0.1), 0.1)
    max_attempts = max(1, int(max(timeout, 0.0) / max(poll_sleep, 0.1)) + 2)
    attempts = 0
    while now_fn() - started <= timeout and attempts < max_attempts:
        attempts += 1
        status, detail = _read_status_detail(_status_path(crew_id))
        if status is not None:
            spoke = True
        if status in {"done", "failed", "resolved"}:
            return status, None, detail
        if status in {"blocked", "needs-decision"}:
            blocked_at = blocked_at if blocked_at is not None else now_fn()
            if now_fn() - blocked_at >= blocked_grace:
                return "blocked", "blocked", detail
        elif status == "resolved":
            blocked_at = None
            mid_work = True
        elif status == "working":
            mid_work = True
        # A crew that has never spoken is not "working" — it is missing. Cut it
        # loose at the startup deadline instead of reserving the whole cycle
        # budget for a process that may not exist.
        if not spoke and now_fn() - started >= startup_grace:
            return "timeout", "spawn_silent", detail
        # working, paused, resolved, unknown, and absent status all remain live.
        sleep_fn(poll_sleep)
    # Timeout fired. If the crew wrote working:/resolved: but never reached
    # a terminal state, return blocked (recoverable) so the supervisor
    # preserves the worktree. Genuine silence and needs-decision stay timeout.
    if mid_work:
        return "blocked", "poll_timeout_mid_work", detail
    return "timeout", "timeout", ""


def _run_premerge_sensors(
    worktree_id: str,
) -> tuple[Optional[dict], Optional[dict]]:
    """Run verification sensors against the live student worktree.

    ``dispatch_crew`` owns the student worktree lifecycle, so these checks must
    happen before teardown. Running them later against the cached target would
    verify the clean base rather than the submitted patch.
    """
    if "::" not in worktree_id:
        return None, None
    worktree_path = Path(worktree_id.split("::", 1)[1])
    if not worktree_path.is_dir():
        return None, None

    verification: Optional[dict] = None
    try:
        from verify_gate import run_verify_gate

        verification = run_verify_gate(
            worktree_path,
            flake_path=Path(__file__).resolve().parent,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime boundary
        log.exception("pre-merge verify failed for %s: %s", worktree_path, exc)
        verification = {
            "passed": False,
            "skipped": False,
            "failures": [{
                "cmd": "(crew_premerge_verify)",
                "exit": None,
                "stderr": str(exc)[:1500],
            }],
            "ran": 0,
        }

    entire_review: Optional[dict] = None
    try:
        from src.entire_review import run_entire_review

        entire_review = run_entire_review(str(worktree_path), base_branch="main")
    except Exception as exc:  # pragma: no cover - defensive runtime boundary
        log.exception("pre-merge Entire review failed for %s: %s", worktree_path, exc)
        entire_review = {
            "status": "error",
            "findings": [],
            "error": str(exc)[:1500],
        }

    return verification, entire_review


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
    fleet_worktree_id: Optional[str] = None,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> CrewResult:
    """Spawn, poll, collect, and clean up one code-producing FirstMate task.

    ``fleet_worktree_id`` is the logical fleet slot (e.g. ``wt-1``) the dispatch
    office leased for this crew. It is recorded in the durable ``running`` entry
    so ``FleetRegistry.assign_worktree`` can treat the slot as occupied for the
    crew's whole async lifecycle — not just the synchronous spawn window. Without
    this, the fleet lease (released right after spawn) leaves no durable trace,
    and ``assign_worktree`` reassigns the same logical slot to the next crew
    (N6.2 regression: parallel assignments must see the slot held).
    """

    crew_id = _crew_id(cycle_session_id, issue_number)
    sweep_stale_runs(now=now_fn(), path=CREW_RUNS_FILE)
    project_dir = Path(project_dir)
    _write_brief(crew_id, task_text, issue_number, project_dir, capability)
    _write_capability_file(crew_id, capability)
    started_at = datetime.now(timezone.utc).isoformat()
    capability_record = _capability_payload(capability)
    # B9 silent-agent diagnostic: capture the agent's spawn-time stderr so a
    # crew that launches cleanly (returncode 0) but writes no status file can
    # be traced. Empty until _spawn returns.
    spawn_stderr = ""
    try:
        if capability is not None and not capability.hermes_toolsets:
            raise CrewUnavailableError("capability policy has no Hermes toolsets")
        result = _spawn(crew_id, project_dir, capability)
        # B9: keep the agent's spawn-time stderr even on the success path.
        # A returncode-0 spawn with no later status file is the silent-agent
        # signature; without this capture the silence is untraceable.
        spawn_stderr = (result.stderr or "").strip()
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
        "fleet_worktree_id": fleet_worktree_id,
        "capability": capability_record,
        "started_at": started_at,
    })

    artifact_identity: Optional[dict[str, object]] = None
    verification_result: Optional[dict] = None
    entire_review_result: Optional[dict] = None
    try:
        terminal_status, fallback_reason, status_detail = _poll(
            crew_id,
            timeout=timeout,
            poll_interval=poll_interval,
            blocked_grace=blocked_grace,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        # Re-read the status file to capture the wrapper's post-exit handshake.
        # The wrapper writes blocked:/failed: AFTER Hermes exits, which can
        # land after the poll loop's timeout fires. Only apply when poll returned
        # a non-terminal status — don't overwrite a valid terminal result.
        if terminal_status not in {"done", "failed", "blocked", "needs-decision", "resolved"}:
            _post_exit_status = _read_status(_status_path(crew_id))
            if _post_exit_status in {"done", "failed", "blocked", "needs-decision"}:
                terminal_status = _post_exit_status
        # B9: a crew that spawned without error yet produced no status file
        # within startup_grace is a *silent agent*, not a generic spawn
        # failure. Rename the code so it is distinguishable in the ledger;
        # its spawn-time stderr is attached to the record below.
        if fallback_reason == "spawn_silent":
            fallback_reason = "silent_agent"

        # Attribution join (bead school-core-a9s): the .meta file carries
        # project= at spawn time; join it into the .status file so every
        # status record is self-verifying (the cited SHA can be checked
        # in the named repo). Skip silently when .meta is absent or has
        # no project= line — not every spawn publishes one.
        meta_path = _meta_path(crew_id)
        if meta_path.exists():
            for line in meta_path.read_text().splitlines():
                if line.startswith("project="):
                    status_path = _status_path(crew_id)
                    with open(status_path, "a") as f:
                        f.write(f"repo={line.split('=', 1)[1]}\n")
                    break

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
                        if status_identity and report_identity and _artifact_identities_match(
                            status_identity, report_identity
                        ):
                            report_path = candidate
                            artifact_identity = report_identity
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
        elif terminal_status == "resolved":
            # The crew determined the work was already satisfied. This is a
            # terminal success state — the crew did its job by identifying
            # that no changes were needed. Capture the report if present.
            fallback_reason = "already_satisfied"
            candidate = _task_dir(crew_id) / "report.md"
            if candidate.exists():
                try:
                    if candidate.stat().st_size <= MAX_REPORT_BYTES:
                        report_path = candidate
                        report_text = candidate.read_text(encoding="utf-8")
                        report_identity = _artifact_identity(report_text)
                        if report_identity:
                            artifact_identity = report_identity
                except OSError:
                    pass
        elif terminal_status == "failed":
            fallback_reason = "crew_failed"
    except Exception as exc:
        # U10: an unexpected supervisor-side failure during polling or artifact
        # collection must still land a terminal state in the registry with a
        # bounded reason; the bridge's direct-Orca fallback then sees a
        # deterministic record instead of a stale `running` entry.
        log.exception("crew %s supervisor failed during lifecycle: %s", crew_id, exc)
        terminal_status = "failed"
        fallback_reason = "supervisor_unexpected"
        report_path = None

    # Verify the submitted patch while its disposable worktree is still alive.
    # The bridge reuses these authoritative results after this function tears
    # the worktree down; it must never silently verify the clean target base.
    if terminal_status == "done" and report_path and worktree_id:
        verification_result, entire_review_result = _run_premerge_sensors(worktree_id)
        if artifact_identity and "::" in worktree_id:
            try:
                from src.entire_review import _get_changed_files
                worktree_path = worktree_id.split("::", 1)[1]
                artifact_identity = {
                    **artifact_identity,
                    "changed_files": _get_changed_files(worktree_path, "main"),
                }
            except Exception:
                # The authoritative sensors have already run; missing optional
                # file inventory must not turn a verified result into a crash.
                pass

    # U10: persist the terminal state BEFORE teardown so a cleanup failure can
    # never mask or lose the outcome record. teardown_ok is then recorded as a
    # second update so failed cleanup stays visible without hiding the result.
    _update_run(crew_id, {
        "status": terminal_status,
        "fallback_reason": fallback_reason,
        # B9: attach the agent's spawn-time stderr when it went silent, so the
        # silence is traceable. Redacted + capped like spawn_failure's error.
        "spawn_stderr": (
            _safe_spawn_error(spawn_stderr)
            if fallback_reason == "silent_agent" and spawn_stderr
            else None
        ),
        # Keep the durable registry portable: callers still receive the
        # absolute runtime Path, but the checkpointable record stores only a
        # path relative to FM_DATA.
        "report_path": (
            str(report_path.relative_to(DATA_DIR))
            if report_path else None
        ),
        "capability": capability_record,
        "verification": verification_result,
        "entire_review": entire_review_result,
    })
    # Probe reachability while the worktree STILL EXISTS — its clone is deleted
    # by the teardown below, and probing afterwards would report every commit
    # unreachable regardless of whether the work was real. Additive only:
    # this writes commit_reachable and never touches status/fallback_reason, so
    # an orphaned commit cannot downgrade a crew that genuinely finished.
    _record_artifact_reachability(crew_id, artifact_identity, worktree_id)
    # Preserve the WORK, not just the record (bead school-core-3um). The commit
    # itself cannot survive — the disposable clone is reset between runs — so
    # capture the diff as text into the task dir, which outlives teardown
    # alongside report.md. Supervisor-side on purpose: no agent compliance
    # required. An empty capture returns None and writes nothing, so a failed
    # capture can never masquerade as preserved work.
    patch_path: Optional[Path] = None
    if worktree_id and "::" in worktree_id and artifact_identity:
        base_ref = str(artifact_identity.get("base") or "").split("@")[-1] or "main"
        patch_path = capture_crew_patch(
            worktree_path=Path(worktree_id.split("::", 1)[1]),
            base=base_ref,
            destination=_task_dir(crew_id) / "changes.patch",
        )
        _update_run(crew_id, {
            "patch_path": (
                str(patch_path.relative_to(DATA_DIR)) if patch_path else None
            ),
        })
    teardown_ok = False
    if terminal_status == "blocked":
        # Tri-state: blocked means mid-work (recoverable). Preserve the
        # worktree so the next dispatch can pick up where this one left off.
        log.info("crew %s blocked (mid-work) — preserving worktree", crew_id)
    else:
        teardown_ok = teardown_worktree(worktree_id)
    _update_run(crew_id, {"teardown_ok": teardown_ok})
    return CrewResult(
        crew_id=crew_id,
        status=terminal_status,
        report_path=report_path,
        fallback_reason=fallback_reason,
        teardown_ok=teardown_ok,
        orca_worktree_id=worktree_id,
        capability=capability_record,
        artifact_identity=artifact_identity,
        verification=verification_result,
        entire_review=entire_review_result,
        patch_path=patch_path,
    )
