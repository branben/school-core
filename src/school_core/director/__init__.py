"""Director package — task selection, classification, review, and scoring.

This package splits the monolithic director.py into single-responsibility
modules. The legacy director.py re-exports everything from here for backward
compatibility.
"""

from school_core.director.classification import triage_issue
from school_core.director.scoring import (
    evaluate_and_update,
    _resolve_capability_metadata,
    _attach_teacher_evidence,
    _agent_role,
    _load_escalation_thresholds,
    _get_threshold,
    _check_readiness,
    _acceptance_checks_from_spec,
    _synthesize_judge_narratives,
    _record_acrouter_outcome,
)
from school_core.director.review import (
    _run_two_judge_review,
    _resolve_repo_path,
)
from school_core.director.selection import (
    _track_session_start,
    _track_session_activity,
    _should_auto_sleep,
    _get_anchor_registry,
    _anchor_context,
    resolve_role,
    build_system_prompt,
    inject_context,
    auto_sleep_check,
    gate_and_readiness,
    _try_a2a_fallback,
    sleep,
    wake,
    _active_sessions,
    SLEEP_TIMEOUT_MINUTES,
    SLEEP_CONTEXT_PRESSURE_THRESHOLD,
    SYSTEM_PROMPTS,
    DEFAULT_SYSTEM_PROMPT,
    ROLE_SYSTEM_PROMPTS,
    ROLE_ANCHOR_DOMAINS,
)

__all__ = [
    # classification
    "triage_issue",
    # scoring
    "evaluate_and_update",
    "_resolve_capability_metadata",
    "_attach_teacher_evidence",
    "_agent_role",
    "_load_escalation_thresholds",
    "_get_threshold",
    "_check_readiness",
    "_acceptance_checks_from_spec",
    "_synthesize_judge_narratives",
    "_record_acrouter_outcome",
    # review
    "_run_two_judge_review",
    "_resolve_repo_path",
    # selection
    "_track_session_start",
    "_track_session_activity",
    "_should_auto_sleep",
    "_get_anchor_registry",
    "_anchor_context",
    "resolve_role",
    "build_system_prompt",
    "inject_context",
    "auto_sleep_check",
    "gate_and_readiness",
    "_try_a2a_fallback",
    "sleep",
    "wake",
    "_active_sessions",
    "SLEEP_TIMEOUT_MINUTES",
    "SLEEP_CONTEXT_PRESSURE_THRESHOLD",
    "SYSTEM_PROMPTS",
    "DEFAULT_SYSTEM_PROMPT",
    "ROLE_SYSTEM_PROMPTS",
    "ROLE_ANCHOR_DOMAINS",
]
