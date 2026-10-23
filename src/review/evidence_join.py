"""Build the additive cross-layer evidence join.

The join is an index, not a task database. ``bd`` remains authoritative for
lifecycle; this packet only makes it possible to trace a bead through the
runtime and review evidence that already exists in their owning systems.
"""

from __future__ import annotations

import re
from typing import Any, Optional


SCHEMA_VERSION = 1
_MAX_TEXT = 180
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^\s/]+")
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{12,})\b"
)


def _safe(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = _HOME_PATH_RE.sub("~", text)
    text = _TOKEN_RE.sub("[REDACTED]", text)
    return text[:_MAX_TEXT]


def _section(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: _safe(values.get(key)) for key in keys}


def _files(values: Any) -> list[str]:
    """Return a bounded, redacted list of changed file references."""
    if not isinstance(values, (list, tuple)):
        return []
    files: list[str] = []
    for value in values[:64]:
        path = _safe(value)
        if path and path not in files:
            files.append(path)
    return files


def _finding_classes(findings: Any) -> list[str]:
    """Reduce Entire findings to bounded comparable categories."""
    if not isinstance(findings, list):
        return []
    classes: list[str] = []
    for finding in findings[:16]:
        if not isinstance(finding, dict):
            continue
        text = str(finding.get("message") or finding.get("raw") or "").lower()
        if "test" in text:
            category = "missing_tests"
        elif "intent" in text or "mismatch" in text:
            category = "intent_mismatch"
        elif "risk" in text or "unsafe" in text or "security" in text:
            category = "risky_diff"
        else:
            category = str(finding.get("severity") or "unknown").lower()
        if category not in classes:
            classes.append(category[:32])
    return classes[:8]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _quality(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def build_evidence_join(
    *,
    control: Optional[dict] = None,
    runtime: Optional[dict] = None,
    artifact: Optional[dict] = None,
    verification: Optional[dict] = None,
    judgment: Optional[dict] = None,
    outcome: Optional[dict] = None,
) -> dict:
    """Return a stable, redacted packet of cross-layer references.

    Every section has a fixed key set and explicit ``None`` for unavailable
    evidence. No prompts, responses, logs, or absolute paths are copied.
    """
    control = control if isinstance(control, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    artifact = artifact if isinstance(artifact, dict) else {}
    verification = verification if isinstance(verification, dict) else {}
    judgment = judgment if isinstance(judgment, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}

    artifact_section = _section(artifact, (
        "repository", "base_ref", "branch", "commit", "report_ref",
        "trajectory_ref", "bookbag_ref",
    ))
    artifact_section["changed_files"] = _files(artifact.get("changed_files"))

    return {
        "schema_version": SCHEMA_VERSION,
        "control": _section(control, (
            "route_id", "bd_id", "plan_id", "plan_unit", "wayfinder_id",
            "knowledge_anchor", "primary_workflow", "chosen_skill",
        )),
        "runtime": _section(runtime, (
            "dispatcher", "cycle_session_id", "firstmate_crew_id",
            "orca_worktree_id", "orca_terminal_id", "hermes_session_id",
        )),
        "artifact": artifact_section,
        "verification": {
            "project_gate": _safe(verification.get("project_gate")),
            "project_gate_reason": _safe(verification.get("project_gate_reason")),
            "entire_status": _safe(verification.get("entire_status")),
            "entire_finding_count": max(0, int(verification.get("entire_finding_count", 0) or 0)),
            "finding_classes": _finding_classes(verification.get("entire_findings")),
        },
        "judgment": {
            "cto_verdict": _safe(judgment.get("cto_verdict")),
            "coo_verdict": _safe(judgment.get("coo_verdict")),
            "accepted": judgment.get("accepted") if isinstance(judgment.get("accepted"), bool) else None,
            "score": _number(judgment.get("score")),
            "critical_findings": max(0, int(judgment.get("critical_findings", 0) or 0)),
        },
        "outcome": {
            "lifecycle": _safe(outcome.get("lifecycle")),
            "quality": _quality(outcome.get("quality")),
            "failure_edge": _safe(outcome.get("failure_edge")),
            "failure_mode": _safe(outcome.get("failure_mode")),
            "fallback_reason": _safe(outcome.get("fallback_reason")),
            "next_action": _safe(outcome.get("next_action")),
        },
    }


__all__ = ["SCHEMA_VERSION", "build_evidence_join"]
