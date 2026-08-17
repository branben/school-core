"""Normalize school-loop outcomes into bounded learning signals.

The bridge observes several independent outcomes: whether a worker completed its
lifecycle, whether the artifact was acceptable, and whether dispatch/verification
had operational problems.  This module keeps those signals separate so a runtime
fallback is not taught as a skill failure and a judge rejection is not mistaken
for a successful task merely because a process returned.
"""

from __future__ import annotations

import re
from typing import Any, Optional


FAILURE_EDGES = frozenset({
    "none",
    "model",
    "task_contract",
    "skill",
    "tool",
    "runtime",
    "verifier",
    "environment",
    "judge",
})

FAILURE_MODES = frozenset({
    "none",
    "syntax",
    "wrong_file",
    "missing_artifact",
    "missing_evidence",
    "identity_mismatch",
    "spawn_failure",
    "timeout",
    "auth",
    "incomplete",
    "quality",
    "disagreement",
    "unknown",
})

NEXT_ACTIONS = frozenset({
    "close",
    "retry_same",
    "change_capability",
    "repair_contract",
    "repair_runtime",
    "escalate",
    "human_decision",
    "no_op",
})

_MAX_REASON_CHARS = 48


def _bounded_label(value: Any) -> str:
    """Turn an operational label into a bounded, comparable token."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "_", text).strip("_")
    return text[:_MAX_REASON_CHARS]


def _number(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _reviewed(review: dict) -> bool:
    return bool(review.get("accepted") is not None or review.get("cto_verdict") or review.get("coo_verdict"))


def _first_number(*values: Any) -> Optional[float]:
    """Select the first present score without treating a valid zero as absent."""
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _has_mixed_judgment(review: dict) -> bool:
    cto = _bounded_label(review.get("cto_verdict"))
    coo = _bounded_label(review.get("coo_verdict"))
    return bool(cto and coo and cto != coo)


def _text_blob(*values: Any) -> str:
    """Build a bounded diagnostic search string, never persist it."""
    return " ".join(str(value or "") for value in values).lower()[:4000]


def _fallback_class(reason: str) -> tuple[str, str, str]:
    """Map known dispatch failures to edge, mode, and permitted repair."""
    reason = _bounded_label(reason)
    if reason in {"timeout", "blocked", "crew_failed", "supervisor_unexpected", "crew_unexpected"}:
        return "runtime", "timeout" if reason == "timeout" else "unknown", "repair_runtime"
    if reason == "spawn_failure":
        return "runtime", "spawn_failure", "repair_runtime"
    if reason in {"artifact_identity_missing", "artifact_identity_mismatch"}:
        return "runtime", "identity_mismatch", "repair_runtime"
    if reason in {"artifact_evidence_missing", "report_missing", "report_empty", "report_unreadable", "report_too_large"}:
        return "task_contract", "missing_evidence" if reason == "artifact_evidence_missing" else "missing_artifact", "repair_contract"
    if reason in {"crew_in_flight", "insufficient_cycle_time", "retry_pressure", "crew_cap_reached"}:
        return "runtime", "unknown", "retry_same"
    if reason in {"capability_resolution_failure"}:
        return "environment", "unknown", "repair_runtime"
    return "", "", ""


def normalize_outcome(
    *,
    status: str,
    task_result: Optional[dict] = None,
    error: Any = None,
    verification: Optional[dict] = None,
    review: Optional[dict] = None,
    entire: Optional[dict] = None,
    fallback_reason: Any = None,
    retry_attempt: int = 0,
) -> dict:
    """Return the canonical bounded outcome fields for one observed run.

    ``lifecycle`` describes process state; ``quality`` describes the best
    available task score.  A completed lifecycle can therefore have low
    quality, and a successful direct fallback can still retain a runtime
    failure edge from the preferred crew route.
    """
    task_result = task_result if isinstance(task_result, dict) else {}
    verification = verification if isinstance(verification, dict) else {}
    review = review if isinstance(review, dict) else (task_result.get("review") or {})
    entire = entire if isinstance(entire, dict) else {}
    raw_status = _bounded_label(status or task_result.get("status")) or "unknown"
    reason = _bounded_label(fallback_reason)
    retry_attempt = max(0, int(retry_attempt or 0))

    if raw_status in {"success", "done", "completed"}:
        lifecycle = "completed"
    elif raw_status in {"retry", "pending"}:
        lifecycle = "retry"
    elif raw_status in {"blocked", "crew_in_flight", "skipped"}:
        lifecycle = "blocked" if raw_status != "skipped" else "skipped"
    elif raw_status in {"running", "in_flight"}:
        lifecycle = "in_flight"
    else:
        lifecycle = "failed"

    score = _first_number(
        review.get("combined_score"),
        task_result.get("task_score"),
        verification.get("score"),
    )
    quality = round((score or 0.0) / 100.0, 4)

    edge = "none"
    mode = "none"
    next_action = "close" if lifecycle == "completed" else "retry_same"

    # Preserve a preferred-route failure even when direct fallback completes.
    fallback_edge, fallback_mode, fallback_action = _fallback_class(reason)
    if fallback_edge:
        edge, mode, next_action = fallback_edge, fallback_mode, fallback_action

    blob = _text_blob(
        error,
        verification.get("failures"),
        (review.get("findings") or []),
        task_result.get("teacher_evidence"),
    )
    if re.search(r"syntaxerror|invalid syntax|runtime_failure|traceback", blob):
        edge, mode, next_action = "model", "syntax", "change_capability"
    elif re.search(r"wrong[_ -]?file|path[_ -]?incorrect|file[_ -]?path", blob):
        edge, mode, next_action = "task_contract", "wrong_file", "repair_contract"
    elif re.search(r"missing[_ -]?(?:evidence|artifact)|report[_ -]?(?:missing|empty|unreadable)", blob):
        edge, mode, next_action = "task_contract", "missing_evidence", "repair_contract"

    if _has_mixed_judgment(review):
        edge, mode, next_action = "judge", "disagreement", "escalate"
    elif _reviewed(review) and review.get("accepted") is False and edge == "none":
        edge, mode, next_action = "judge", "quality", "change_capability"

    if verification.get("skipped") and edge == "none":
        edge, mode, next_action = "verifier", "missing_evidence", "repair_runtime"
    elif verification and verification.get("passed") is False and edge == "none":
        edge, mode, next_action = "verifier", "unknown", "repair_contract"

    if lifecycle == "completed" and _reviewed(review) and review.get("accepted") is True:
        # Keep a preferred-route fallback visible, but a clean accepted run is
        # an ordinary close. Entire is observational and cannot make a failure.
        if not fallback_edge and edge == "none":
            next_action = "close"
    elif lifecycle == "retry":
        next_action = "retry_same"
    elif lifecycle == "blocked":
        next_action = "human_decision" if reason in {"crew_in_flight", "blocked"} else next_action

    # Guard the public contract even if a future mapping is edited incorrectly.
    if edge not in FAILURE_EDGES:
        edge = "unknown" if "unknown" in FAILURE_EDGES else "runtime"
    if mode not in FAILURE_MODES:
        mode = "unknown"
    if next_action not in NEXT_ACTIONS:
        next_action = "no_op"

    return {
        "lifecycle": lifecycle,
        "quality": quality,
        "failure_edge": edge,
        "failure_mode": mode,
        "fallback_reason": reason or None,
        "next_action": next_action,
        "retry_attempt": retry_attempt,
    }


__all__ = [
    "FAILURE_EDGES",
    "FAILURE_MODES",
    "NEXT_ACTIONS",
    "normalize_outcome",
]
