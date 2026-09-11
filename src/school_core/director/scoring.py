"""Director-specific scoring logic for the school-core pipeline.

Owns escalation thresholds, readiness checks, capability metadata resolution,
teacher evidence attachment, and the EMA score finalization façade
(``evaluate_and_update``) that delegates to ``score_finalizer``.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from activity_log import get_log
from executor import call_model
from scoring import ScoreStore, GATES
from teacher_feedback import build_teacher_evidence, persist_teacher_evidence, routing_signal
from adversarial_reviewer import extract_balanced_json
from school_core.paths import ESCALATION_PATH

import json


# ── Module state ──────────────────────────────────────────────────────────
_escalation_log = None  # Lazy-loaded below


def _get_escalation_log():
    global _escalation_log
    if _escalation_log is None:
        from escalation_log import EscalationLog
        _escalation_log = EscalationLog()
    return _escalation_log


# ── Score finalization façade ──────────────────────────────────────────────

def evaluate_and_update(
    result: dict,
    task_score: float,
    evaluation: Optional[str] = None,
    store: Optional[ScoreStore] = None,
) -> dict:
    """Compatibility façade for the extracted score finalizer."""
    from score_finalizer import finalize_score
    return finalize_score(
        result,
        task_score,
        evaluation=evaluation,
        store=store,
        attach_teacher_evidence=_attach_teacher_evidence,
    )


# ── Capability metadata ────────────────────────────────────────────────────

def _resolve_capability_metadata(
    role: str,
    domain: str,
    difficulty: str,
    score: float,
) -> Optional[dict]:
    """Resolve the persona/profile/skill/tool contract for observability."""
    try:
        from capabilities import resolve_capability

        bounded_score = max(0.0, min(100.0, float(score)))
        metadata = resolve_capability(
            domain,
            bounded_score,
            task_role=role,
            difficulty=difficulty,
        ).to_dict()
        metadata["selection_reason"] = (
            f"domain={domain}; task_role={metadata['task_role']}; "
            f"score={bounded_score:.1f}; difficulty={difficulty}"
        )
        return metadata
    except Exception as e:
        sys.stderr.write(f"[director] capability resolution skipped: {e}\n")
        return None


# ── Teacher evidence ───────────────────────────────────────────────────────

def _record_acrouter_outcome(agent: str, success: bool, quality: float = 1.0) -> None:
    """Feed the routing outcome back into the ACRouter bandit (best-effort)."""
    try:
        from executor import record_routing_outcome

        record_routing_outcome(agent, success=success, quality=quality)
    except Exception:
        # Routing feedback is non-critical.
        pass


def _attach_teacher_evidence(result: dict) -> Optional[dict]:
    """Persist teacher evidence and send its normalized signal to ACRouter.

    Idempotent for a result: synchronous reviews and delayed teacher reviews
    both pass through this helper, but only the first pass records the router
    outcome.
    """
    review = result.get("review")
    if not isinstance(review, dict) or result.get("teacher_evidence"):
        return result.get("teacher_evidence")

    review = dict(review)
    if "accepted" in result:
        review["accepted"] = bool(result["accepted"])

    evidence = build_teacher_evidence(
        agent=result.get("agent", ""),
        domain=result.get("domain", ""),
        difficulty=result.get("difficulty", ""),
        review=review,
    )
    persist_teacher_evidence(result.get("trajectory"), evidence)
    result["teacher_evidence"] = evidence
    success, quality = routing_signal(evidence)
    _record_acrouter_outcome(result.get("agent", ""), success=success, quality=quality)
    return evidence


# ── Agent role mapping ─────────────────────────────────────────────────────

def _agent_role(agent: str, score: float) -> str:
    """Map agent (role) and score to a school role for activity logging."""
    if score >= 75:
        return "Faculty"
    elif score >= 50:
        return "Senior"
    elif score >= 25:
        return "Junior"
    return "Trainee"


# ── Escalation thresholds ──────────────────────────────────────────────────

def _load_escalation_thresholds() -> dict:
    config_path = ESCALATION_PATH
    if not config_path.exists():
        return {"easy": 3, "medium": 5, "hard": 7, "diploma": 8}
    import yaml
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    thresholds = cfg.get("thresholds", {})
    return {
        "easy": thresholds.get("easy", 3),
        "medium": thresholds.get("medium", 5),
        "hard": thresholds.get("hard", 7),
        "diploma": thresholds.get("diploma", 8),
    }


def _get_threshold(domain: str, difficulty: str) -> float:
    thresholds = _load_escalation_thresholds()
    if ESCALATION_PATH.exists():
        import yaml
        with open(ESCALATION_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        domain_overrides = cfg.get("domain_overrides", {}).get(domain, {})
        if difficulty in domain_overrides:
            return float(domain_overrides[difficulty])
    return float(thresholds.get(difficulty, 0))


# ── Readiness check ────────────────────────────────────────────────────────

def _check_readiness(agent: str, domain: str, difficulty: str, prompt: str) -> float:
    # Late import so tests patching ``director.call_model`` still apply
    # (the function's globals live in this module, not director).
    from director import call_model

    readiness_prompt = (
        "On a scale of 1-10, how confident are you that you can solve this issue? "
        "Reply with only a number."
    )
    try:
        response = call_model(agent, readiness_prompt, timeout=10)
        match = re.search(r"(\d+(?:\.\d+)?)", response.strip())
        if not match:
            return 0.0
        return float(match.group(1))
    except Exception:
        return 0.0


# ── Acceptance checks from spec ────────────────────────────────────────────

def _acceptance_checks_from_spec(task: dict, repo: str = "__global__") -> list[dict]:
    """Derive the current acceptance-check surface from the task's DoD spec."""
    from scripts.spec_gate import _load_spec

    spec = None
    try:
        task_id = task.get("task_id") or task.get("id") or ""
        spec = _load_spec(task_id)
    except Exception:
        spec = None
    if not spec:
        return []
    checks: list[dict] = []
    for crit in spec.get("criteria", []):
        checks.append({
            "covers": [crit.get("id", "unknown")],
            "status": "active",
            "kind": "dod_criterion",
        })
    return checks


# ── Judge narrative synthesis ──────────────────────────────────────────────

def _synthesize_judge_narratives(
    _call_model,
    task: dict,
    output: str,
    cto_verdict: str, cto_score: float, cto_lens: str,
    coo_verdict: str, coo_score: float, coo_lens: str,
    cto_findings: list,
    coo_findings: list,
):
    """One best-effort LLM call producing short CTO + COO narrative blocks.

    Returns ``(cto_narrative, coo_narrative)`` — dicts or None on any failure.
    """
    def _compact(findings):
        if not findings:
            return "none"
        lines = []
        for f in findings[:5]:
            sev = (f.get("severity") or "LOW")
            desc = (f.get("description") or "")[:180]
            lines.append(f"- {sev}: {desc}")
        if len(findings) > 5:
            lines.append(f"- … +{len(findings) - 5} more")
        return "\n".join(lines)

    sys_prompt = (
        "You are the review panel of a small software school. Two reviewers "
        "(CTO and COO) just judged a student's work. Write their notes.\n\n"
        "Rules:\n"
        "- Plain, simple English. Short sentences. Light STE.\n"
        "- Be honest and specific, but kind — this is feedback to a student.\n"
        "- CTO speaks with a technical, direct tone (correctness + security).\n"
        "- COO speaks conversationally (completeness + acceptance criteria).\n"
        "- Do NOT invent findings that are not listed.\n"
        "- Respond with ONLY valid JSON. No markdown fences.\n\n"
        '{"cto": {"summary": "2-3 sentence lens review summary", '
        '"liked": "what was done well", "improve": "what could be better", '
        '"why_passed": "only if PASS, why it passed", '
        '"why_failed": "only if FAIL, why it failed", '
        '"lesson": "what to learn from this"}, '
        '"coo": {"summary": "2-3 sentence lens review summary", '
        '"liked": "what was done well", "improve": "what could be better", '
        '"why_passed": "only if PASS, why it passed", '
        '"why_failed": "only if FAIL, why it failed"}}'
    )
    user_prompt = (
        f"[TASK]\nTitle: {task.get('title', 'n/a')}\n"
        f"Domain: {task.get('domain', 'n/a')} · Difficulty: {task.get('difficulty', 'n/a')}\n\n"
        f"[CTO REVIEW] verdict={cto_verdict} score={cto_score:.0f} lenses={cto_lens}\n"
        f"Findings:\n{_compact(cto_findings)}\n\n"
        f"[COO REVIEW] verdict={coo_verdict} score={coo_score:.0f} lenses={coo_lens}\n"
        f"Findings:\n{_compact(coo_findings)}\n\n"
        f"[STUDENT OUTPUT (first 1200 chars)]\n{output[:1200]}"
    )
    try:
        raw = _call_model(user_prompt, sp=sys_prompt, timeout=60)
        block = extract_balanced_json(raw, "}", "}")
        parsed = json.loads(block)
        cto = parsed.get("cto") or {}
        coo = parsed.get("coo") or {}
        cto_narrative = {k: (v or "").strip() for k, v in cto.items() if isinstance(v, str) and v.strip()}
        coo_narrative = {k: (v or "").strip() for k, v in coo.items() if isinstance(v, str) and v.strip()}
        return (cto_narrative or None), (coo_narrative or None)
    except Exception as e:
        sys.stderr.write(f"[director] Judge-narrative synthesis skipped: {e}\n")
        return None, None
