"""Backward-compatibility façade for the split director package.

This module re-exports everything from ``school_core.director`` so that
existing callers (conductor, issue_bridge, cli, tests, etc.) that do
``from director import ...`` or ``import director`` continue to work
unchanged. The actual logic lives in ``school_core.director.*`` submodules.
"""

# Re-export everything from the new package
from school_core.director import *  # noqa: F401,F403
from school_core.director import (
    triage_issue,
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
    _run_two_judge_review,
    _resolve_repo_path,
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

# ── Legacy module-level state ──────────────────────────────────────────────
# These are re-exported from selection.py but kept here for any code that
# accesses them as director.<name> directly.

# Re-import for direct access
from school_core.director.selection import (
    _active_sessions,
    SLEEP_TIMEOUT_MINUTES,
    SLEEP_CONTEXT_PRESSURE_THRESHOLD,
    SYSTEM_PROMPTS,
    DEFAULT_SYSTEM_PROMPT,
    ROLE_SYSTEM_PROMPTS,
    ROLE_ANCHOR_DOMAINS,
)

# ── Legacy imports that director.py used to provide ────────────────────────
# These are needed by tests and other modules that import from director.

import json
import os
import re
import sys
import yaml
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scoring import ScoreStore, GATES
from routing import route_task
from sleep_state import execute_sleep, execute_wake, load_session, SessionNotFoundError
from executor import call_model, COMBO_MAP, ExecutorError, get_role_for_domain
from trajectory import capture_trajectory, trajectories_for_training, list_trajectories as _list_trajectories
from engram_adapter import engram_available
from training.lora_pipeline import has_adapter
from cocoindex_client import cocoindex_available
from context_orchestrator import DEFAULT_VAULT, enrich_prompt
from repo_reader import CACHE_DIR as _REPO_CACHE_DIR
from anchor_loader import AnchorRegistry
from triage_classifier import classify_issue
from activity_log import get_log
from decision_log import get_decision_log, DecisionType
from escalation_log import EscalationLog
from bookbag import write_bookbag, update_bookbag, read_bookbag, bead_path, REPO_GLOBAL
from teacher_feedback import build_teacher_evidence, persist_teacher_evidence, routing_signal
from adversarial_reviewer import (
    AdversarialReviewer,
    LensType,
    Verdict,
    Finding,
    Severity,
    VerificationCoevolution,
    CoevolutionReport,
    ReviewResult,
    extract_balanced_json,
)
from orca_executor import OrcaExecutionManager, CodeExtractor, OrcaUnavailableError
from scripts.spec_gate import check_dod, _load_spec
from verify_gate import run_verify_gate
from pipeline_metrics import PipelineMetrics
from review_packet import ReviewPacket
from score_finalizer import finalize_score

# ── TaskRunner class ───────────────────────────────────────────────────────
# The TaskRunner class is too large to re-export cleanly, so we provide
# a thin wrapper that delegates to the new implementation.

from school_core.director.selection import (
    resolve_role,
    build_system_prompt,
    inject_context,
    auto_sleep_check,
    gate_and_readiness,
    _try_a2a_fallback,
    _active_sessions,
    SLEEP_TIMEOUT_MINUTES,
    SLEEP_CONTEXT_PRESSURE_THRESHOLD,
    SYSTEM_PROMPTS,
    DEFAULT_SYSTEM_PROMPT,
    ROLE_SYSTEM_PROMPTS,
    ROLE_ANCHOR_DOMAINS,
    _resolve_repo_path,
    _track_session_start,
    _track_session_activity,
    _should_auto_sleep,
    _get_anchor_registry,
    _anchor_context,
)

from school_core.director.review import _run_two_judge_review
from school_core.director.scoring import (
    _resolve_capability_metadata,
    _attach_teacher_evidence,
    _agent_role,
    _load_escalation_thresholds,
    _get_threshold,
    _check_readiness,
    _acceptance_checks_from_spec,
    _synthesize_judge_narratives,
    _record_acrouter_outcome,
    evaluate_and_update,
)


class TaskRunner:
    """Encapsulates the run_task pipeline.

    This is a backward-compat wrapper that preserves the original TaskRunner
    interface. The actual logic is split across school_core.director modules.
    """

    def __init__(
        self,
        prompt: str,
        domain: str = "_default",
        difficulty: str = "easy",
        force_agent: str = None,
        store: ScoreStore = None,
        system_prompt: str = None,
        session_id: Optional[str] = None,
        skip_review: bool = False,
        repo: str = REPO_GLOBAL,
        repo_path: Optional[Path] = None,
        ce_enabled: bool = False,
        complex_task: bool = False,
        dod_gate: bool = False,
        skip_readiness: bool = False,
        isolated_phases: bool = False,
        phase_students: Optional[list] = None,
        phase_drop_rate: float = 0.5,
        phase_seeds: Optional[list] = None,
        provided_student_output: Optional[str] = None,
        preverified_verification: Optional[dict] = None,
        pipeline_metrics: Optional[PipelineMetrics] = None,
        synthesize_narratives: bool = False,
    ):
        # ── Inputs ────────────────────────────────────────────────
        self.prompt = prompt
        self.domain = domain
        self.difficulty = difficulty
        self.force_agent = force_agent
        self.store = store if store is not None else ScoreStore()
        self.system_prompt = system_prompt
        self.session_id = session_id
        self.skip_review = skip_review
        self.repo = repo
        self.repo_path = repo_path
        self.ce_enabled = ce_enabled
        self.complex_task = complex_task
        self.dod_gate = dod_gate
        self.skip_readiness = skip_readiness
        self.isolated_phases = isolated_phases
        self.phase_students = phase_students
        self.phase_drop_rate = phase_drop_rate
        self.phase_seeds = phase_seeds
        self.provided_student_output = provided_student_output
        self.preverified_verification = preverified_verification
        self.pipeline_metrics = pipeline_metrics
        self.synthesize_narratives = synthesize_narratives

        # ── Derived state (set during run()) ─────────────────────
        self.role: str = ""
        self.role_score: float = 0.0
        self.capability: Optional[dict] = None
        self.old_score: float = 0.0
        self.response: str = ""
        self.error: Optional[str] = None
        self.ce_phases: list = []
        self.plan_result: Optional[dict] = None
        self.escalated: bool = False
        self.traj_path = None
        self.context_blob: str = ""
        self.bead: Optional[str] = None

    def run(self) -> dict:
        """Execute the full task pipeline and return the result dict."""
        if self.provided_student_output is not None and self.isolated_phases:
            raise ValueError(
                "provided_student_output is invalid with isolated_phases: "
                "that path reasons its own response and returns before the "
                "student model call."
            )

        # Isolated phases has its own early-return path
        if self.isolated_phases:
            return self._run_isolated_phases()

        # Determine the role: force_agent overrides domain mapping
        self._resolve_role()
        if self.role not in COMBO_MAP:
            return {
                "status": "error", "domain": self.domain,
                "difficulty": self.difficulty, "agent": self.role,
                "error": f"Unknown role '{self.role}' — not in COMBO_MAP",
            }

        # Build system prompt: role-specific prompt > domain-specific > default
        self._build_system_prompt()

        # Inject vault context (includes past bookbag feedback for this role).
        self._inject_context()

        # Auto-sleep check
        self._auto_sleep_check()

        # Gate check + readiness + A2A escalation
        gate_result = self._gate_and_readiness()
        if gate_result is not None:
            return gate_result

        # Resolve the capability contract after any readiness/A2A role change.
        self.capability = _resolve_capability_metadata(
            self.role, self.domain, self.difficulty, self.role_score,
        )

        # Execute the task: dispatch to the model, capture trajectory, handle
        # A2A fallback on hard failure
        early_return = self._call_model_and_sensors()
        if early_return is not None:
            return early_return

        # Bookbag + Two-Judge Review + final result assembly
        return self._persist_acceptance()

    def _run_isolated_phases(self) -> dict:
        from isolated_reasoning import run_isolated_phases

        if self.force_agent:
            base_role = self.force_agent
        else:
            base_role = get_role_for_domain(self.domain)
        students = list(self.phase_students or [base_role])

        def _phase_reason_fn(student_id, prompt, seed):
            return call_model(student_id, prompt, system_prompt=self.system_prompt)

        iso = run_isolated_phases(
            task_prompt=self.prompt,
            students=students,
            base_blocks={"role": base_role, "domain": self.domain,
                         "difficulty": self.difficulty},
            reason_fn=_phase_reason_fn,
            seeds=self.phase_seeds,
            drop_rate=self.phase_drop_rate,
        )
        get_decision_log().log(
            DecisionType.CONTEXT_RETRIEVED,
            agent="isolated-phases",
            context={"domain": self.domain, "students": students},
            choice={
                "isolated_phases": True,
                "vendi_score": round(iso.vendi_score, 4),
                "collapsed": iso.collapsed,
            },
            expected="Decoupled context should raise output diversity",
        )
        return {
            "status": "success",
            "domain": self.domain,
            "difficulty": self.difficulty,
            "agent": "isolated-phases",
            "students": students,
            "isolated_phases": True,
            "vendi_score": iso.vendi_score,
            "collapsed": iso.collapsed,
            "selected_student": iso.selected_student,
            "response": iso.selected_response,
            "phase_responses": [p.response for p in iso.phases],
            "error": None,
            "old_score": self.store.get_score(base_role, self.domain),
            "new_score": self.store.get_score(base_role, self.domain),
        }

    def _resolve_role(self) -> None:
        if self.force_agent:
            canonical_role = get_role_for_domain(self.domain)
            from resilience import force_agent_allowed
            if force_agent_allowed(self.force_agent, canonical_role,
                                    lora_twin=f"lora-{self.domain}"):
                self.role = self.force_agent
            else:
                sys.stderr.write(
                    f"[director] force_agent '{self.force_agent}' denied: not the "
                    f"capability profile for domain '{self.domain}' (expected "
                    f"'{canonical_role}'); falling back to domain role\n"
                )
                self.role = canonical_role
        else:
            self.role = get_role_for_domain(self.domain)
            if has_adapter(self.domain):
                lora_role = f"lora-{self.domain}"
                if self.store.get_score(lora_role, self.domain) == 0.0:
                    self.store.set_score(
                        lora_role, self.domain,
                        self.store.get_score(self.role, self.domain),
                    )
                self.role = lora_role

    def _build_system_prompt(self) -> None:
        if not self.system_prompt:
            if self.role in ROLE_SYSTEM_PROMPTS:
                self.system_prompt = ROLE_SYSTEM_PROMPTS[self.role]
            else:
                self.system_prompt = SYSTEM_PROMPTS.get(self.domain, DEFAULT_SYSTEM_PROMPT)

    def _auto_sleep_check(self) -> None:
        if self.session_id is not None and _should_auto_sleep(self.session_id):
            sys.stderr.write(
                f"[director] Auto-sleep: session {self.session_id} timed out "
                f"({SLEEP_TIMEOUT_MINUTES}min)\n"
            )
            sleep(
                session_id=self.session_id,
                agent=self.role,
                store=self.store,
            )

    def _resolve_repo(self) -> None:
        """Resolve the repository checkout path."""
        self.repo_path = _resolve_repo_path(self.repo, explicit_path=self.repo_path)

    def _inject_context(self) -> None:
        """Enrich the system prompt with vault context and semantic anchors."""
        self._resolve_repo()
        if self.pipeline_metrics is not None:
            with self.pipeline_metrics.stage("context"):
                context_blob = enrich_prompt(
                    self.domain, self.prompt,
                    vault_path=DEFAULT_VAULT,
                    session_id=self.session_id,
                    repo_path=self.repo_path,
                    metrics=self.pipeline_metrics,
                )
        else:
            context_blob = enrich_prompt(
                self.domain, self.prompt,
                vault_path=DEFAULT_VAULT,
                session_id=self.session_id,
                repo_path=self.repo_path,
            )
        self.context_blob = context_blob
        if self.pipeline_metrics is not None:
            self.pipeline_metrics.record_context("vault", hit=bool(context_blob))
        if context_blob:
            self.system_prompt = self.system_prompt + context_blob
            get_decision_log().log(
                DecisionType.CONTEXT_RETRIEVED,
                agent=self.role,
                context={"domain": self.domain, "prompt_length": len(self.prompt)},
                choice={"context_injected": True, "context_length": len(context_blob)},
                expected="Vault context should improve response quality",
            )

        # Inject semantic anchors from the AnchorRegistry
        anchor_str = _anchor_context(self.role)
        if anchor_str:
            self.system_prompt = (
                self.system_prompt + "\n\n---\n### Semantic Anchors\n" + anchor_str + "\n---"
            )
            get_decision_log().log(
                DecisionType.CONTEXT_RETRIEVED,
                agent=self.role,
                context={"domain": self.domain},
                choice={"anchors_injected": True},
                expected="Semantic anchors should improve constraint adherence",
            )

    def _gate_and_readiness(self) -> Optional[dict]:
        from routing import route_task

        if self.difficulty not in GATES:
            raise ValueError(f"Invalid difficulty '{self.difficulty}'")

        if self.force_agent:
            self.role_score = self.store.get_score(self.role, self.domain)
        else:
            route = route_task(self.store, self.domain, self.difficulty)
            if route.blocked:
                return {
                    "status": "blocked", "domain": self.domain,
                    "difficulty": self.difficulty, "agent": self.role,
                    "role_score": route.score or 0.0,
                    "gate_threshold": GATES.get(self.difficulty, 0),
                }
            self.role_score = route.score or 0.0

        self.escalated = False
        role_qualifies = self.role_score >= GATES.get(self.difficulty, 0)
        if not role_qualifies and not self.skip_readiness:
            confidence = _check_readiness(self.role, self.domain,
                                          self.difficulty, self.prompt)
            if confidence < _get_threshold(self.domain, self.difficulty):
                _escalation_log = EscalationLog()
                _escalation_log.log(
                    agent=self.role, domain=self.domain,
                    difficulty=self.difficulty,
                    confidence=confidence,
                    threshold=_get_threshold(self.domain, self.difficulty),
                    escalated_to="a2a_fallback",
                )
                sys.stderr.write(
                    f"[director] {self.role} not ready for "
                    f"{self.domain}/{self.difficulty} (confidence={confidence:.1f}) "
                    "— escalating\n"
                )
                esc = _try_a2a_fallback(self.role, self.prompt, self.system_prompt)
                if esc is not None:
                    self.role, self.response, self.error, self.escalated = esc
                else:
                    return {
                        "status": "blocked", "domain": self.domain,
                        "difficulty": self.difficulty, "agent": self.role,
                        "reason": f"readiness check failed (confidence={confidence:.1f}) "
                                  "and A2A fallback unavailable",
                    }
        return None

    def _call_model_and_sensors(self) -> Optional[dict]:
        self.old_score = self.store.get_score(self.role, self.domain)
        self.error = None
        self.response = ""
        self.ce_phases = []

        get_log().start_task(
            agent=self.role, domain=self.domain, difficulty=self.difficulty,
            role=_agent_role(self.role, self.role_score),
            prompt_preview=self.prompt[:80],
        )

        # Rank 4b: retrieve prior similar trajectories
        prior = _list_trajectories(domain=self.domain, limit=6)
        if prior:
            scored = [t for t in prior
                      if t.get('task_score') is not None and t['task_score'] > 0]
            unscored = [t for t in prior
                        if t.get('task_score') is None or t['task_score'] == 0]
            scored.sort(key=lambda t: t['task_score'], reverse=True)
            selected = (scored + unscored)[:3]
            prior_blob = "\n\n---\n### Prior Approaches\n" + "\n".join(
                f"- [{t.get('timestamp','?')[:10]}] **{t.get('agent','?') or '?'}** "
                f"(score={t.get('task_score') or 0:.1f}): "
                f"{(t.get('response') or '')[:240]}"
                for t in selected
                if t.get('response') is not None
            ) + "\n---"
            self.system_prompt = self.system_prompt + prior_blob

        # Rank 5: complex-task decomposition
        self.plan_result = None
        if self.complex_task:
            from scripts.student_plan import generate_plan, execute_plan, gate_plan
            plan = generate_plan(task_prompt=self.prompt)

            gate_result = gate_plan(plan["plan_path"], task_id=plan["task_id"])
            if gate_result.get("decision") == "annotated":
                self.error = f"Plan revised by human: {gate_result.get('feedback', '')}"
                return
            elif gate_result.get("decision") == "dismissed":
                self.error = "Plan dismissed by human reviewer"
                return

            self.plan_result = execute_plan(
                plan,
                role=self.role, domain=self.domain, difficulty=self.difficulty,
                store=self.store, repo=self.repo,
            )
            self.response = (
                f"Plan {plan['task_id']} executed: "
                f"{len(self.plan_result['sub_task_results'])} sub-task(s), "
                f"all_passed={self.plan_result['all_passed']}"
            )
            self.ce_phases = [f"plan:{plan['task_id']}"]
        elif self.ce_enabled:
            from scripts.ce_runner import run_ce_loop
            ce_result = run_ce_loop(
                task_prompt=self.prompt,
                domain=self.domain,
                role=self.role,
                difficulty=self.difficulty,
                repo=self.repo,
            )
            if ce_result["status"] != "success":
                self.error = ce_result.get("error", "CE loop failed")
            else:
                self.response = (
                    f"CE execution completed. Artifacts written to "
                    f"docs/solutions/{ce_result['task_id']}/"
                )
                self.ce_phases = ce_result["ce_phases"]
        else:
            try:
                if self.provided_student_output is not None:
                    self.response = self.provided_student_output
                else:
                    if self.pipeline_metrics is not None:
                        with self.pipeline_metrics.stage("student_model"):
                            self.response = call_model(
                                self.role, self.prompt, system_prompt=self.system_prompt,
                            )
                        self.pipeline_metrics.record_model(
                            self.role,
                            prompt_chars=len(self.prompt or "") + len(self.system_prompt or ""),
                            output_chars=len(self.response or ""),
                        )
                    else:
                        self.response = call_model(
                            self.role, self.prompt, system_prompt=self.system_prompt,
                        )
            except Exception as e:
                self.error = str(e)

        self.traj_path = capture_trajectory(
            domain=self.domain, difficulty=self.difficulty, agent=self.role,
            prompt=self.prompt, system_prompt=self.system_prompt,
            response=self.response,
            task_score=0.0 if self.error else None,
            old_score=self.old_score,
            new_score=self.store.get_score(self.role, self.domain) if self.error else None,
            error=self.error,
        )

        if self.error:
            esc = _try_a2a_fallback(self.role, self.prompt, self.system_prompt)
            if esc is not None:
                self.role, self.response, self.error, self.escalated = esc

            if self.error:
                self.store.update_score(self.role, self.domain, 0.0)
                get_log().task_error(agent=self.role, domain=self.domain, error=self.error)
                _record_acrouter_outcome(self.role, success=False, quality=0.0)
                return {
                    "status": "error", "domain": self.domain,
                    "difficulty": self.difficulty, "agent": self.role,
                    "error": self.error, "old_score": self.old_score,
                    "new_score": self.store.get_score(self.role, self.domain),
                    "trajectory": self.traj_path,
                    "capability": self.capability,
                }

        get_log().finish_task(
            agent=self.role, domain=self.domain,
            score=self.store.get_score(self.role, self.domain), success=True,
        )
        return None

    def _run_verify_gate(self, result: dict) -> dict:
        """Rank 6: spec-gate (DOD checker)."""
        if self.dod_gate:
            spec = _load_spec(self.bead, self.repo)
            if spec is not None:
                gate_result = check_dod(self.bead, result, repo=self.repo)
                result["dod_gate"] = gate_result
                if not gate_result["passed"]:
                    result["accepted"] = False
        return result

    def _run_adversarial_review(self) -> tuple:
        """Run the two-judge adversarial review. Returns (review, orca_error)."""
        try:
            review = _run_two_judge_review(
                bead=self.bead,
                output=self.response,
                task={"title": self.prompt[:100], "body": self.prompt,
                      "domain": self.domain, "difficulty": self.difficulty},
                codebase_context=self.context_blob or "",
                role="reviewer",
                repo=self.repo,
                repo_path=self.repo_path,
                preverified_verification=self.preverified_verification,
                pipeline_metrics=self.pipeline_metrics,
                synthesize_narratives=self.synthesize_narratives,
            )
            return review, False
        except OrcaUnavailableError as e:
            return {"error": str(e)}, True

    def _compute_score(self, review: dict) -> float:
        """Compute task_score from the review result."""
        if review["accepted"]:
            return max(60, review["combined_score"])
        else:
            return min(40, review["combined_score"])

    def _persist_acceptance(self) -> dict:
        import uuid
        self.bead = f"{self.role}-{self.domain}-{uuid.uuid4().hex[:8]}"

        write_bookbag(
            self.bead,
            student=self.role,
            domain=self.domain,
            difficulty=self.difficulty,
            task=self.prompt[:200],
            output=self.response,
            repo=self.repo,
        )

        if self.skip_review:
            result = {
                "status": "success",
                "domain": self.domain,
                "difficulty": self.difficulty,
                "agent": self.role,
                "escalation": self.escalated,
                "prompt": self.prompt,
                "response": self.response,
                "error": None,
                "old_score": self.old_score,
                "new_score": self.store.get_score(self.role, self.domain),
                "task_score": 0.0,
                "trajectory": self.traj_path,
                "capability": self.capability,
                "bookbag": str(bead_path(self.bead, self.repo)),
                "bead": self.bead,
                "review": {
                    "cto_verdict": "",
                    "coo_verdict": "",
                    "cto_score": 0,
                    "coo_score": 0,
                    "findings": [],
                    "accepted": False,
                },
                "async": True,
            }
            if self.ce_enabled:
                result["ce_phases"] = self.ce_phases
            sys.stderr.write(
                f"[director] Async dispatch: bead={self.bead} role={self.role} "
                f"\u2192 awaiting teacher review\n"
            )
            return result

        try:
            review = _run_two_judge_review(
                bead=self.bead,
                output=self.response,
                task={"title": self.prompt[:100], "body": self.prompt,
                      "domain": self.domain, "difficulty": self.difficulty},
                codebase_context=self.context_blob or "",
                role="reviewer",
                repo=self.repo,
                repo_path=self.repo_path,
                preverified_verification=self.preverified_verification,
                pipeline_metrics=self.pipeline_metrics,
                synthesize_narratives=self.synthesize_narratives,
            )
        except OrcaUnavailableError as e:
            sys.stderr.write(f"[director] Orca unavailable: {e}\n")
            self.store.update_score(self.role, self.domain, 0.0)
            return {
                "status": "error", "domain": self.domain,
                "difficulty": self.difficulty, "agent": self.role,
                "error": f"Orca sandbox unavailable: {e}",
                "old_score": self.old_score,
                "new_score": self.store.get_score(self.role, self.domain),
                "trajectory": self.traj_path,
                "capability": self.capability,
            }

        if review["accepted"]:
            task_score = max(60, review["combined_score"])
        else:
            task_score = min(40, review["combined_score"])

        result = {
            "status": "success",
            "domain": self.domain,
            "difficulty": self.difficulty,
            "agent": self.role,
            "escalation": self.escalated,
            "prompt": self.prompt,
            "response": self.response,
            "error": None,
            "old_score": self.old_score,
            "new_score": self.store.get_score(self.role, self.domain),
            "task_score": task_score,
            "trajectory": self.traj_path,
            "capability": self.capability,
            "bookbag": str(bead_path(self.bead)),
            "bead": self.bead,
            "review": review,
        }
        if self.ce_enabled:
            result["ce_phases"] = self.ce_phases
        if self.plan_result is not None:
            result["plan"] = {
                "task_id": self.plan_result["task_id"],
                "sub_task_count": len(self.plan_result["sub_task_results"]),
                "all_passed": self.plan_result["all_passed"],
            }

        if self.dod_gate:
            spec = _load_spec(self.bead, self.repo)
            if spec is not None:
                gate_result = check_dod(self.bead, result, repo=self.repo)
                result["dod_gate"] = gate_result
                if not gate_result["passed"]:
                    result["accepted"] = False

        _attach_teacher_evidence(result)
        return result


# ── run_task wrapper ────────────────────────────────────────────────────────

def run_task(
    prompt: str,
    domain: str = "_default",
    difficulty: str = "easy",
    force_agent: str = None,
    store: ScoreStore = None,
    system_prompt: str = None,
    session_id: Optional[str] = None,
    skip_review: bool = False,
    repo: str = REPO_GLOBAL,
    repo_path: Optional[Path] = None,
    ce_enabled: bool = False,
    complex_task: bool = False,
    dod_gate: bool = False,
    skip_readiness: bool = False,
    isolated_phases: bool = False,
    phase_students: Optional[list] = None,
    phase_drop_rate: float = 0.5,
    phase_seeds: Optional[list] = None,
    provided_student_output: Optional[str] = None,
    preverified_verification: Optional[dict] = None,
    pipeline_metrics: Optional[PipelineMetrics] = None,
    synthesize_narratives: bool = False,
) -> dict:
    """Thin wrapper around TaskRunner.run()."""
    return TaskRunner(
        prompt=prompt,
        domain=domain,
        difficulty=difficulty,
        force_agent=force_agent,
        store=store,
        system_prompt=system_prompt,
        session_id=session_id,
        skip_review=skip_review,
        repo=repo,
        repo_path=repo_path,
        ce_enabled=ce_enabled,
        complex_task=complex_task,
        dod_gate=dod_gate,
        skip_readiness=skip_readiness,
        isolated_phases=isolated_phases,
        phase_students=phase_students,
        phase_drop_rate=phase_drop_rate,
        phase_seeds=phase_seeds,
        provided_student_output=provided_student_output,
        preverified_verification=preverified_verification,
        pipeline_metrics=pipeline_metrics,
        synthesize_narratives=synthesize_narratives,
    ).run()


# ── Other public functions ──────────────────────────────────────────────────

def get_training_data(domain: str, min_score: float = 50.0) -> list:
    return trajectories_for_training(domain, min_score)


def available_combos() -> dict:
    return dict(COMBO_MAP)


def run_staff(
    plugin_name: str = None,
    vault_path: str = None,
    building: str = "default",
    config: dict = None,
    config_path: str = None,
) -> list:
    """Run one or all Staff plugins and return results."""
    from engram_adapter import engram_available
    from context_orchestrator import DEFAULT_VAULT

    store = ScoreStore()
    vault = vault_path or str(DEFAULT_VAULT)
    cfg = config or {}

    if config_path:
        import yaml
        try:
            cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        except (FileNotFoundError, Exception):
            pass

    loader = StaffLoader()
    plugins = loader.discover(cfg)

    if not plugins:
        return [{"status": "error", "summary": "no plugins found"}]

    results = []
    to_run = {plugin_name: plugins[plugin_name]} if plugin_name and plugin_name in plugins else plugins

    for name, plugin in to_run.items():
        plugin_cfg = cfg.get(name, {})
        sandbox = StaffSandbox(trust=plugin.trust, vault_path=vault)
        ctx = StaffContext(
            vault_path=vault,
            score_store=store,
            engram_available=engram_available(),
            cocoindex_available=cocoindex_available(),
            building=building,
            config=plugin_cfg,
        )
        try:
            get_log()._add({
                "type": "task_start",
                "agent": f"staff:{name}",
                "description": f"Staff plugin '{name}' started",
                "status": "in_progress",
            })
            result = plugin.run(sandbox, ctx)
            get_log().staff_run(plugin=name, summary=result.summary, metrics=result.metrics)
        except Exception as e:
            result = StaffResult(
                plugin_name=name, status="error", summary=str(e),
                score_recommendations=[], vault_writes=[], metrics={},
            )
            get_log().task_error(agent=f"staff:{name}", domain="staff", error=str(e))

        for rec in result.score_recommendations:
            try:
                store.apply_recommendation(rec)
            except ValueError as e:
                sys.stderr.write(f"[staff] {name} recommendation rejected: {e}\n")

        results.append({
            "plugin": result.plugin_name,
            "status": result.status,
            "summary": result.summary,
            "metrics": result.metrics,
            "score_changes": len(result.score_recommendations),
        })

    return results


def staff_list(vault_path: str = None, config_path: str = None) -> list:
    from context_orchestrator import DEFAULT_VAULT
    vault = vault_path or str(DEFAULT_VAULT)
    cfg = {}
    if config_path:
        import yaml
        try:
            cfg = yaml.safe_load(Path(config_path).read_text()) or {}
        except Exception:
            pass
    loader = StaffLoader()
    plugins = loader.discover(cfg)
    return [
        {"name": p.name, "trust": p.trust.value, "health": p.health_check()}
        for p in plugins.values()
    ]
