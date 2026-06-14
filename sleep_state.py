"""
Sleep/Wake State Schema & Sequence — Phase 4.

Defines data structures and serialization for sleep/wake lifecycle:
- SleepState: full session state persisted to JSON
- ConsolidationArtifact: YAML summary of compressed episodic context
- Library Log: append-only YAML audit trail of sleep/wake cycles
- execute_sleep(): 6-step sleep sequence
- execute_wake(): 3-step wake sequence
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

# ── Paths ────────────────────────────────────────────────────────────────────

SESSIONS_DIR = Path(__file__).parent / "data" / "sessions"
CONSOLIDATION_DIR = Path(__file__).parent / "data" / "sessions" / "consolidation"
LIBRARY_LOG_PATH = Path(__file__).parent / "data" / "sessions" / "library_log.yaml"


# ── Exceptions ───────────────────────────────────────────────────────────────

class SessionError(Exception):
    """Base exception for session-related errors."""


class SessionNotFoundError(SessionError):
    """Raised when a session file does not exist."""


class SessionCorruptedError(SessionError):
    """Raised when session JSON is invalid or corrupted."""


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class SleepState:
    session_id: str
    agent: str
    building: str = "default"
    task_queue: list = field(default_factory=list)
    scores_snapshot: dict = field(default_factory=dict)
    layer_0: dict = field(default_factory=dict)
    layer_2_summary: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SleepState":
        return cls(
            session_id=data["session_id"],
            agent=data["agent"],
            building=data.get("building", "default"),
            task_queue=data.get("task_queue", []),
            scores_snapshot=data.get("scores_snapshot", {}),
            layer_0=data.get("layer_0", {}),
            layer_2_summary=data.get("layer_2_summary", ""),
            timestamp=data.get("timestamp", ""),
        )


@dataclass
class ConsolidationArtifact:
    session_id: str
    agent: str
    duration_minutes: float = 0.0
    tasks_completed: int = 0
    domains_visited: list = field(default_factory=list)
    key_decisions: list = field(default_factory=list)
    patterns_observed: list = field(default_factory=list)
    failed_approaches: list = field(default_factory=list)
    compressed_output_size_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConsolidationArtifact":
        return cls(
            session_id=data["session_id"],
            agent=data["agent"],
            duration_minutes=data.get("duration_minutes", 0.0),
            tasks_completed=data.get("tasks_completed", 0),
            domains_visited=data.get("domains_visited", []),
            key_decisions=data.get("key_decisions", []),
            patterns_observed=data.get("patterns_observed", []),
            failed_approaches=data.get("failed_approaches", []),
            compressed_output_size_tokens=data.get("compressed_output_size_tokens", 0),
        )


# ── Session Persistence (JSON) ───────────────────────────────────────────────

def _ensure_sessions_dir() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def save_session(state: SleepState, path: Optional[str] = None) -> str:
    """Serialize SleepState to JSON. Returns filepath."""
    _ensure_sessions_dir()
    filepath = Path(path) if path else SESSIONS_DIR / f"{state.session_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
    return str(filepath)


def load_session(session_id: str, path: Optional[str] = None) -> SleepState:
    """Deserialize SleepState from JSON. Raises SessionNotFoundError or SessionCorruptedError."""
    filepath = Path(path) if path else SESSIONS_DIR / f"{session_id}.json"
    if not filepath.exists():
        raise SessionNotFoundError(f"Session '{session_id}' not found at {filepath}")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise SessionCorruptedError(f"Session '{session_id}' corrupted: {e}")
    return SleepState.from_dict(data)


# ── Consolidation Artifact (YAML) ────────────────────────────────────────────

def _ensure_consolidation_dir() -> None:
    CONSOLIDATION_DIR.mkdir(parents=True, exist_ok=True)


def save_consolidation(artifact: ConsolidationArtifact, path: Optional[str] = None) -> str:
    """Serialize ConsolidationArtifact to YAML. Returns filepath."""
    _ensure_consolidation_dir()
    filepath = Path(path) if path else CONSOLIDATION_DIR / f"{artifact.session_id}.yaml"
    with open(filepath, "w", encoding="utf-8") as f:
        yaml.dump(artifact.to_dict(), f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return str(filepath)


def load_consolidation(session_id: str, path: Optional[str] = None) -> ConsolidationArtifact:
    """Load YAML consolidation artifact."""
    filepath = Path(path) if path else CONSOLIDATION_DIR / f"{session_id}.yaml"
    if not filepath.exists():
        raise SessionNotFoundError(f"Consolidation '{session_id}' not found at {filepath}")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SessionCorruptedError(f"Consolidation '{session_id}' corrupted: {e}")
    if data is None:
        raise SessionCorruptedError(f"Consolidation '{session_id}' is empty")
    return ConsolidationArtifact.from_dict(data)


# ── Library Log (append-only YAML) ───────────────────────────────────────────

def _ensure_library_log_dir() -> None:
    LIBRARY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def append_library_log(entry: dict, path: Optional[str] = None) -> None:
    """Append a sleep/wake log entry to the Library Log (YAML document)."""
    _ensure_library_log_dir()
    log_path = Path(path) if path else LIBRARY_LOG_PATH
    entry = {**entry, "timestamp": entry.get("timestamp", datetime.now(timezone.utc).isoformat())}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("---\n")
        yaml.dump(entry, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def read_library_log(path: Optional[str] = None) -> list:
    """Read all entries from the Library Log."""
    log_path = Path(path) if path else LIBRARY_LOG_PATH
    if not log_path.exists():
        return []
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    entries = []
    for doc in yaml.safe_load_all(content):
        if doc is not None:
            entries.append(doc)
    return entries


# ── Sleep Sequence ───────────────────────────────────────────────────────────

def execute_sleep(
    session_id: str,
    agent: str,
    store: "ScoreStore",
    building: str = "default",
    task_queue: Optional[list] = None,
    layer_0: Optional[dict] = None,
    episodic_history: Optional[list] = None,
    duration_minutes: float = 0.0,
) -> dict:
    """Execute the 6-step sleep sequence.

    Returns a dict with:
        - state: SleepState (persisted)
        - consolidation: ConsolidationArtifact (saved)
        - log_entry: dict (logged)
    """
    now = datetime.now(timezone.utc).isoformat()

    # 1. Freeze: stop accepting tasks (Director sets _accepting_tasks=False externally)
    # 2. Consolidate: compress Layer 2 episodic context into YAML summary
    layer_2_summary = _consolidate_episodic(episodic_history or [])

    # 3. Check-in: return checked-out books (clear ephemeral context)
    #    (External: Director tracks checked-out books; sleep signals release)

    # 4. Persist: save session state to disk
    scores_snapshot = {}
    for domain in store.domains():
        score = store.get_score(agent, domain)
        if score > 0:
            scores_snapshot[domain] = score

    state = SleepState(
        session_id=session_id,
        agent=agent,
        building=building,
        task_queue=task_queue or [],
        scores_snapshot=scores_snapshot,
        layer_0=layer_0 or {},
        layer_2_summary=layer_2_summary,
        timestamp=now,
    )
    save_session(state)

    # Also save a scores snapshot in data/sessions for fast lookup
    _save_scores_snapshot(session_id, scores_snapshot)

    # Create consolidation artifact
    tasks_completed = len([e for e in (episodic_history or []) if e.get("status") == "success"])
    domains_visited = list(set(e.get("domain", "_default") for e in (episodic_history or [])))
    key_decisions = [e.get("decision", "") for e in (episodic_history or []) if e.get("decision")]
    failed = [e.get("error", "") for e in (episodic_history or []) if e.get("error")]

    consolidation = ConsolidationArtifact(
        session_id=session_id,
        agent=agent,
        duration_minutes=duration_minutes,
        tasks_completed=tasks_completed,
        domains_visited=domains_visited,
        key_decisions=key_decisions,
        failed_approaches=failed,
        compressed_output_size_tokens=len(layer_2_summary.split()),
    )
    save_consolidation(consolidation)

    # 5. Clear: log KV cache clearing (actual clearing is model-backend-specific)
    # 6. Log: record sleep cycle to Library Log
    log_entry = {
        "session_id": session_id,
        "agent": agent,
        "event": "sleep",
        "details": f"Sleep after {duration_minutes:.1f}min, {tasks_completed} tasks",
        "timestamp": now,
    }
    append_library_log(log_entry)

    return {"state": state, "consolidation": consolidation, "log_entry": log_entry}


def _consolidate_episodic(episodic_history: list) -> str:
    """Compress episodic history into a summary string."""
    if not episodic_history:
        return "No activity recorded."
    lines = []
    for entry in episodic_history:
        domain = entry.get("domain", "unknown")
        status = entry.get("status", "?")
        if entry.get("decision"):
            lines.append(f"[{domain}] {entry['decision']}")
        elif entry.get("error"):
            lines.append(f"[{domain}] FAILED: {entry['error']}")
        else:
            lines.append(f"[{domain}] {status}")
    return "\n".join(lines)


def _save_scores_snapshot(session_id: str, scores: dict) -> None:
    """Save a fast-lookup scores snapshot alongside session JSON."""
    _ensure_sessions_dir()
    path = SESSIONS_DIR / f"{session_id}_scores.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scores, f, indent=2)
    # also update scores.json for persistence across restarts
    scores_file = Path(__file__).parent / "data" / "scores.json"
    if scores_file.exists():
        with open(scores_file, "r", encoding="utf-8") as f:
            all_scores = json.load(f)
        for domain, score in scores.items():
            if session_id in all_scores:
                all_scores[session_id][domain] = score
            else:
                all_scores[session_id] = {domain: score}
        with open(scores_file, "w", encoding="utf-8") as f:
            json.dump(all_scores, f, indent=4, sort_keys=True)


# ── Wake Sequence ────────────────────────────────────────────────────────────

def execute_wake(session_id: str) -> dict:
    """Execute the 3-step wake sequence.

    Returns a dict with:
        - state: SleepState (loaded)
        - consolidation: ConsolidationArtifact or None
        - log_entry: dict (logged)
    """
    now = datetime.now(timezone.utc).isoformat()

    # 1. Load: read persisted session state from disk
    state = load_session(session_id)

    # 2. Hydrate: load consolidation for context restoration
    consolidation = None
    try:
        consolidation = load_consolidation(session_id)
    except SessionNotFoundError:
        pass  # No consolidation artifact — ok for sessions that never slept

    # 3. Resume: log the wake event
    log_entry = {
        "session_id": session_id,
        "agent": state.agent,
        "event": "wake",
        "details": f"Wake for agent {state.agent}, {len(state.task_queue)} queued tasks",
        "timestamp": now,
    }
    append_library_log(log_entry)

    return {"state": state, "consolidation": consolidation, "log_entry": log_entry}
