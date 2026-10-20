"""Bounded teacher evidence for routing and cross-cycle learning.

The two-judge review is the source of truth for acceptance. This module keeps a
small, machine-readable summary on the existing trajectory so the next cycle can
see why a route succeeded or failed without storing prompts or full review prose.
The normalized signal is deliberately the same signal accepted by the existing
RouterExperience API: ``success`` plus quality in ``[0, 1]``.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_MAX_FINDINGS = 8
_MAX_DESCRIPTION = 240
_MAX_CITATION = 160
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+")
_TOKEN_RE = re.compile(
    r"(?i)(?:\b(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{12,})\b|"
    r"(?:OMNIROUTE_API_KEY|AGENTMAIL_API_KEY|GH_TOKEN)=\S+)"
)


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    text = _HOME_PATH_RE.sub("~", text)
    text = _TOKEN_RE.sub("[REDACTED]", text)
    return text[:limit]


def _quality(review: dict) -> float:
    raw = review.get("combined_score")
    if raw is None:
        scores = [review.get("cto_score"), review.get("coo_score")]
        numeric = [float(score) for score in scores if isinstance(score, (int, float))]
        raw = sum(numeric) / len(numeric) if numeric else 0.0
    try:
        return round(max(0.0, min(100.0, float(raw))) / 100.0, 4)
    except (TypeError, ValueError):
        return 0.0


def build_teacher_evidence(
    agent: str,
    domain: str,
    difficulty: str,
    review: dict,
) -> dict:
    """Build a bounded, JSON-safe summary of the teacher review."""
    findings = review.get("findings") or []
    compact_findings = []
    severities = Counter()
    for finding in findings[:_MAX_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        severity = _bounded_text(finding.get("severity"), 24).upper() or "UNKNOWN"
        severities[severity] += 1
        compact_findings.append(
            {
                "severity": severity,
                "issue_class": _bounded_text(finding.get("issue_class"), 80),
                "citation": _bounded_text(finding.get("citation"), _MAX_CITATION),
                "description": _bounded_text(finding.get("description"), _MAX_DESCRIPTION),
            }
        )

    quality = _quality(review)
    accepted = bool(review.get("accepted"))
    return {
        "schema_version": 1,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "agent": _bounded_text(agent, 80),
        "domain": _bounded_text(domain, 80),
        "difficulty": _bounded_text(difficulty, 24),
        "accepted": accepted,
        "cto_verdict": _bounded_text(review.get("cto_verdict"), 16),
        "coo_verdict": _bounded_text(review.get("coo_verdict"), 16),
        "cto_score": review.get("cto_score", 0),
        "coo_score": review.get("coo_score", 0),
        "combined_score": round(quality * 100.0, 2),
        "finding_counts": dict(sorted(severities.items())),
        "findings": compact_findings,
        "routing_feedback": {"success": accepted, "quality": quality},
        # RouterExperience currently selects by task role. Keep the richer
        # context visible so later context-aware routing can use it, and so
        # operators do not mistake role-scoped learning for domain isolation.
        "routing_context": {
            "agent": _bounded_text(agent, 80),
            "domain": _bounded_text(domain, 80),
            "difficulty": _bounded_text(difficulty, 24),
            "selection_scope": "agent",
        },
    }


def routing_signal(evidence: dict) -> tuple[bool, float]:
    """Return the normalized signal consumed by RouterExperience."""
    feedback = evidence.get("routing_feedback") or {}
    return bool(feedback.get("success")), float(feedback.get("quality", 0.0))


def persist_teacher_evidence(
    trajectory_path: Optional[str | Path],
    evidence: dict,
) -> bool:
    """Attach evidence to an existing trajectory using an atomic replacement.

    A missing or malformed trajectory is non-fatal: routing still receives the
    signal, while the caller can report that durable evidence was unavailable.
    """
    if not trajectory_path:
        evidence["persisted_to_trajectory"] = False
        return False
    path = Path(trajectory_path)
    try:
        trajectory = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(trajectory, dict):
            evidence["persisted_to_trajectory"] = False
            return False
        evidence["persisted_to_trajectory"] = True
        trajectory["teacher_evidence"] = evidence
        temporary = path.with_name(f".{path.name}.teacher-feedback.tmp")
        temporary.write_text(
            json.dumps(trajectory, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        evidence["persisted_to_trajectory"] = False
        try:
            temporary.unlink()
        except (NameError, OSError):
            pass
        return False


__all__ = [
    "build_teacher_evidence",
    "persist_teacher_evidence",
    "routing_signal",
]
