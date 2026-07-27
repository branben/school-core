"""
activity_log.py — Real-time activity stream for Agent School.

Maintains a JSON activity log that captures what each agent is doing,
with plain-language descriptions suitable for a live dashboard.

Usage:
    from activity_log import ActivityLog, ActivityType

    log = ActivityLog()
    log.start_task(agent="foundry-coder-7b", domain="python-testing", difficulty="hard")
    # ... agent works ...
    log.finish_task(agent="foundry-coder-7b", domain="python-testing", score=70.0, success=True)
    log.staff_run(plugin="janitor", summary="Pruned 3 stale trajectories")
    log.idle(agent="foundry-coder-0.5b", reason="waiting for next task")
"""

import json
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

ACTIVITY_LOG_PATH = Path(__file__).parent / "data" / "activity_log.json"
MAX_ENTRIES = 500  # rotate after this many entries


class ActivityType(str, Enum):
    # Task lifecycle
    TASK_START = "task_start"
    TASK_FINISH = "task_finish"
    TASK_ERROR = "task_error"

    # Agent lifecycle
    AGENT_ENROLL = "agent_enroll"
    AGENT_SLEEP = "agent_sleep"
    AGENT_WAKE = "agent_wake"
    AGENT_IDLE = "agent_idle"

    # Staff / system
    STAFF_RUN = "staff_run"
    GATE_CROSS = "gate_cross"
    SCORE_AUDIT = "score_audit"

    # Autonomous
    SELF_DIRECTED = "self_directed"

    # Student (disposable leaf) lifecycle — observability for async dispatch
    STUDENT_STAGE = "student_stage"

# ── Semantic description templates ──

DOMAIN_VERBS = {
    "_default": "doing general coursework",
    "python-testing": "writing pytest tests",
    "python-coding": "writing Python code",
    "code-review": "reviewing code",
    "code-implementation": "implementing code",
    "debugging": "debugging code",
    "git-operations": "working with git",
    "triage-category": "classifying issues by category",
    "triage-state": "triaging issue states",
    "implementation": "implementing features",
    "agentic-coding": "doing agentic coding",
}

DIFFICULTY_ADJ = {
    "easy": "easy",
    "medium": "medium",
    "hard": "challenging",
    "blocker": "blocker-level",
}

ROLE_VERBS = {
    "Student": "studying",
    "Senior Student": "practicing",
    "Teacher": "mentoring and reviewing",
    "Faculty": "designing curriculum",
    "Unenrolled": "waiting to enroll",
}

STAFF_VERBS = {
    "janitor": "cleaning up stale data",
    "score-auditor": "auditing scores",
    "session_manager": "managing sessions",
}


def _task_start_description(agent: str, domain: str, difficulty: str, role: str = "") -> str:
    verb = DOMAIN_VERBS.get(domain, f"working on {domain}")
    adj = DIFFICULTY_ADJ.get(difficulty, difficulty)
    if role and role != "Unenrolled":
        role_verb = ROLE_VERBS.get(role, "working")
        return f"{agent} started {verb} ({adj}) — {role_verb}"
    return f"{agent} started {verb} ({adj})"


def _task_finish_description(agent: str, domain: str, score: float, success: bool, gate_crossed: str = None) -> str:
    verb = DOMAIN_VERBS.get(domain, f"working on {domain}")
    if success and gate_crossed:
        return f"{agent} aced {verb} (score {score:.0f}) — crossed into {gate_crossed}!"
    elif success:
        return f"{agent} finished {verb} (score {score:.0f})"
    else:
        return f"{agent} struggled with {verb} (score {score:.0f})"


def _task_error_description(agent: str, domain: str, error: str) -> str:
    verb = DOMAIN_VERBS.get(domain, f"working on {domain}")
    short_err = error[:80] if error else "unknown error"
    return f"{agent} hit an error while {verb}: {short_err}"


def _staff_description(plugin: str, summary: str) -> str:
    verb = STAFF_VERBS.get(plugin, f"running {plugin}")
    return f"Staff: {verb} — {summary}"


class ActivityLog:
    """Thread-safe activity log with JSON persistence."""

    def __init__(self, path: Path = None):
        self.path = path or ACTIVITY_LOG_PATH
        self._lock = threading.Lock()
        self._entries = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._entries = data.get("entries", [])
            except (json.JSONDecodeError, OSError):
                self._entries = []
        else:
            self._entries = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate if too many entries
        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES // 2:]
        self.path.write_text(json.dumps({
            "entries": self._entries,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, indent=2, ensure_ascii=False))

    def _add(self, entry: dict) -> dict:
        entry["id"] = len(self._entries) + 1
        if "timestamp" not in entry:
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._entries.append(entry)
            self._save()
        return entry

    def _agent_state(self, agent: str) -> dict:
        """Get the latest known state for an agent."""
        for e in reversed(self._entries):
            if e.get("agent") == agent:
                return e
        return {}

    # ── Public API ──

    def start_task(self, agent: str, domain: str, difficulty: str,
                   role: str = "", prompt_preview: str = "") -> dict:
        desc = _task_start_description(agent, domain, difficulty, role)
        return self._add({
            "type": ActivityType.TASK_START,
            "agent": agent,
            "domain": domain,
            "difficulty": difficulty,
            "role": role,
            "description": desc,
            "prompt_preview": prompt_preview[:120] if prompt_preview else "",
            "status": "in_progress",
        })

    def finish_task(self, agent: str, domain: str, score: float,
                    success: bool = True, gate_crossed: str = None) -> dict:
        desc = _task_finish_description(agent, domain, score, success, gate_crossed)
        return self._add({
            "type": ActivityType.TASK_FINISH,
            "agent": agent,
            "domain": domain,
            "score": score,
            "success": success,
            "gate_crossed": gate_crossed,
            "description": desc,
            "status": "completed",
        })

    def task_error(self, agent: str, domain: str, error: str) -> dict:
        desc = _task_error_description(agent, domain, error)
        return self._add({
            "type": ActivityType.TASK_ERROR,
            "agent": agent,
            "domain": domain,
            "error": error[:200],
            "description": desc,
            "status": "error",
        })

    def gate_cross(self, agent: str, domain: str, from_gate: str, to_gate: str, score: float) -> dict:
        return self._add({
            "type": ActivityType.GATE_CROSS,
            "agent": agent,
            "domain": domain,
            "from_gate": from_gate,
            "to_gate": to_gate,
            "score": score,
            "description": f"🎓 {agent} crossed from {from_gate} to {to_gate}! (score: {score:.1f})",
            "status": "milestone",
        })

    def staff_run(self, plugin: str, summary: str, metrics: dict = None) -> dict:
        desc = _staff_description(plugin, summary)
        return self._add({
            "type": ActivityType.STAFF_RUN,
            "agent": f"staff:{plugin}",
            "plugin": plugin,
            "description": desc,
            "metrics": metrics or {},
            "status": "completed",
        })

    def agent_sleep(self, agent: str, session_id: str = "") -> dict:
        return self._add({
            "type": ActivityType.AGENT_SLEEP,
            "agent": agent,
            "description": f"{agent} went to sleep (session: {session_id or 'unknown'})",
            "status": "sleeping",
        })

    def agent_wake(self, agent: str, session_id: str = "") -> dict:
        return self._add({
            "type": ActivityType.AGENT_WAKE,
            "agent": agent,
            "description": f"{agent} woke up (session: {session_id or 'unknown'})",
            "status": "active",
        })

    def agent_idle(self, agent: str, reason: str = "waiting for next task") -> dict:
        return self._add({
            "type": ActivityType.AGENT_IDLE,
            "agent": agent,
            "description": f"{agent} is idle — {reason}",
            "status": "idle",
        })

    def self_directed(self, agent: str, action: str, domain: str = "") -> dict:
        return self._add({
            "type": ActivityType.SELF_DIRECTED,
            "agent": agent,
            "domain": domain,
            "description": f"{agent} decided to {action}" + (f" ({domain})" if domain else ""),
            "status": "self_directed",
        })

    def student_stage(self, bead: str, role: str, stage: str,
                      detail: str = "", repo: str = "") -> dict:
        """Emit a plain-English stage of a disposable student's async dispatch.

        Lets a human watch a leaf's lifecycle on the live dashboard
        (activity_server.py) instead of a silent, headless run.

        Stages: clone | boot | hermes_thinking | bookbag_written |
        teachers_reviewing | done | error.
        """
        STAGE_VERBS = {
            "clone": "cloning target repo",
            "boot": "booting worktree",
            "hermes_thinking": "thinking (Hermes agent running)",
            "bookbag_written": "wrote output + bookbag",
            "teachers_reviewing": "COT + COO reviewing bookbag",
            "done": "dispatch complete",
            "error": "dispatch hit an error",
        }
        verb = STAGE_VERBS.get(stage, stage)
        label = f"student:{role}-{bead[:8]}"
        repo_bit = f" ({repo})" if repo else ""
        desc = f"{label} {verb}{repo_bit}"
        if detail:
            desc += f" — {detail}"
        return self._add({
            "type": ActivityType.STUDENT_STAGE,
            "agent": label,
            "bead": bead,
            "role": role,
            "stage": stage,
            "repo": repo,
            "description": desc,
            "status": "error" if stage == "error" else
                        ("completed" if stage == "done" else "in_progress"),
        })

    def enroll(self, agent: str) -> dict:
        return self._add({
            "type": ActivityType.AGENT_ENROLL,
            "agent": agent,
            "description": f"{agent} enrolled in Agent School",
            "status": "enrolled",
        })

    # ── Query API ──

    def recent(self, n: int = 50, agent: str = None) -> list:
        """Get recent activity entries, optionally filtered by agent."""
        entries = self._entries
        if agent:
            entries = [e for e in entries if e.get("agent") == agent]
        return entries[-n:]

    def current_activities(self) -> dict:
        """Get the latest activity per agent — what each agent is doing right now."""
        latest = {}
        for e in self._entries:
            a = e.get("agent", "")
            if a and not a.startswith("staff:"):
                latest[a] = e
        return latest

    def since(self, timestamp: str) -> list:
        """Get all entries since an ISO timestamp."""
        return [e for e in self._entries if e.get("timestamp", "") > timestamp]

    def all_entries(self) -> list:
        return list(self._entries)

    def clear(self):
        with self._lock:
            self._entries = []
            self._save()


# ── Singleton for import convenience ──

_default_log = None


def get_log() -> ActivityLog:
    global _default_log
    if _default_log is None:
        _default_log = ActivityLog()
    return _default_log
