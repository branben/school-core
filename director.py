import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Also load from ~/.omniroute/.env if it exists (for OmniRoute keys)
OMNIRoute_ENV = Path.home() / ".omniroute" / ".env"
if OMNIRoute_ENV.exists():
    load_dotenv(OMNIRoute_ENV)
from concurrent.futures import ThreadPoolExecutor
import re
import sys
import yaml
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
    AdversarialReviewer, LensType, Verdict, Finding, Severity,
    VerificationCoevolution, CoevolutionReport, ReviewResult,
    extract_balanced_json,
)
from orca_executor import OrcaExecutionManager, CodeExtractor, OrcaUnavailableError
from scripts.spec_gate import check_dod, _load_spec
from verify_gate import run_verify_gate
from pipeline_metrics import PipelineMetrics
from review_packet import ReviewPacket
from score_finalizer import finalize_score

SYSTEM_PROMPTS = {
    "python-testing": (
        "You are a senior Python testing engineer. Write clear, thorough pytest tests. "
        "Follow Arrange-Act-Assert pattern. Use fixtures for shared setup. "
        "Parameterize for edge cases. Only output the test code — no explanation."
    ),
    "git-operations": (
        "You are a git expert. Provide precise git commands and strategies. "
        "Explain the approach concisely, then give the exact commands to run."
    ),
    "code-review": (
        "You are a senior code reviewer. Analyze code for correctness, security, "
        "maintainability, and style. Provide actionable feedback with specific line references. "
        "Flag any potential bugs, race conditions, or security issues immediately."
    ),
}

# Shared preamble for the "BEFORE responding, reason step-by-step" verification
# block used across multiple roles. Each role appends its own role-specific
# examples. The {ROLE_TERM} placeholder is replaced per role.
_VERIFICATION_PREAMBLE = (
    "\n"
    "BEFORE responding, reason step-by-step about whether your "
    "{ROLE_TERM} ACTUALLY SOLVES the problem \u2014 stop and think, do not rush.\n"
    "Verify that your approach is the RIGHT tool for the problem, "
    "not just A tool that works. For example:\n"
)

# Role-specific system prompts — each specialized role gets a tailored prompt.
ROLE_SYSTEM_PROMPTS = {
    "searcher": (
        "You are a Searcher \u2014 a specialized code search agent. "
        "You produce code-search suggestions that get reviewed for correctness. "
        "Your output should be precise commands (ripgrep, ast-grep, etc.) "
        "that another system can execute. "
        "Find relevant code, trace call paths, identify all references. "
        "Be exhaustive. Report file paths and line numbers. "
        "Apply [Five Whys] to trace root causes through the codebase.\n"
    ) + _VERIFICATION_PREAMBLE.replace("{ROLE_TERM}", "answer") + (
        "- If the task asks for \'a grep command to find TODO comments\', verify:\n"
        "  Does this command actually find ONLY comments? Or does it also match "
        "strings, variable names, and other non-comment occurrences?\n"
        "- If the task asks for \'the CSS selector for buttons inside a form\', verify:\n"
        "  Does this selector actually work? What edge cases might break it?\n"
        "- If the task asks for \'a bash one-liner to count lines\', verify:\n"
        "  Is it correct for filenames with spaces? Edge cases? Unicode?"
    ),
    "executor": (
        "You are an Executor \u2014 a specialized terminal operations agent. "
        "Your tools: shell commands, git operations, build systems, package managers. "
        "Provide exact, copy-pasteable commands. Verify exit codes. "
        "Apply [KISS] \u2014 prefer simple, composable commands over complex scripts.\n"
    ) + _VERIFICATION_PREAMBLE.replace("{ROLE_TERM}", "command") + (
        "- If asked for a git command to undo a commit, verify:\n"
        "  Does this command preserve history? What if the commit was already pushed?\n"
        "- If asked to find files modified today, verify:\n"
        "  Does the command handle filenames with spaces? Symlinks?\n"
        "- If asked to kill a process by name, verify:\n"
        "  Does this match only the intended process? What about multiple matches?"
    ),
    "reviewer": (
        "You are a Reviewer \u2014 a specialized code review agent. "
        "Your tools: adversarial review patterns, security analysis, correctness verification. "
        "Challenge every assumption. Find bugs, security issues, missing edge cases. "
        "Apply [Fagan Inspection] \u2014 systematic, checklist-driven review. "
        "Every piece of work passes through challenge before scoring."
    ),
    "browser": (
        "You are a Browser \u2014 a specialized web automation agent. "
        "Your tools: page navigation, form interaction, data extraction, screenshot capture. "
        "Navigate websites, fill forms, extract structured data. "
        "Report what you see \u2014 URLs, page titles, form states, extracted values.\n"
    ) + _VERIFICATION_PREAMBLE.replace("{ROLE_TERM}", "answer") + (
        "- If asked for a CSS selector, verify:\n"
        "  Does it actually match the intended elements? What if the DOM structure changes?\n"
        "- If asked to extract data from a page, verify:\n"
        "  Does the approach handle dynamic content? Pagination? Missing elements?\n"
        "- If asked to fill a form, verify:\n"
        "  Does the selector work for all form states? What about validation errors?"
    ),
    "coder": (
        "You are a Coder \u2014 a specialized code generation agent. "
        "Your tools: Python, TypeScript, testing frameworks, git. "
        "Write clean, correct, well-typed code. Follow [SOLID Principles]. "
        "Apply [TDD] \u2014 test first, then implement. "
        "Your output is used for [Distillation] into smaller models.\n"
        "\n"
        "[OUTPUT FORMAT — CRITICAL]\n"
        "Output ONLY the code changes. Place EVERY code change inside a"
        " ```python ... ``` code block (one block per file change). "
        "Do NOT include any planning, reasoning, step-by-step analysis, "
        "markdown headings, or explanatory text outside the code blocks.\n"
        "If the task has multiple files, output a separate code block for each.\n"
        "Never reject a task as underspecified. If the task is ambiguous, "
        "make a reasonable assumption and implement it.\n"
        "\n"
        "Correct example (no preamble, just code blocks):\n"
        "```python\n# backend/file.py\nclass Foo:\n    BAR = 42\n```\n"
        "```python\n# tests/test_file.py\ndef test_bar():\n    assert Foo.BAR == 42\n```\n"
    ) + _VERIFICATION_PREAMBLE.replace("{ROLE_TERM}", "OUTPUT") + (
        "- If writing a function to chunk a list, verify:\n"
        "  Does it handle empty lists? Edge cases like n > len(lst)? n <= 0?\n"
        "- If implementing an algorithm, verify:\n"
        "  Is this the right algorithm for the constraints? What is the time complexity?\n"
        "- If writing a test, verify:\n"
        "  Does the test actually test the behavior? What edge cases are missing?"
    ),
}

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful coding assistant. Provide clear, correct, and concise answers."
)

_escalation_log = EscalationLog()

# Session state tracking for sleep/wake
_accepting_tasks = True
_last_activity = None
_active_sessions = {}  # session_id -> {agent, building, task_queue, layer_0, episodic_history, start_time}

# Sleep configuration
SLEEP_TIMEOUT_MINUTES = 15
SLEEP_CONTEXT_PRESSURE_THRESHOLD = 0.70  # 70% of context window


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_acrouter_outcome(agent: str, success: bool, quality: float = 1.0) -> None:
    """Feed the routing outcome back into the ACRouter bandit.

    Wraps executor.record_routing_outcome so failures (e.g. the executor
    module not importable, or a missing combo) never crash run_task — routing
    feedback is best-effort and must not affect task correctness.
    """
    try:
        from executor import record_routing_outcome

        record_routing_outcome(agent, success=success, quality=quality)
    except Exception:
        # Routing feedback is non-critical; swallow everything so a
        # persistence/IO hiccup never breaks the actual task pipeline.
        pass


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
        # Observability must not make a task fail; the missing bundle remains
        # visible as null in the run record and can be diagnosed separately.
        sys.stderr.write(f"[director] capability resolution skipped: {e}\n")
        return None


def _attach_teacher_evidence(result: dict) -> Optional[dict]:
    """Persist teacher evidence and send its normalized signal to ACRouter.

    This is idempotent for a result: synchronous reviews and delayed teacher
    reviews both pass through this helper, but only the first pass records the
    router outcome. Evidence is attached to the existing trajectory, which is
    already sanitized and checkpointed by the school-loop workflow.
    """
    review = result.get("review")
    if not isinstance(review, dict) or result.get("teacher_evidence"):
        return result.get("teacher_evidence")

    # A DoD gate can reject work after the two judges pass. The final result
    # acceptance is authoritative for learning; never teach the router that a
    # spec-failed task succeeded merely because the raw review passed.
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


def _agent_role(agent: str, score: float) -> str:
    """Map agent (role) and score to a school role for activity logging."""
    if score >= 75:
        return "Faculty"
    elif score >= 50:
        return "Senior"
    elif score >= 25:
        return "Junior"
    return "Trainee"


def _track_session_start(session_id: str, agent: str, building: str = "default") -> None:
    """Register an active session for sleep/wake tracking."""
    _active_sessions[session_id] = {
        "agent": agent,
        "building": building,
        "task_queue": [],
        "layer_0": {},
        "episodic_history": [],
        "start_time": _now_iso(),
        "last_activity": _now_iso(),
        "tasks_completed": 0,
    }


def _track_session_activity(session_id: str, event: dict) -> None:
    """Record task activity in an active session."""
    if session_id in _active_sessions:
        _active_sessions[session_id]["last_activity"] = _now_iso()
        _active_sessions[session_id]["episodic_history"].append(event)
        if event.get("status") == "success":
            _active_sessions[session_id]["tasks_completed"] += 1


def _should_auto_sleep(session_id: str) -> bool:
    """Check if auto-sleep triggers (timeout or explicit request)."""
    if session_id not in _active_sessions:
        return False
    sess = _active_sessions[session_id]
    last = sess.get("last_activity")
    if last:
        from datetime import datetime as _dt, timezone as _tz
        try:
            last_dt = _dt.fromisoformat(last)
            elapsed = (_dt.now(_tz.utc) - last_dt).total_seconds() / 60
            if elapsed >= SLEEP_TIMEOUT_MINUTES:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _run_two_judge_review(
    bead: str,
    output: str,
    task: dict,
    codebase_context: str = "",
    role: str = "reviewer",
    repo: str = REPO_GLOBAL,
    repo_path: Optional[Path] = None,
    preverified_verification: Optional[dict] = None,
    pipeline_metrics: Optional[PipelineMetrics] = None,
    synthesize_narratives: Optional[bool] = None,
) -> dict:
    """Run CTO+COO two-judge adversarial review on student output.

    CTO (Chief Technical Officer): correctness + security lenses.
        "Does this code actually work? Is it secure? Are there bugs?"
    COO (Chief Operating Officer): completeness + acceptance criteria.
        "Does this address the issue fully? Are edge cases covered?"

    Both must return PASS for the work to be accepted.
    Returns dict with cto_verdict, coo_verdict, combined findings, and accepted flag.

    ``synthesize_narratives=None`` preserves the legacy direct-call behavior;
    the normal ``run_task`` path passes False so this non-gating enrichment does
    not delay issue completion.
    """
    if synthesize_narratives is None:
        synthesize_narratives = True

    def _call_model(prompt, sp=None, **kw):
        try:
            response = call_model(
                role, prompt, system_prompt=sp,
                timeout=kw.get("timeout", 90),
            )
        except Exception:
            if pipeline_metrics is not None:
                pipeline_metrics.record_model(
                    role,
                    prompt_chars=len(prompt or ""),
                )
            raise
        if pipeline_metrics is not None:
            pipeline_metrics.record_model(
                role,
                prompt_chars=len(prompt or ""),
                output_chars=len(response or ""),
            )
        return response
    if pipeline_metrics is not None:
        pipeline_metrics.record_call("two_judge_review")

    # ── Orca Execution ──
    # Execute student code in an Orca terminal sandbox before CTO review.
    # Exit code 0 → PASS signal. Runtime errors → HIGH findings (advisory, not
    # veto-level — extracted code may be context-dependent and can't run
    # standalone). OrcaUnavailableError is a hard failure — the pipeline cannot
    # verify code without a sandbox, so the exception propagates up to run_task().
    #
    # Language detection: for code-implementation tasks, resolve the repo path
    # and detect the project language. Orca only supports Python execution, so
    # non-Python repos (TypeScript, Rust, Go, etc.) skip the sandbox and rely
    # on CTO/COO code review instead.
    execution_findings: list = []
    executable_domains = {"python-coding", "python-testing", "code-implementation"}
    if task.get("domain") in executable_domains:
        orca = OrcaExecutionManager()  # Raises OrcaUnavailableError if Orca is down
        try:
            # Resolve repo path for language detection (cross-repo dispatch)
            orca_repo_path = _resolve_repo_path(repo, explicit_path=repo_path)
            lang = CodeExtractor.language_for_domain(
                task.get("domain", ""),
                repo_path=orca_repo_path,
            )
            # Skip Orca execution for non-Python languages (sandbox only
            # supports python3). The CTO/COO judges handle correctness.
            if lang is not None and lang != "python":
                execution_findings.append(Finding(
                    section="execution",
                    issue_class="language_not_supported",
                    severity=Severity.LOW,
                    citation=f"detected_language={lang}",
                    description=f"Orca sandbox only supports Python; skipping execution for {lang} project",
                    suggestion="",
                ))
            else:
                code = CodeExtractor.extract(output, language=lang)
                if code.strip():
                    result = orca.execute(code=code, bead=bead, timeout_ms=30000)

                    if result.timed_out:
                        execution_findings.append(Finding(
                            section="execution",
                            issue_class="timeout",
                            severity=Severity.HIGH,
                            citation="timed out after 30s",
                            description="Code execution timed out — possible infinite loop or blocking call",
                            suggestion="Ensure the code terminates in reasonable time",
                        ))
                    elif result.exit_code != 0:
                        # HIGH, not CRITICAL: extracted code may be a context-dependent
                        # snippet (e.g. TypeScript refactoring in a JS project) that
                        # can't run standalone in Orca.  The CTO/COO judges assess
                        # correctness; this finding is advisory only.
                        execution_findings.append(Finding(
                            section="execution",
                            issue_class="runtime_failure",
                            severity=Severity.HIGH,
                            citation=f"exit_code={result.exit_code}",
                            description=(result.stderr or "Unknown execution error")[:300],
                            suggestion="Fix the runtime errors above",
                        ))
                    else:
                        execution_findings.append(Finding(
                            section="execution",
                            issue_class="execution_passed",
                            severity=Severity.LOW,
                            citation="exit_code=0",
                            description=f"Code executed successfully in {result.duration_ms}ms",
                            suggestion="",
                        ))
                else:
                    execution_findings.append(Finding(
                        section="execution",
                        issue_class="no_code_found",
                        severity=Severity.LOW,
                        citation="code extraction returned empty",
                        description="No runnable code could be extracted from the student's output",
                        suggestion="",
                    ))
        except Exception as e:
            execution_findings.append(Finding(
                section="execution",
                issue_class="sandbox_error",
                severity=Severity.LOW,
                citation="Orca execution error",
                description=str(e)[:200],
                suggestion="",
            ))

    # ── Repo verify gate (BUILD lens) ──
    # The Orca step above executes the extracted *snippet*; this step runs the
    # repo's OWN hermetic verify commands (typecheck/test/lint declared in
    # project_verify.yaml, inside `nix develop .#verifyShell`) against a real
    # checkout when one resolves — the "compiler before the critic speaks"
    # layer (campus.md #3) that actually reaches the two teachers. Real
    # failures are CRITICAL (auto-veto): the repo's declared contract failing
    # on the actual checkout is hard execution evidence, not model opinion.
    # A skipped gate (no checkout path / no Nix / no declared commands) is a
    # LOUD LOW advisory — never a fabricated pass; VERIFY_GATE_STRICT=1
    # escalates the skip to a veto (cannot pass if we cannot run).
    # Set REVIEW_RUN_VERIFY_GATE=0 to disable entirely (hermetic unit runs).
    build_findings: list = []
    verification_output: str = ""
    verify_strict = os.environ.get("VERIFY_GATE_STRICT") == "1"
    if preverified_verification is not None:
        # A crew's verify result was produced against its disposable student
        # worktree before teardown. Re-running here would inspect the clean
        # cache checkout and can manufacture a repo_unresolved/gate_skipped
        # veto. Preserve the authoritative sensor result instead.
        vg = dict(preverified_verification)
        verification_output = json.dumps(vg, indent=2)[:2000]
        if vg.get("passed"):
            build_findings.append(Finding(
                section="build",
                issue_class="verification_passed",
                severity=Severity.LOW,
                citation=f"ran={vg.get('ran', 0)} commands (premerge)",
                description=f"Repo verify gate passed ({vg.get('ran', 0)} commands)",
                suggestion="",
            ))
        else:
            for failure in vg.get("failures", []) or [{"cmd": "(verify_gate)"}]:
                build_findings.append(Finding(
                    section="build",
                    issue_class="verify_failed",
                    severity=Severity.CRITICAL,
                    citation=f"cmd: {failure.get('cmd', '')}",
                    description=(failure.get("stderr") or "pre-merge verify gate failed")[:300],
                    suggestion="Fix the failing verify command (typecheck/test/lint)",
                ))
    elif (
        task.get("domain") in executable_domains
        and os.environ.get("REVIEW_RUN_VERIFY_GATE", "1").strip().lower()
        not in ("0", "false", "no")
    ):
        repo_path = _resolve_repo_path(repo, explicit_path=repo_path)
        if pipeline_metrics is not None:
            pipeline_metrics.record_call("verify_gate")
            pipeline_metrics.record_verification(invocations=1)
        if repo_path is None:
            # A missing checkout is an unrunnable gate, not an implicit pass.
            # Keep the default path advisory, but make strict mode a veto.
            vg = {
                "passed": False,
                "skipped": True,
                "failures": [{
                    "cmd": "(repo)",
                    "exit": None,
                    "stderr": "No repository checkout resolved — verify gate SKIPPED.",
                }],
                "ran": 0,
            }
            verification_output = json.dumps(vg, indent=2)[:2000]
            build_findings.append(Finding(
                section="build",
                issue_class="verification_skipped",
                severity=Severity.CRITICAL if verify_strict else Severity.LOW,
                citation="repo_unresolved",
                description=vg["failures"][0]["stderr"],
                suggestion="Resolve a cached checkout before running the build gate",
            ))
        else:
            try:
                # Pin the runner flake to this module's checkout. The review
                # may run from a workflow working-directory unrelated to the
                # school-core flake that provides verifyShell.
                vg = run_verify_gate(
                    repo_path=repo_path,
                    project_verify=None,
                    flake_path=Path(__file__).resolve().parent,
                )
                if pipeline_metrics is not None:
                    gate_metrics = vg.get("telemetry") or {}
                    pipeline_metrics.record_verification(
                        shell_starts=gate_metrics.get("shell_starts", 0),
                        commands=gate_metrics.get("commands", 0),
                        copied_bytes=gate_metrics.get("copied_bytes", 0),
                    )
                verification_output = json.dumps(vg, indent=2)[:2000]
                if vg.get("skipped"):
                    reason = (vg.get("failures") or [{}])[0].get(
                        "stderr", "verify gate could not run"
                    )
                    build_findings.append(Finding(
                        section="build",
                        issue_class="verification_skipped",
                        severity=Severity.CRITICAL if verify_strict else Severity.LOW,
                        citation="gate_skipped",
                        description=reason[:300],
                        suggestion=("Install Nix with a verifyShell and declare "
                                    "project_verify.yaml commands"),
                    ))
                elif vg.get("strict_escalated") or not vg.get("passed"):
                    for failure in vg.get("failures", []):
                        build_findings.append(Finding(
                            section="build",
                            issue_class="verify_failed",
                            severity=Severity.CRITICAL,
                            citation=f"cmd: {failure.get('cmd', '')}",
                            description=(failure.get("stderr") or "verify command failed")[:300],
                            suggestion="Fix the failing verify command (typecheck/test/lint)",
                        ))
                else:
                    build_findings.append(Finding(
                        section="build",
                        issue_class="verification_passed",
                        severity=Severity.LOW,
                        citation=f"ran={vg.get('ran', 0)} commands",
                        description=f"Repo verify gate passed ({vg.get('ran', 0)} commands)",
                        suggestion="",
                    ))
            except Exception as e:
                # Keep the review alive in soft mode, but never let strict mode
                # accept work after the compiler layer itself errored.
                vg = {
                    "passed": False,
                    "skipped": True,
                    "gate_error": True,
                    "failures": [{"cmd": "(verify_gate)", "exit": None,
                                  "stderr": str(e)[:500]}],
                    "ran": 0,
                }
                verification_output = json.dumps(vg, indent=2)[:2000]
                build_findings.append(Finding(
                    section="build",
                    issue_class="verify_gate_error",
                    severity=Severity.CRITICAL if verify_strict else Severity.LOW,
                    citation="exception",
                    description=str(e)[:200],
                    suggestion="Inspect the verify gate infrastructure before retrying",
                ))

    def _run_judge(lens_types):
        # Separate reviewer instances keep per-reviewer statistics isolated;
        # only immutable result values are merged below.
        judge = AdversarialReviewer(call_model_fn=_call_model)
        return judge.review(
            output=output,
            task=task,
            codebase_context=codebase_context,
            lens_types=lens_types,
        )

    def _run_both_judges():
        # CTO and COO are independent lenses. The fixed bound of two prevents
        # review fan-out while allowing their model calls to overlap.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="school-review") as pool:
            cto_future = pool.submit(_run_judge, [LensType.CORRECTNESS, LensType.SECURITY])
            coo_future = pool.submit(_run_judge, [LensType.COMPLETENESS])
            # Resolve by named future, not completion order, so persisted output
            # remains CTO then COO and acceptance is deterministic.
            return cto_future.result(), coo_future.result()

    if pipeline_metrics is not None:
        with pipeline_metrics.stage("review"):
            cto_result, coo_result = _run_both_judges()
    else:
        cto_result, coo_result = _run_both_judges()

    cto_verdict = cto_result.verdict.value  # "PASS" or "FAIL"
    coo_verdict = coo_result.verdict.value
    all_findings = execution_findings + build_findings + cto_result.findings + coo_result.findings
    # Acceptance requires both judges PASS at score >= 50. A CRITICAL finding
    # (from lens findings) is an automatic veto — broken or unsafe output cannot
    # be accepted even if both judges happen to say PASS.
    has_critical = any(
        getattr(f, "severity", None) == Severity.CRITICAL for f in all_findings
    )
    # A judge whose raw output could not be parsed is INCONCLUSIVE, not
    # approving. adversarial_reviewer's parse-failure branches return
    # verdict=PASS with no findings, which is indistinguishable from a real
    # approval — so a broken judge would actively vote to accept work nobody
    # reviewed. Live run 32319064467 logged lens_parse_failed twice while COO
    # scored 100 on the same issue. Never count that as a pass.
    parse_failed = bool(
        getattr(cto_result, "parse_failed", False)
        or getattr(coo_result, "parse_failed", False)
    )
    accepted = (
        cto_verdict == "PASS"
        and coo_verdict == "PASS"
        and cto_result.score >= 50
        and coo_result.score >= 50
        and not has_critical
        and not parse_failed
    )
    combined_score = (cto_result.score + coo_result.score) / 2.0

    # ── Verification-co-evolution pass (P2.2) ──
    # As the agent/harness improves, *fixed* acceptance checks stop measuring real
    # quality and become a reward to game. After review we record the capability
    # now demonstrated and, if it rose on a dimension no acceptance check covers,
    # we flag it as a coverage gap and propose hardening the check set (so the next
    # evaluation verifies the gain instead of rewarding it blindly). The report is
    # attached to the result and persisted to the bookbag for human review — the
    # loop never silently mutates the active check set.
    #
    # We reuse the CTO+COO sub-results (already traced per-lens) rather than
    # re-running the models, so the co-evolution pass adds ZERO extra LLM calls.
    coevolution_report: Optional["CoevolutionReport"] = None
    try:
        _coevo_checks = _acceptance_checks_from_spec(task, repo)
        _coevo = VerificationCoevolution(call_model_fn=_call_model)
        _merged = ReviewResult(
            verdict=cto_result.verdict if cto_result.verdict == coo_result.verdict
            else Verdict.FAIL,
            findings=cto_result.findings + coo_result.findings,
            lens_used=cto_result.lens_used,
            confidence=max(cto_result.confidence, coo_result.confidence),
            difficulty=task.get("difficulty", "medium"),
        )
        _trace: dict = dict(getattr(cto_result, "_lens_trace", {}))
        _trace.update(getattr(coo_result, "_lens_trace", {}))
        _merged._lens_trace = _trace  # type: ignore[attr-defined]
        coevolution_report = _coevo.analyze(_merged, task, _coevo_checks)
    except Exception as e:  # co-evolution is advisory; never break the acceptance verdict
        sys.stderr.write(f"[director] coevolution pass skipped: {e}\n")

    review_packet = ReviewPacket.create(
        artifact={"bead": bead, "repo": repo},
        execution={"findings": execution_findings},
        verification=verification_output,
        cto={
            "verdict": cto_verdict,
            "score": cto_result.score,
            "confidence": cto_result.confidence,
            "findings": [f.to_dict() for f in cto_result.findings],
        },
        coo={
            "verdict": coo_verdict,
            "score": coo_result.score,
            "confidence": coo_result.confidence,
            "findings": [f.to_dict() for f in coo_result.findings],
        },
        accepted=accepted,
    )

    # Update bookbag with review results
    update_bookbag(
        bead,
        cto_verdict=cto_verdict,
        coo_verdict=coo_verdict,
        findings=[f.to_dict() for f in all_findings],
        accepted=accepted,
        lens=f"cto({cto_verdict})+coo({coo_verdict})",
        verification=verification_output or None,
        repo=repo,
    )

    sys.stderr.write(
        f"[director] Two-judge review: CTO={cto_verdict} (score={cto_result.score:.0f}), "
        f"COO={coo_verdict} (score={coo_result.score:.0f}) → "
        f"{'ACCEPTED' if accepted else 'REJECTED'}\n"
    )

    # ── Per-judge narrative (optional, non-gating enrichment) ──
    # The normal issue path returns immediately after the structured verdict.
    # Direct/manual callers can opt into the legacy synchronous detail pass.
    if synthesize_narratives:
        cto_narrative, coo_narrative = _synthesize_judge_narratives(
            _call_model=_call_model,
            task=task,
            output=output,
            cto_verdict=cto_verdict, cto_score=cto_result.score, cto_lens=cto_result.lens_used,
            coo_verdict=coo_verdict, coo_score=coo_result.score, coo_lens=coo_result.lens_used,
            cto_findings=[f.to_dict() for f in cto_result.findings],
            coo_findings=[f.to_dict() for f in coo_result.findings],
        )
    else:
        cto_narrative, coo_narrative = None, None
    if cto_narrative:
        update_bookbag(bead, cto_narrative=cto_narrative, repo=repo)
    if coo_narrative:
        update_bookbag(bead, coo_narrative=coo_narrative, repo=repo)

    return {
        "cto_verdict": cto_verdict,
        "coo_verdict": coo_verdict,
        "cto_score": cto_result.score,
        "coo_score": coo_result.score,
        "cto_confidence": cto_result.confidence,
        "coo_confidence": coo_result.confidence,
        "confidence": max(cto_result.confidence, coo_result.confidence),
        "combined_score": combined_score,
        "findings": [f.to_dict() for f in all_findings],
        "build_findings": [f.to_dict() for f in build_findings],
        "build_verification": verification_output or None,
        "accepted": accepted,
        "coevolution": coevolution_report.to_dict() if coevolution_report else None,
        "cto_narrative": cto_narrative,
        "coo_narrative": coo_narrative,
        "review_packet": review_packet.to_dict(),
    }


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

    Returns ``(cto_narrative, coo_narrative)`` — dicts or None on any
    failure. The prompt demands JSON-only output; the narrative is written
    for a human reader AND a future agent (plain sentences, light STE, no
    internal jargon). The CTO block carries a "what to learn from this"
    line; the COO block is more conversational. Findings are summarized
    (not pasted) so the call stays small and cheap.
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
        block = extract_balanced_json(raw, "{", "}")
        parsed = json.loads(block)
        cto = parsed.get("cto") or {}
        coo = parsed.get("coo") or {}
        cto_narrative = {k: (v or "").strip() for k, v in cto.items() if isinstance(v, str) and v.strip()}
        coo_narrative = {k: (v or "").strip() for k, v in coo.items() if isinstance(v, str) and v.strip()}
        return (cto_narrative or None), (coo_narrative or None)
    except Exception as e:
        sys.stderr.write(f"[director] Judge-narrative synthesis skipped: {e}\n")
        return None, None


def _acceptance_checks_from_spec(task: dict, repo: str = REPO_GLOBAL) -> list[dict]:
    """Derive the current acceptance-check surface from the task's DoD spec.

    Each DoD criterion ``id`` names a capability dimension the acceptance surface
    already covers, so the co-evolution loop can tell a *covered* improvement from
    an *uncovered* one. Returns [] when no spec is present (most tasks) — in that
    case any capability gain is treated as uncovered, which is the correct default
    (no checks => every gain is a potential reward-hack surface).
    """
    spec = None
    try:
        spec = _load_spec(task.get("task_id") or task.get("id"))
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


def triage_issue(title: str, labels: list, body: str = "") -> dict:
    """Classify an issue using the local rule-based classifier. Returns {category, state}."""
    category, state = classify_issue(title, labels, body)
    return {"category": category, "state": state}


def _load_escalation_thresholds() -> dict:
    config_path = Path(__file__).parent / "config" / "escalation_thresholds.yaml"
    if not config_path.exists():
        return {"easy": 3, "medium": 5, "hard": 7, "diploma": 8}
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
    config_path = Path(__file__).parent / "config" / "escalation_thresholds.yaml"
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        domain_overrides = cfg.get("domain_overrides", {}).get(domain, {})
        if difficulty in domain_overrides:
            return float(domain_overrides[difficulty])
    return float(thresholds.get(difficulty, 0))


def _check_readiness(agent: str, domain: str, difficulty: str, prompt: str) -> float:
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


# Role → primary domain mapping for anchor selection.
# Determines which methodology/principle anchors are loaded for each role.
ROLE_ANCHOR_DOMAINS = {
    "searcher": ["debugging"],
    "executor": ["git-operations"],
    "reviewer": ["code-review"],
    "browser": [],  # No domain-specific anchors yet
    "coder": ["code-implementation", "python-testing"],
}

# Lazy-loaded singleton for the AnchorRegistry
_anchor_registry: Optional[AnchorRegistry] = None


def _get_anchor_registry() -> Optional[AnchorRegistry]:
    """Get or create the AnchorRegistry singleton."""
    global _anchor_registry
    if _anchor_registry is None:
        try:
            _anchor_registry = AnchorRegistry()
        except Exception:
            return None
    return _anchor_registry


def _anchor_context(role: str, domains: list[str] = None) -> Optional[str]:
    """Load role-relevant semantic anchors from the registry and format them
    as a compact context block for the system prompt.

    Always includes constraint anchors ([OneCommand], [NoExplanation], etc.).
    Also loads domain-specific methodology/principle anchors for the role.

    Non-blocking: returns None on any error or missing registry.
    """
    registry = _get_anchor_registry()
    if registry is None:
        return None

    lines = []

    # Always load constraint anchors (applicable to all roles)
    constraint_anchors = registry.get_anchors(tier="constraint")
    for a in constraint_anchors:
        lines.append(f"  {a.bracket_notation()} \u2014 {a.activation_pattern}")

    if lines:
        lines.insert(0, "Respect these output constraints:")
        lines.append("")

    # Load domain-specific methodology/principle anchors
    role_domains = domains or ROLE_ANCHOR_DOMAINS.get(role, [])
    domain_lines = []
    for d in role_domains:
        for a in registry.get_anchors(domain=d):
            if a.tier in ("methodology", "principle"):
                domain_lines.append(f"  {a.bracket_notation()} \u2014 {a.activation_pattern}")

    if domain_lines:
        domain_lines.insert(0, "Apply these methods and principles:")
        lines.extend(domain_lines)

    if not lines:
        return None

    return "\n".join(lines)




class TaskRunner:
    """Encapsulates the run_task pipeline: role resolution, context
    enrichment, model call, adversarial review, scoring, and acceptance
    persistence.

    Extracted from the monolithic run_task() function. Each phase is a
    separate method for testability and readability. run_task() remains
    as a thin wrapper that delegates to TaskRunner(...).run().
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

    # ──────────────────────────────────────────────────────────────
    # run_task() pipeline
    # ──────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────
    # Phase 1: isolated-reasoning short-circuit
    # ──────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────
    # Phase 2: role resolution
    # ──────────────────────────────────────────────────────────────

    def _resolve_role(self) -> None:
        if self.force_agent:
            # N4.3 (worst-day-ever): a forced agent must be the capability's
            # own resolved profile — it must NOT escalate to a higher-trust
            # role. The allowlist anchor is the domain's canonical role.
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
            # Prefer LoRA-tuned role if a trained adapter exists for this domain
            if has_adapter(self.domain):
                lora_role = f"lora-{self.domain}"
                # Seed LoRA agent score from base role so first run doesn't start at 0
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
        # Auto-sleep check (mutates the session via sleep()) if applicable.
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

    # ──────────────────────────────────────────────────────────────
    # Phase 3: context injection (vault, anchors)
    # ──────────────────────────────────────────────────────────────

    def _resolve_repo(self) -> None:
        """Resolve the repository checkout path."""
        self.repo_path = _resolve_repo_path(self.repo, explicit_path=self.repo_path)

    def _inject_context(self) -> None:
        """Enrich the system prompt with vault context and semantic anchors."""
        # Resolve repo path via _resolve_repo
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

        # Inject semantic anchors from the AnchorRegistry (constraint + domain-specific)
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

    # ──────────────────────────────────────────────────────────────
    # Phase 4: gate check + readiness + A2A escalation
    # ──────────────────────────────────────────────────────────────

    def _gate_and_readiness(self) -> Optional[dict]:
        # Gate check: use route_task to find if ANY agent qualifies — scores
        # live under model names (e.g. foundry-coder-1.5b), not role names
        # (e.g. coder). route_task searches all agents, so a Foundry-era
        # score in code-implementation still gates correctly.
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

        # Readiness check. On low confidence, escalate to the A2A fallback
        # (openhands) instead of blocking — this is the U8 "I Don't Know"
        # escalation path. When route_task already found a qualified agent
        # (score >= gate), skip the readiness check — it's redundant.
        self.escalated = False
        role_qualifies = self.role_score >= GATES.get(self.difficulty, 0)
        if not role_qualifies and not self.skip_readiness:
            confidence = _check_readiness(self.role, self.domain,
                                          self.difficulty, self.prompt)
            if confidence < _get_threshold(self.domain, self.difficulty):
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

    # ──────────────────────────────────────────────────────────────
    # Phase 5: model call + sensors + trajectory + A2A retry
    # ──────────────────────────────────────────────────────────────

    def _call_model_and_sensors(self) -> Optional[dict]:
        # Execute the task
        self.old_score = self.store.get_score(self.role, self.domain)
        self.error = None
        self.response = ""
        self.ce_phases = []

        get_log().start_task(
            agent=self.role, domain=self.domain, difficulty=self.difficulty,
            role=_agent_role(self.role, self.role_score),
            prompt_preview=self.prompt[:80],
        )

        # Rank 4b: retrieve prior similar trajectories from filesystem to inform
        # routing. Prefer scored trajectories, then fall back by recency.
        prior = _list_trajectories(domain=self.domain, limit=6)
        if prior:
            # Sort: scored (desc) first, then unscored, then by recency
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

        # Rank 5: complex-task decomposition into a bite-sized plan, executed as
        # per-sub-task CE/TDD loops (each sub-task is its own run_leaf call).
        self.plan_result = None
        if self.complex_task:
            from scripts.student_plan import generate_plan, execute_plan
            plan = generate_plan(task_prompt=self.prompt)
            self.plan_result = execute_plan(
                plan,
                role=self.role, domain=self.domain, difficulty=self.difficulty,
                store=self.store, repo=self.repo,
            )
            # The "response" becomes a summary of the plan execution.
            self.response = (
                f"Plan {plan['task_id']} executed: "
                f"{len(self.plan_result['sub_task_results'])} sub-task(s), "
                f"all_passed={self.plan_result['all_passed']}"
            )
            self.ce_phases = [f"plan:{plan['task_id']}"]
        # CE-enabled execution
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
            # Original execution path. U8: when the crew path already produced a
            # student deliverable (crew report.md), substitute it for the student
            # model call — the deliverable was created by a real code-producing
            # crew, so no model call happens here. Review/scoring treat it exactly
            # like any other response.
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
            # Try A2A fallback (reuses the same escalation helper)
            esc = _try_a2a_fallback(self.role, self.prompt, self.system_prompt)
            if esc is not None:
                self.role, self.response, self.error, self.escalated = esc

            if self.error:
                # Both primary role and A2A failed — NOW penalize the role
                self.store.update_score(self.role, self.domain, 0.0)
                get_log().task_error(agent=self.role, domain=self.domain, error=self.error)
                # ACRouter feedback: a hard routing failure is a strong negative
                # outcome for the combo that was selected.
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

    # ──────────────────────────────────────────────────────────────
    # Phase 6: bookbag + two-judge review + score + persist
    # ──────────────────────────────────────────────────────────────

    def _run_verify_gate(self, result: dict) -> dict:
        """Rank 6: spec-gate (DOD checker). Evaluate DOD criteria when a
        spec file exists for this bead."""
        if self.dod_gate:
            spec = _load_spec(self.bead, self.repo)
            if spec is not None:
                gate_result = check_dod(self.bead, result, repo=self.repo)
                result["dod_gate"] = gate_result
                if not gate_result["passed"]:
                    result["accepted"] = False
        return result

    def _run_adversarial_review(self) -> tuple[dict, bool]:
        """Run the two-judge adversarial review. Returns (review, orca_error).
        orca_error is True if OrcaUnavailableError was raised."""
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
        """Compute task_score from the review result.
        accepted → max(60, combined_score); rejected → min(40, combined_score)."""
        if review["accepted"]:
            return max(60, review["combined_score"])
        else:
            return min(40, review["combined_score"])

    def _persist_acceptance(self) -> dict:
        # ── Bookbag + Two-Judge Review ──
        # Student output goes into a bookbag. CTO+COO review the bookbag.
        # Both must PASS for the work to be accepted.
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
            # Phase 2 async dispatch: only LLM call + bookbag, no review.
            # Teachers (in persistent worktrees) will review the bookbag
            # asynchronously. The caller must poll for verdicts and score.
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
                "task_score": 0.0,  # Will be set after teacher review
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
            # Rank 2: include ce_phases ONLY when the CE loop ran (see note above).
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
            # Hard fail: Orca sandbox is required for executable domains.
            # Return a clean error instead of crashing the conductor.
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

        # Score reflects review: accepted → high score, rejected → penalty.
        # Note: callers (autonomous_loop, issue_bridge) call evaluate_and_update()
        # after run_task() — do NOT call store.update_score() here to avoid double-EMA.
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
        # Rank 2: include ce_phases ONLY when the CE loop ran. When ce_enabled is
        # False the key is intentionally absent (backward compat — callers and the
        # test_ce_disabled_behavior test assert `"ce_phases" not in result`).
        if self.ce_enabled:
            result["ce_phases"] = self.ce_phases
        # Rank 5: include plan ONLY when a complex-task plan was executed.
        if self.plan_result is not None:
            result["plan"] = {
                "task_id": self.plan_result["task_id"],
                "sub_task_count": len(self.plan_result["sub_task_results"]),
                "all_passed": self.plan_result["all_passed"],
            }

        # Rank 6: spec-gate (DOD checker). When a spec file exists for this
        # task/bead, evaluate every DOD criterion against the execution result.
        # The result carries a falsy "accepted" if any criterion fails, so the
        # existing review/scoring pipeline treats a spec-gate failure like a
        # normal CTO+COO rejection without extra plumbing.
        if self.dod_gate:
            spec = _load_spec(self.bead, self.repo)
            if spec is not None:
                gate_result = check_dod(self.bead, result, repo=self.repo)
                result["dod_gate"] = gate_result
                if not gate_result["passed"]:
                    result["accepted"] = False

        # Teacher evidence is durable on the trajectory and is the sole source for
        # the normalized router signal. This runs after optional DoD evaluation so
        # the returned result and the learning record describe the same decision.
        _attach_teacher_evidence(result)
        return result


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
    """Thin wrapper around TaskRunner.run().

    Preserves the original run_task() signature and return-dict contract so
    that issue_bridge.py and conductor.py callers are unaffected. The full
    pipeline (role resolution, context injection, model call, adversarial
    review, score, acceptance persistence) lives in TaskRunner.

    See TaskRunner's docstring and the original run_task() docstring for
    parameter documentation.
    """
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




def evaluate_and_update(
    result: dict,
    task_score: float,
    evaluation: str = None,
    store: ScoreStore = None,
) -> dict:
    """Compatibility façade for the extracted score finalizer."""
    return finalize_score(
        result,
        task_score,
        evaluation=evaluation,
        store=store,
        attach_teacher_evidence=_attach_teacher_evidence,
    )


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


def sleep(
    session_id: str,
    agent: str,
    store: ScoreStore = None,
    building: str = "default",
    task_queue: list = None,
    layer_0: dict = None,
    episodic_history: list = None,
    duration_minutes: float = 0.0,
) -> dict:
    """Execute sleep sequence for a session. Returns sleep result with state + consolidation."""
    if store is None:
        store = ScoreStore()
    get_log().agent_sleep(agent=agent, session_id=session_id)
    return execute_sleep(
        session_id=session_id,
        agent=agent,
        store=store,
        building=building,
        task_queue=task_queue,
        layer_0=layer_0,
        episodic_history=episodic_history,
        duration_minutes=duration_minutes,
    )


def wake(session_id: str) -> dict:
    """Execute wake sequence for a session. Returns wake result with restored state."""
    result = execute_wake(session_id=session_id)
    if result.get("state"):
        get_log().agent_wake(agent=result["state"].agent, session_id=session_id)
    return result


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


def _resolve_repo_path(
    repo: str,
    *,
    explicit_path: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the checkout for a task without silently crossing repositories.

    An explicit caller-provided checkout wins over slug lookup. This is needed
    for bridge replays where the authoritative candidate is local-only and is
    intentionally not present in the normal remote cache. Invalid explicit
    paths fail closed instead of falling back to an unrelated repository.
    """
    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.exists() and (candidate / ".git").exists():
            return candidate
        return None

    if not repo or repo == REPO_GLOBAL:
        return None

    # Check the repo_reader cache.
    cached = _REPO_CACHE_DIR / repo.replace("/", "__")
    if cached.exists() and (cached / ".git").exists():
        return cached

    # Fallback: if the slug matches the current checkout, use its root.
    try:
        from repo_default import default_repo
        if repo == default_repo():
            return Path(__file__).resolve().parent
    except Exception:
        pass

    return None


def _try_a2a_fallback(primary_role, prompt, system_prompt=None):
    """Attempt the A2A fallback (openhands) for a low-confidence/primary failure.

    Returns ``(role, response, error, escalated)`` on success, or ``None`` if
    openhands is not available. ``escalated`` is True so callers can flag the
    escalation in their result.
    """
    if "openhands" not in COMBO_MAP:
        return None
    try:
        response = call_model("openhands", prompt, system_prompt=system_prompt)
        return ("openhands", response, None, True)
    except Exception as e:
        sys.stderr.write(f"[director] A2A fallback failed: {e}\n")
        return None
