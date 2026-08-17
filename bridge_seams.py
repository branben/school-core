"""Pure, schema-preserving seams used by the issue bridge.

These helpers have no GitHub, model, filesystem, or process side effects. Keeping
packet construction and policy-result shapes here makes the large bridge facade
easier to test and lets future orchestration changes preserve compatibility.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from shadow_routing import build_shadow_evidence
from failure_taxonomy import normalize_outcome
from evidence_join import build_evidence_join


def observability_fields(task_result: dict) -> dict:
    """Return bounded persona/review evidence for durable run records."""
    fields = {}
    for key in ("capability", "teacher_evidence"):
        if key in task_result:
            fields[key] = task_result.get(key)
    return fields


def build_shadow_routing_packet(
    task_result: dict,
    issue: dict,
    score: float,
    retry_count: int,
    history: Optional[list[dict]],
    candidates: Iterable[str],
) -> dict:
    """Build observational routing evidence without changing route selection."""
    current = {
        "agent": task_result.get("agent"),
        "score": score,
        "status": task_result.get("status"),
        "difficulty": issue.get("difficulty"),
        "retry_count": retry_count,
        "confidence": task_result.get("confidence")
        if task_result.get("confidence") is not None
        else (task_result.get("review") or {}).get("confidence"),
        "review_packet": task_result.get("review_packet"),
        "capability": task_result.get("capability"),
        # Runtime tool usage is intentionally passed through only when a
        # producer proves it. Capability declarations are merely "offered".
        "tool_usage": task_result.get("tool_usage"),
    }
    # The normal loader already returns a bounded list, but this helper also
    # accepts test/legacy callers. Reassert the packet invariant at the seam so
    # malformed or oversized preloaded state cannot expand the shadow payload.
    if not isinstance(history, list):
        history = []
    history = [item for item in history if isinstance(item, dict)][-256:]
    return build_shadow_evidence(
        history,
        current=current,
        candidates=candidates,
    )


def outcome_fields(
    *,
    status: str,
    task_result: Optional[dict] = None,
    error: Any = None,
    verification: Optional[dict] = None,
    review: Optional[dict] = None,
    entire: Optional[dict] = None,
    fallback_reason: Optional[str] = None,
    retry_attempt: int = 0,
) -> dict:
    """Expose the canonical lifecycle/quality taxonomy at the bridge seam."""
    return normalize_outcome(
        status=status,
        task_result=task_result,
        error=error,
        verification=verification,
        review=review,
        entire=entire,
        fallback_reason=fallback_reason,
        retry_attempt=retry_attempt,
    )


def strict_gate_failure(reason: str) -> dict:
    """Build the VERIFY_GATE_STRICT escalation verdict."""
    return {
        "passed": False,
        "skipped": False,
        "strict_escalated": True,
        "ran": 0,
        "failures": [{
            "cmd": "(verify_gate)",
            "exit": None,
            "stderr": (
                f"{reason}\n[VERIFY_GATE_STRICT] Escalation: the verify gate "
                "could not run, so this issue cannot pass "
                "(compiler-before-critic is enforced)."
            ),
        }],
    }


__all__ = [
    "build_evidence_join",
    "build_shadow_routing_packet",
    "observability_fields",
    "outcome_fields",
    "strict_gate_failure",
]
