"""Bounded post-bead compound-learning loop.

This module records observations and proposals without granting the loop
permission to mutate routing or skills. A proposal must pass independent
verification, and the same validated change must recur on comparable evidence
before it becomes promotion-eligible.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1
MAX_RECORDS = 256
_VALID_TRIGGERS = {"bead_completed", "bead_failed"}
_VALID_STOPS = {"blocked", "no_op", "stagnated", "awaiting_repetition", "completed"}
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{12,})\b"
)
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^\s/]+")


def _safe(value: Any, limit: int = 160) -> Optional[str]:
    if value is None:
        return None
    text = _HOME_PATH_RE.sub("~", _TOKEN_RE.sub("[REDACTED]", str(value).strip()))
    return text[:limit] or None


def _compact_evidence(value: Any) -> dict:
    """Keep only the bounded join/outcome fields needed for learning."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for section in ("control", "runtime", "verification", "judgment", "outcome"):
        data = value.get(section)
        if isinstance(data, dict):
            result[section] = {
                str(key): _safe(item) if not isinstance(item, bool) else item
                for key, item in list(data.items())[:24]
                if isinstance(item, (str, int, float, bool)) or item is None
            }
    # A caller may provide the already-normalized outcome separately.
    if isinstance(value.get("outcome"), dict) and "outcome" not in result:
        result["outcome"] = _compact_evidence({"outcome": value["outcome"]})["outcome"]
    return result


def _change(change: Any) -> Optional[dict]:
    if not isinstance(change, dict):
        return None
    change_id = _safe(change.get("change_id"), 80)
    kind = _safe(change.get("kind"), 40)
    target = _safe(change.get("target"), 120)
    if not change_id or not kind or not target:
        return None
    return {
        "change_id": change_id,
        "kind": kind,
        "target": target,
        "reason": _safe(change.get("reason"), 240),
    }


class CompoundLearningStore:
    """Persist observations and bounded improvement-loop state in JSON."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records = self._load()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [item for item in data if isinstance(item, dict)][-MAX_RECORDS:]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.records[-MAX_RECORDS:]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    def observe(self, *, bead_id: str, trigger: str, evidence: dict) -> dict:
        """Start a loop at the bead completion/failure boundary."""
        if trigger not in _VALID_TRIGGERS:
            raise ValueError(f"unsupported compound trigger: {trigger}")
        record = {
            "schema_version": SCHEMA_VERSION,
            "observation_id": f"obs-{len(self.records) + 1:06d}",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "bead_id": _safe(bead_id, 100),
            "trigger": trigger,
            "phase": "observed",
            "evidence": _compact_evidence(evidence),
            "candidate": None,
            "independent_verification": None,
            "promotion": {"eligible": False, "validated_repetitions": 0},
            "stop_reason": "awaiting_comparable_evidence",
        }
        self.records.append(record)
        self._save()
        return dict(record)

    def _find(self, observation_id: str) -> dict:
        for record in reversed(self.records):
            if record.get("observation_id") == observation_id:
                return record
        raise KeyError(f"unknown compound observation: {observation_id}")

    def propose(self, observation_id: str, change: dict) -> dict:
        """Choose exactly one bounded improvement for an observation."""
        record = self._find(observation_id)
        if record.get("candidate") is not None:
            raise ValueError("compound loop permits only one candidate change")
        candidate = _change(change)
        if candidate is None:
            record["phase"] = "stopped"
            record["stop_reason"] = "no_op"
        else:
            record["candidate"] = candidate
            record["phase"] = "proposed"
            record["stop_reason"] = None
        self._save()
        return dict(record)

    def verify(self, observation_id: str, *, accepted: bool, evidence: dict) -> dict:
        """Require independent acceptance evidence before recording a change."""
        record = self._find(observation_id)
        if record.get("candidate") is None:
            record["phase"] = "stopped"
            record["stop_reason"] = "no_op"
        elif not bool((evidence or {}).get("independent")):
            record["phase"] = "blocked"
            record["stop_reason"] = "blocked"
            record["independent_verification"] = {
                "accepted": False,
                "reason": "independent verification evidence required",
            }
        elif accepted:
            record["phase"] = "verified"
            record["stop_reason"] = None
            record["independent_verification"] = {
                "accepted": True,
                "evidence": _compact_evidence(evidence),
            }
        else:
            record["phase"] = "stopped"
            record["stop_reason"] = "stagnated"
            record["independent_verification"] = {
                "accepted": False,
                "evidence": _compact_evidence(evidence),
            }
        self._save()
        return dict(record)

    def record(self, observation_id: str) -> dict:
        """Record a verified change and assess repetition-based promotion."""
        record = self._find(observation_id)
        if record.get("phase") != "verified":
            record["phase"] = "blocked"
            record["stop_reason"] = "blocked"
            self._save()
            return dict(record)
        candidate = record.get("candidate") or {}
        change_id = candidate.get("change_id")
        repetitions = sum(
            1 for item in self.records
            if item.get("phase") == "recorded"
            and (item.get("candidate") or {}).get("change_id") == change_id
        ) + 1
        record["phase"] = "recorded"
        record["promotion"] = {
            "eligible": repetitions >= 2,
            "validated_repetitions": repetitions,
        }
        record["stop_reason"] = "completed" if repetitions >= 2 else "awaiting_repetition"
        self._save()
        return dict(record)

    def stop(self, observation_id: str, reason: str) -> dict:
        """Stop explicitly on a bounded no-progress or external condition."""
        if reason not in _VALID_STOPS:
            raise ValueError(f"unsupported compound stop: {reason}")
        record = self._find(observation_id)
        record["phase"] = "stopped" if reason != "blocked" else "blocked"
        record["stop_reason"] = reason
        self._save()
        return dict(record)

    def recent(self, limit: int = 20) -> list[dict]:
        return [dict(item) for item in self.records[-max(1, int(limit)):]]


__all__ = ["CompoundLearningStore", "MAX_RECORDS", "SCHEMA_VERSION"]
