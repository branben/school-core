"""
decision_log.py — Track critical agent decisions and correlate with performance.

This is the "key point" logger you described: when an agent makes a decision
that matters (which anchor to use, whether to consult the library, which
strategy to pick), we log it. Then we can see if that decision led to
improvement.

Decision types:
  anchor_chosen       — Which semantic anchors were selected for a prompt
  context_retrieved   — Agent consulted the library (CocoIndex / Engram)
  strategy_selected   — Baseline vs. semantic anchors
  difficulty_routed   — Which difficulty gate was used
  agent_selected      — Routing picked this agent over others
  retry_decision      — Agent failed, retry with different approach
  sleep_triggered     — Context pressure or timeout triggered sleep
  self_directed       — Agent chose its own task (autonomous mode)

Each decision is logged with:
  - The context (what was the situation?)
  - The choice (what did the agent decide?)
  - The outcome (did performance improve afterward?)

Usage:
    from decision_log import DecisionLog, DecisionType

    dlog = DecisionLog()
    dlog.log(DecisionType.ANCHOR_CHOSEN, agent="Grace",
             context={"domain": "python-testing", "difficulty": "hard"},
             choice={"anchors": ["Fagan Inspection", "Code Smells"]},
             expected="better structured review output")
    # ... after task completes ...
    dlog.outcome(decision_id, score=78.5, success=True)
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

DECISION_LOG_PATH = Path(__file__).parent / "data" / "decision_log.json"
MAX_ENTRIES = 1000


class DecisionType(str, Enum):
    ANCHOR_CHOSEN = "anchor_chosen"
    CONTEXT_RETRIEVED = "context_retrieved"
    STRATEGY_SELECTED = "strategy_selected"
    DIFFICULTY_ROUTED = "difficulty_routed"
    AGENT_SELECTED = "agent_selected"
    RETRY_DECISION = "retry_decision"
    SLEEP_TRIGGERED = "sleep_triggered"
    SELF_DIRECTED = "self_directed"
    STAFF_RECOMMENDATION = "staff_recommendation"
    GATE_CROSS = "gate_cross"


class DecisionLog:
    def __init__(self, path: Path = None):
        self.path = path or DECISION_LOG_PATH
        self._lock = threading.Lock()
        self._entries = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._entries = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES // 2:]
        self.path.write_text(json.dumps(self._entries, indent=2, ensure_ascii=False))

    def log(self, dtype: DecisionType, agent: str, context: dict,
            choice: dict, expected: str = "", task_id: str = "") -> str:
        """Log a decision. Returns decision ID for later outcome tracking."""
        dec_id = str(uuid.uuid4())[:8]
        entry = {
            "id": dec_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": dtype.value if isinstance(dtype, DecisionType) else dtype,
            "agent": agent,
            "context": context,
            "choice": choice,
            "expected": expected,
            "task_id": task_id,
            "outcome": None,  # filled in later
            "score_delta": None,
            "success": None,
        }
        with self._lock:
            self._entries.append(entry)
            self._save()
        return dec_id

    def outcome(self, decision_id: str, score: float = None,
                success: bool = None, score_delta: float = None,
                notes: str = "") -> Optional[dict]:
        """Record the outcome of a previous decision."""
        with self._lock:
            for e in reversed(self._entries):
                if e["id"] == decision_id:
                    e["outcome"] = {
                        "score": score,
                        "success": success,
                        "score_delta": score_delta,
                        "notes": notes,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    if score is not None:
                        e["score_delta"] = score_delta
                    if success is not None:
                        e["success"] = success
                    self._save()
                    return e
        return None

    def correlate(self, agent: str, decision_type: str = None,
                  window: int = 10) -> dict:
        """
        For a given agent (or all), compute correlation between decision
        type and subsequent performance improvement.
        """
        entries = [e for e in self._entries if e.get("outcome")]
        if agent:
            entries = [e for e in entries if e.get("agent") == agent]
        if decision_type:
            entries = [e for e in entries if e.get("type") == decision_type]

        if not entries:
            return {"count": 0, "message": "No decisions with outcomes yet"}

        improved = sum(1 for e in entries
                       if e["outcome"].get("score_delta", 0) > 0)
        worsened = sum(1 for e in entries
                       if e["outcome"].get("score_delta", 0) < 0)
        neutral = len(entries) - improved - worsened
        avg_delta = (sum(e["outcome"].get("score_delta", 0) for e in entries)
                     / len(entries))

        return {
            "count": len(entries),
            "improved": improved,
            "worsened": worsened,
            "neutral": neutral,
            "avg_score_delta": round(avg_delta, 2),
            "improvement_rate": round(improved / len(entries) * 100, 1),
        }

    def recent(self, n: int = 30, agent: str = None, dtype: str = None) -> list:
        entries = self._entries
        if agent:
            entries = [e for e in entries if e.get("agent") == agent]
        if dtype:
            entries = [e for e in entries if e.get("type") == dtype]
        return entries[-n:]

    def all_entries(self) -> list:
        return list(self._entries)

    def clear(self):
        with self._lock:
            self._entries = []
            self._save()


# ── Singleton ──
_default_log = None


def get_decision_log() -> DecisionLog:
    global _default_log
    if _default_log is None:
        _default_log = DecisionLog()
    return _default_log
