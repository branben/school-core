import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


LOG_PATH = Path(__file__).parent / "data" / "escalation_log.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_log() -> list:
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text())
    except Exception:
        return []


def _save_log(entries: list) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(entries, indent=2))


class EscalationLog:
    def __init__(self, log_path: str = None):
        global LOG_PATH
        if log_path:
            LOG_PATH = Path(log_path)
        self._entries = _load_log()

    def log(self, agent: str, domain: str, difficulty: str, confidence: float, threshold: float, escalated_to: str) -> None:
        try:
            entry = {
                "timestamp": _now_iso(),
                "agent": agent,
                "domain": domain,
                "difficulty": difficulty,
                "confidence": confidence,
                "threshold": threshold,
                "escalated_to": escalated_to,
            }
            self._entries.append(entry)
            _save_log(self._entries)
        except Exception as e:
            sys.stderr.write(f"[escalation_log] Failed to log event: {e}\n")

    def get_rate(self, agent: str, days: int = 7) -> float:
        try:
            entries = self._entries
            if not entries:
                return 0.0
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            recent = [e for e in entries if e.get("agent") == agent and datetime.fromisoformat(e["timestamp"]) >= cutoff]
            if not recent:
                return 0.0
            return len(recent) / max(len([e for e in entries if datetime.fromisoformat(e["timestamp"]) >= cutoff]), 1)
        except Exception as e:
            sys.stderr.write(f"[escalation_log] Failed to get rate: {e}\n")
            return 0.0

    def get_all_rates(self, days: int = 7) -> dict:
        try:
            entries = self._entries
            if not entries:
                return {}
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            recent = [e for e in entries if datetime.fromisoformat(e["timestamp"]) >= cutoff]
            agents = set(e["agent"] for e in recent)
            total_recent = max(len(recent), 1)
            return {agent: len([e for e in recent if e["agent"] == agent]) / total_recent for agent in agents}
        except Exception as e:
            sys.stderr.write(f"[escalation_log] Failed to get all rates: {e}\n")
            return {}

    def all_entries(self) -> list:
        return list(self._entries)
