"""Shadow-only growth and skill evidence for routing experiments.

This module deliberately does not select the live route.  It produces a small,
redacted packet that can be evaluated offline before any routing policy changes.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
_MAX_HISTORY = 256
_MAX_CANDIDATES = 32
_MAX_TOOLS = 16
_MAX_LENSES = 8

_DIFFICULTY_WEIGHT = {
    "easy": 0.75,
    "medium": 1.0,
    "hard": 1.25,
    "diploma": 1.5,
    "blocker": 1.5,
}
_DIFFICULTIES = tuple(_DIFFICULTY_WEIGHT)
_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _score(record: dict) -> Optional[float]:
    for key in ("score", "combined_score", "task_score", "new_score"):
        value = record.get(key)
        if value is not None:
            return max(0.0, min(100.0, _number(value)))
    return None


def _difficulty(record: dict) -> str:
    value = str(record.get("difficulty") or "medium").strip().lower()
    return value if value in _DIFFICULTY_WEIGHT else "medium"


def _slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2.0
    y_mean = sum(values) / len(values)
    numerator = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    return numerator / denominator if denominator else 0.0


def _confidence_values(records: list[dict]) -> list[float]:
    values: list[float] = []
    for record in records:
        if record.get("confidence") is not None:
            values.append(max(0.0, min(1.0, _number(record.get("confidence")))))
            continue
        packet = record.get("review_packet")
        judges = packet.get("judges") if isinstance(packet, dict) else None
        confidences = [
            _number(judge.get("confidence"))
            for judge in (judges or {}).values()
            if isinstance(judge, dict) and judge.get("confidence") is not None
        ]
        if confidences:
            values.append(max(0.0, min(1.0, sum(confidences) / len(confidences))))
    return values


def _records(history: Iterable[Any], current: Optional[dict]) -> list[dict]:
    result = [item for item in history if isinstance(item, dict)][-_MAX_HISTORY:]
    if isinstance(current, dict):
        result.append(current)
    return result[-_MAX_HISTORY:]


def _tool_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    names = {
        str(item).strip()
        for item in value
        if isinstance(item, (str, int, float))
        and _TOOL_RE.fullmatch(str(item).strip())
    }
    return sorted(names)[:_MAX_TOOLS]


def _lens_evidence(records: list[dict]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        packet = record.get("review_packet")
        judges = packet.get("judges") if isinstance(packet, dict) else None
        if not isinstance(judges, dict):
            continue
        for lens, evidence in list(judges.items())[:_MAX_LENSES]:
            if isinstance(evidence, dict):
                buckets[str(lens)[:32]].append(evidence)

    result: dict[str, dict[str, Any]] = {}
    for lens in sorted(buckets)[:_MAX_LENSES]:
        entries = buckets[lens]
        scores = [max(0.0, min(100.0, _number(item.get("score")))) for item in entries]
        high_findings = 0
        accepted = 0
        for item in entries:
            if str(item.get("verdict") or "").upper() == "PASS":
                accepted += 1
            for finding in item.get("findings") or []:
                if isinstance(finding, dict) and str(finding.get("severity") or "").upper() in {"HIGH", "CRITICAL"}:
                    high_findings += 1
        result[lens] = {
            "samples": len(entries),
            "accepted": accepted,
            "acceptance_rate": round(accepted / len(entries), 4) if entries else 0.0,
            "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "critical_or_high_findings": high_findings,
        }
    return result


def _recommendation(records: list[dict], current_agent: str, candidates: Iterable[str]) -> dict:
    names = {str(name).strip() for name in candidates if str(name).strip()}
    if current_agent:
        names.add(current_agent)
    names = set(sorted(names)[:_MAX_CANDIDATES])
    by_agent: dict[str, list[float]] = defaultdict(list)
    for record in records:
        agent = str(record.get("agent") or "").strip()
        score = _score(record)
        if agent in names and score is not None:
            by_agent[agent].append(score)

    eligible = {
        agent: values
        for agent, values in by_agent.items()
        if len(values) >= 2
    }
    if not eligible:
        return {
            "current_agent": current_agent or None,
            "recommended_agent": current_agent or None,
            "changed": False,
            "reason": "insufficient_history",
            "candidates_with_history": 0,
        }

    def key(item: tuple[str, list[float]]) -> tuple[float, float, str]:
        agent, values = item
        # A small, bounded growth bonus is diagnostic only; it cannot alter the
        # live route because this packet is never passed to routing.route_task.
        return (sum(values) / len(values) + max(-10.0, min(10.0, _slope(values))), _slope(values), agent)

    recommended = max(eligible.items(), key=key)[0]
    return {
        "current_agent": current_agent or None,
        "recommended_agent": recommended,
        "changed": bool(current_agent and recommended != current_agent),
        "reason": "offline_shadow_score",
        "candidates_with_history": len(eligible),
    }


def build_shadow_evidence(
    history: Iterable[Any],
    *,
    current: Optional[dict] = None,
    candidates: Iterable[str] = (),
) -> dict:
    """Build a bounded shadow packet without changing live routing.

    ``tool_usage.used`` is accepted only with an explicit ``proven`` marker.
    Capability declarations populate ``offered`` only; they never imply use.
    """
    records = _records(history, current)
    scored = [(_score(record), record) for record in records]
    scored = [(score, record) for score, record in scored if score is not None]
    current_agent = str((current or {}).get("agent") or "").strip()

    adjusted = [score * _DIFFICULTY_WEIGHT[_difficulty(record)] for score, record in scored]
    confidence_values = _confidence_values(records)

    difficulty: dict[str, dict[str, Any]] = {}
    for level in _DIFFICULTIES:
        level_records = [record for record in records if _difficulty(record) == level]
        attempted = len(level_records)
        succeeded = sum(
            1 for record in level_records
            if str(record.get("status") or "").lower() == "success"
            and (_score(record) or 0.0) >= 50.0
        )
        difficulty[level] = {
            "attempted": attempted,
            "succeeded": succeeded,
            "success_rate": round(succeeded / attempted, 4) if attempted else None,
        }

    retried = sum(
        1 for record in records
        if _number(record.get("retry_count"), 0.0) > 0
        or str(record.get("status") or "").lower() == "retry"
    )
    tools_source = current or {}
    capability = tools_source.get("capability") if isinstance(tools_source, dict) else None
    capability = capability if isinstance(capability, dict) else {}
    usage = tools_source.get("tool_usage") if isinstance(tools_source, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    proven = usage.get("proven") is True
    tools = {
        "offered": _tool_names(capability.get("allowed_tools")),
        "used": _tool_names(usage.get("used")) if proven else [],
        "usage_evidence": "proven" if proven else "absent",
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "shadow",
        "live_routing_unchanged": True,
        "insufficient_data": len(scored) < 2,
        "samples": len(records),
        "growth": {
            "samples": len(adjusted),
            "slope": round(_slope(adjusted), 4),
            "difficulty_adjusted_mean": round(sum(adjusted) / len(adjusted), 4) if adjusted else 0.0,
        },
        "difficulty": difficulty,
        "retry": {
            "attempted": len(records),
            "retried": retried,
            "rate": round(retried / len(records), 4) if records else 0.0,
        },
        "confidence": {
            "samples": len(confidence_values),
            "mean": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else None,
            "slope": round(_slope(confidence_values), 4),
        },
        "skills": _lens_evidence(records),
        "tools": tools,
        "recommendation": _recommendation(records, current_agent, candidates),
    }


def load_shadow_history(path: str | Path, *, limit: int = _MAX_HISTORY) -> list[dict]:
    """Load only bounded JSON-list run history; malformed state means no history."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)][-max(1, min(int(limit), _MAX_HISTORY)):]


__all__ = ["build_shadow_evidence", "load_shadow_history"]
