#!/usr/bin/env python3
"""ce_router.py — Compound Engineering skill-dispatch router (Rank 4).

Deterministic router that maps a task's *shape* to one of the Layer B skill
integrations (ranks 1-6). The Principal calls this BEFORE dispatch so the
right integration runs for the right task.

Task shape → skill mapping (from the Layer B plan):

    failed gate                  → Rank 1  (systematic-debugging + TDD)
    new implementation           → Rank 2  (CE workflow)
    architectural routing dec.   → Rank 3  (DDD)
    complex decomposition (>3)   → Rank 5  (plan for students)
    spec-gap closure             → Rank 6  (harness-ready)

The router is DETERMINISTIC: the same task shape always yields the same skill
choice. When multiple flags are set, a fixed precedence resolves the conflict
(see PRECEDENCE) so the output never varies run-to-run.

The router has NO external dependencies and makes NO network/LLM calls — it is
purely a function of the task-shape dict, so it is trivially testable offline.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Skill keys returned by the router. These are the canonical rank identifiers.
SKILL_RANK_1 = "rank1_systematic_debugging"   # teacher diagnose loop
SKILL_RANK_2 = "rank2_ce_workflow"             # student CE loop
SKILL_RANK_3 = "rank3_ddd_routing"             # principal doubt cycle
SKILL_RANK_5 = "rank5_student_plan"            # plan decomposition
SKILL_RANK_6 = "rank6_harness_ready"           # spec-JSON DOD gate

# Runtime policy overlays. These are orthogonal to the primary Rank skill.
# Their order is the canonical bounded precedence from using-agent-skills.
OVERLAY_BOUNDED_REVIEW = "bounded-adversarial-plan-review"
OVERLAY_DOUBT = "doubt-driven-development"
OVERLAY_SECURITY = "security-and-hardening"
OVERLAY_SOURCE = "source-driven-development"
OVERLAY_OBSERVABILITY = "observability-and-instrumentation"
OVERLAY_BROWSER = "browser-testing-with-devtools"
OVERLAY_PERFORMANCE = "performance-optimization"
OVERLAY_TDD = "test-driven-development"

OVERLAY_PRECEDENCE = [
    OVERLAY_BOUNDED_REVIEW,
    OVERLAY_DOUBT,
    OVERLAY_SECURITY,
    OVERLAY_SOURCE,
    OVERLAY_OBSERVABILITY,
    OVERLAY_BROWSER,
    OVERLAY_PERFORMANCE,
    OVERLAY_TDD,
]

# Fixed precedence when several flags are true. Earlier = higher priority.
# Order rationale: a failed gate (needs a regression test) and a spec-gap
# (needs a DOD gate) are the most concrete, actionable shapes; architectural
# routing and new-implementation are broader; complex decomposition is a
# refinement that can layer on top of the chosen primary skill.
PRECEDENCE: List[str] = [
    SKILL_RANK_1,  # failed gate
    SKILL_RANK_6,  # spec-gap closure
    SKILL_RANK_3,  # architectural routing decision
    SKILL_RANK_2,  # new implementation
    SKILL_RANK_5,  # complex decomposition
]

# Human-readable labels for logging / bookbag.
SKILL_LABELS = {
    SKILL_RANK_1: "systematic-debugging+TDD (teacher diagnose)",
    SKILL_RANK_2: "Compound Engineering workflow (student)",
    SKILL_RANK_3: "Doubt-Driven Development (principal routing)",
    SKILL_RANK_5: "Plan decomposition (student sub-tasks)",
    SKILL_RANK_6: "Harness-ready DOD gate (spec-gap closure)",
}

# Threshold above which a task is "complex" (needs Rank 5 plan decomposition).
COMPLEXITY_THRESHOLD = 3


def classify_task(
    *,
    has_failed_gate: bool = False,
    is_new_implementation: bool = False,
    requires_architectural_routing: bool = False,
    complexity: int = 1,
    is_spec_gap: bool = False,
    # Runtime policy signals. These remain separate from primary rank skills.
    is_curiosity_driven: bool = False,
    requires_human_gate: bool = False,
    is_plan_review: bool = False,
    requires_doubt: bool = False,
    requires_security_review: bool = False,
    requires_source_review: bool = False,
    requires_observability: bool = False,
    requires_browser_verification: bool = False,
    requires_performance_review: bool = False,
    requires_tdd: bool = False,
) -> Dict[str, Any]:
    """Build the task-shape dict from explicit flags.

    Callers pass the booleans/int they already know about the task. This is a
    thin constructor so the router's input shape is uniform and testable.

    Args:
        has_failed_gate: A prior gate/verdict failed for this task.
        is_new_implementation: Net-new feature with no prior solution.
        requires_architectural_routing: Needs a routing/lens/model decision.
        complexity: Implied number of sub-steps (>=1).
        is_spec_gap: Task closes an open spec/DOD gap.
        is_curiosity_driven: Ambiguity may change the plan; surface curiosity questions.
        requires_human_gate: The decision needs explicit human authorization.
        is_plan_review: An existing plan or architecture artifact is under review.
        requires_doubt: A non-trivial decision needs adversarial review.
        requires_security_review: Security-hardening overlay is independently triggered.
        requires_source_review: Official-documentation verification is independently triggered.
        requires_observability: Observability/instrumentation overlay is independently triggered.
        requires_browser_verification: Browser runtime verification is independently triggered.
        requires_performance_review: Performance analysis is independently triggered.
        requires_tdd: Test-driven-development overlay is independently triggered.

    Returns:
        A task-shape dict consumed by choose_skill() and _policy_route().
    """
    return {
        "has_failed_gate": bool(has_failed_gate),
        "is_new_implementation": bool(is_new_implementation),
        "requires_architectural_routing": bool(requires_architectural_routing),
        "complexity": int(complexity),
        "is_spec_gap": bool(is_spec_gap),
        "is_curiosity_driven": bool(is_curiosity_driven),
        "requires_human_gate": bool(requires_human_gate),
        "is_plan_review": bool(is_plan_review),
        "requires_doubt": bool(requires_doubt),
        "requires_security_review": bool(requires_security_review),
        "requires_source_review": bool(requires_source_review),
        "requires_observability": bool(requires_observability),
        "requires_browser_verification": bool(requires_browser_verification),
        "requires_performance_review": bool(requires_performance_review),
        "requires_tdd": bool(requires_tdd),
    }


def choose_skill(task_shape: Dict[str, Any]) -> str:
    """Deterministically map a task shape to a skill (rank) key.

    Pure function: identical input → identical output. No side effects, no I/O.

    Args:
        task_shape: dict with keys produced by classify_task(). May also be
            passed positionally as a plain dict.

    Returns:
        One of the SKILL_RANK_* keys.
    """
    if not isinstance(task_shape, dict):
        raise TypeError(f"task_shape must be a dict, got {type(task_shape).__name__}")

    has_failed_gate = bool(task_shape.get("has_failed_gate", False))
    is_new_implementation = bool(task_shape.get("is_new_implementation", False))
    requires_architectural_routing = bool(
        task_shape.get("requires_architectural_routing", False)
    )
    complexity = int(task_shape.get("complexity", 1) or 1)
    is_spec_gap = bool(task_shape.get("is_spec_gap", False))

    # Candidate skills in precedence order; first match wins.
    candidates = []
    if has_failed_gate:
        candidates.append(SKILL_RANK_1)
    if is_spec_gap:
        candidates.append(SKILL_RANK_6)
    if requires_architectural_routing:
        candidates.append(SKILL_RANK_3)
    if is_new_implementation:
        candidates.append(SKILL_RANK_2)
    if complexity > COMPLEXITY_THRESHOLD:
        candidates.append(SKILL_RANK_5)

    if not candidates:
        # Default: a simple, single-step new task → CE workflow (Rank 2).
        return SKILL_RANK_2

    # Apply fixed precedence to resolve multi-flag conflicts deterministically.
    for skill in PRECEDENCE:
        if skill in candidates:
            return skill

    # Fallback (should be unreachable given the default above).
    return candidates[0]


def _policy_route(task_shape: Dict[str, Any]) -> Dict[str, Any]:
    """Build the bounded runtime route contract from a task shape.

    The existing ``choose_skill`` result remains the primary workflow for
    compatibility. Overlays are independently triggered, ranked, capped at
    two, and returned with discarded candidates for observability.
    """
    chosen = choose_skill(task_shape)
    is_plan_review = bool(task_shape.get("is_plan_review", False))

    requested = []
    if is_plan_review:
        requested.append(OVERLAY_BOUNDED_REVIEW)
    if task_shape.get("requires_doubt", False):
        requested.append(OVERLAY_DOUBT)
    if task_shape.get("requires_security_review", False):
        requested.append(OVERLAY_SECURITY)
    if task_shape.get("requires_source_review", False):
        requested.append(OVERLAY_SOURCE)
    if task_shape.get("requires_observability", False):
        requested.append(OVERLAY_OBSERVABILITY)
    if task_shape.get("requires_browser_verification", False):
        requested.append(OVERLAY_BROWSER)
    if task_shape.get("requires_performance_review", False):
        requested.append(OVERLAY_PERFORMANCE)
    if task_shape.get("requires_tdd", False):
        requested.append(OVERLAY_TDD)

    # A bounded plan review replaces doubt; it does not consume a second slot.
    if OVERLAY_BOUNDED_REVIEW in requested:
        requested = [o for o in requested if o != OVERLAY_DOUBT]

    ranked = [overlay for overlay in OVERLAY_PRECEDENCE if overlay in requested]
    overlays = ranked[:2]
    discarded = ranked[2:]
    return {
        "primary_workflow": chosen,
        "overlays": overlays,
        "discarded_overlays": discarded,
        "curiosity_required": bool(task_shape.get("is_curiosity_driven", False)),
        "human_gate_required": bool(task_shape.get("requires_human_gate", False)),
    }


def route_id_for(bead: Optional[str]) -> Optional[str]:
    """Return the stable route identity for a persisted bookbag bead."""
    return f"route-{bead}" if bead else None


def route_decision(
    task_shape: Dict[str, Any],
    bead: Optional[str] = None,
    repo: str = "__global__",
    bookbag_writer: Optional[Callable[..., Any]] = None,
    evidence_join: Optional[dict] = None,
) -> Dict[str, Any]:
    """Choose a skill and (optionally) log it to the bookbag for traceability.

    Args:
        task_shape: dict from classify_task().
        bead: bookbag id to log the choice against (optional).
        repo: repo namespace for the bookbag (default global).
        bookbag_writer: callable(bead, repo, **fields) -> optional; defaults to
            bookbag.locked_update_bookbag. A non-None return means the fields
            were written; None means the durable write failed. Passed in so
            tests can stub it (no real bookbag I/O required).

    Returns:
        {
            "chosen_skill": str,          # backward-compatible primary alias
            "primary_workflow": str,      # selected primary rank workflow
            "overlays": list[str],        # at most two ranked overlays
            "discarded_overlays": list[str],
            "curiosity_required": bool,
            "human_gate_required": bool,
            "label": str,
            "task_shape": dict,
            "logged": bool,               # True if a bookbag write happened
            "evidence_join": dict | None,  # additive cross-layer references
        }
    """
    policy = _policy_route(task_shape)
    chosen = policy["primary_workflow"]
    route_id = route_id_for(bead)
    result = {
        "route_id": route_id,
        "chosen_skill": chosen,
        "label": SKILL_LABELS.get(chosen, chosen),
        "task_shape": task_shape,
        **policy,
        "logged": False,
        "evidence_join": evidence_join,
    }

    if bead and bookbag_writer is not None:
        try:
            written = bookbag_writer(
                bead,
                repo,
                route_id=route_id,
                chosen_skill=chosen,
                chosen_skill_label=result["label"],
                primary_workflow=result["primary_workflow"],
                overlays=result["overlays"],
                discarded_overlays=result["discarded_overlays"],
                curiosity_required=result["curiosity_required"],
                human_gate_required=result["human_gate_required"],
                **({"evidence_join": evidence_join} if evidence_join is not None else {}),
            )
            # Bookbag writers use None to report that no artifact was written
            # (for example, a missing bead or lock timeout). Do not claim a
            # durable route in that case; callers may need to withhold a ready
            # signal and retry instead.
            result["logged"] = written is not None
        except Exception as e:  # noqa: BLE001 — logging must never break dispatch
            logger.warning("ce_router: failed to log chosen_skill to bookbag: %s", e)
            result["logged"] = False

    return result
