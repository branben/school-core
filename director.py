import re
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scoring import ScoreStore, GATES
from sleep_state import execute_sleep, execute_wake, load_session, SessionNotFoundError
from executor import call_model, COMBO_MAP, ExecutorError, get_role_for_domain
from trajectory import capture_trajectory, trajectories_for_training
from engram_adapter import engram_available, save_trajectory as engram_save, delete_observation
from context_orchestrator import enrich_prompt
from anchor_loader import AnchorRegistry
from triage_classifier import classify_issue
from activity_log import get_log
from decision_log import get_decision_log, DecisionType
from escalation_log import EscalationLog
from bookbag import write_bookbag, update_bookbag, read_bookbag, bead_path
from adversarial_reviewer import AdversarialReviewer, LensType, Verdict, Finding, Severity
from orca_executor import OrcaExecutionManager, CodeExtractor, OrcaUnavailableError

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
        "\n"
        "BEFORE responding, reason step-by-step about whether your "
        "answer ACTUALLY SOLVES the problem \u2014 stop and think, do not rush.\n"
        "Verify that your approach is the RIGHT tool for the problem, "
        "not just A tool that works. For example:\n"
        "- If the task asks for \'a grep command to find TODO comments\', verify:\n"
        "  Does this command actually find ONLY comments? Or does it also match "
        "strings, variable names, and other non-comment occurrences?\n"
        "- If the task asks for \'the CSS selector for buttons inside a form\', verify:\n"
        "  Does this selector actually work? What edge cases might break it?\n"
        "- If the task asks for \'a bash one-liner to count lines\', verify:\n"
        "  Is it correct for filenames with spaces? Edge cases? Unicode?\n"
        "\n"
        "Respect [OneCommand], [NoExplanation], [OneWord], [NoExtras] when specified in the task description."
    ),
    "executor": (
        "You are an Executor \u2014 a specialized terminal operations agent. "
        "Your tools: shell commands, git operations, build systems, package managers. "
        "Provide exact, copy-pasteable commands. Verify exit codes. "
        "Apply [KISS] \u2014 prefer simple, composable commands over complex scripts.\n"
        "\n"
        "BEFORE responding, reason step-by-step about whether your "
        "command ACTUALLY SOLVES the problem \u2014 stop and think, do not rush.\n"
        "Verify that your approach is the RIGHT tool for the problem, "
        "not just A tool that works. For example:\n"
        "- If asked for a git command to undo a commit, verify:\n"
        "  Does this command preserve history? What if the commit was already pushed?\n"
        "- If asked to find files modified today, verify:\n"
        "  Does the command handle filenames with spaces? Symlinks?\n"
        "- If asked to kill a process by name, verify:\n"
        "  Does this match only the intended process? What about multiple matches?\n"
        "\n"
        "Respect [OneCommand], [NoExplanation], [OneWord], [NoExtras] when specified in the task description."
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
        "\n"
        "BEFORE responding, reason step-by-step about whether your "
        "answer ACTUALLY SOLVES the problem \u2014 stop and think, do not rush.\n"
        "Verify that your approach is the RIGHT tool for the problem, "
        "not just A tool that works. For example:\n"
        "- If asked for a CSS selector, verify:\n"
        "  Does it actually match the intended elements? What if the DOM structure changes?\n"
        "- If asked to extract data from a page, verify:\n"
        "  Does the approach handle dynamic content? Pagination? Missing elements?\n"
        "- If asked to fill a form, verify:\n"
        "  Does the selector work for all form states? What about validation errors?\n"
        "\n"
        "Respect [OneCommand], [NoExplanation], [OneWord], [NoExtras] when specified in the task description."
    ),
    "coder": (
        "You are a Coder \u2014 a specialized code generation agent. "
        "Your tools: Python, TypeScript, testing frameworks, git. "
        "Write clean, correct, well-typed code. Follow [SOLID Principles]. "
        "Apply [TDD] \u2014 test first, then implement. "
        "Your output is used for [Distillation] into smaller models.\n"
        "\n"
        "BEFORE responding, reason step-by-step about whether your "
        "CODE ACTUALLY SOLVES the problem \u2014 stop and think, do not rush.\n"
        "Verify that your approach is the RIGHT tool for the problem, "
        "not just A tool that works. For example:\n"
        "- If writing a function to chunk a list, verify:\n"
        "  Does it handle empty lists? Edge cases like n > len(lst)? n <= 0?\n"
        "- If implementing an algorithm, verify:\n"
        "  Is this the right algorithm for the constraints? What is the time complexity?\n"
        "- If writing a test, verify:\n"
        "  Does the test actually test the behavior? What edge cases are missing?\n"
        "\n"
        "Respect [OneCommand], [NoExplanation], [OneWord], [NoExtras] when specified in the task description."
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
) -> dict:
    """Run CTO+COO two-judge adversarial review on student output.

    CTO (Chief Technical Officer): correctness + security lenses.
        "Does this code actually work? Is it secure? Are there bugs?"
    COO (Chief Operating Officer): completeness + acceptance criteria.
        "Does this address the issue fully? Are edge cases covered?"

    Both must return PASS for the work to be accepted.
    Returns dict with cto_verdict, coo_verdict, combined findings, and accepted flag.
    """
    _call_model = lambda prompt, sp=None, **kw: call_model(
        role, prompt, system_prompt=sp,
        timeout=kw.get("timeout", 90),
    )
    reviewer = AdversarialReviewer(call_model_fn=_call_model)

    # ── Orca Execution ──
    # Execute student code in an Orca terminal sandbox before CTO review.
    # Exit code 0 → PASS signal. Runtime errors → CRITICAL findings with traceback.
    # OrcaUnavailableError is a hard failure — the pipeline cannot verify code
    # without a sandbox, so the exception propagates up to run_task().
    execution_findings: list = []
    executable_domains = {"python-coding", "python-testing", "code-implementation", "git-operations", "terminal"}
    if task.get("domain") in executable_domains:
        orca = OrcaExecutionManager()  # Raises OrcaUnavailableError if Orca is down
        try:
            lang = CodeExtractor.language_for_domain(task.get("domain", ""))
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
                    execution_findings.append(Finding(
                        section="execution",
                        issue_class="runtime_failure",
                        severity=Severity.CRITICAL,
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

    # CTO review: correctness + security
    cto_result = reviewer.review(
        output=output,
        task=task,
        codebase_context=codebase_context,
        lens_types=[LensType.CORRECTNESS, LensType.SECURITY],
    )

    # COO review: completeness + build verification
    coo_result = reviewer.review(
        output=output,
        task=task,
        codebase_context=codebase_context,
        lens_types=[LensType.COMPLETENESS],
    )

    cto_verdict = cto_result.verdict.value  # "PASS" or "FAIL"
    coo_verdict = coo_result.verdict.value
    all_findings = execution_findings + cto_result.findings + coo_result.findings
    # Acceptance requires both judges PASS at score >= 50. A CRITICAL finding
    # (from CTO review or execution/sandbox) is an automatic veto — broken or
    # unsafe output cannot be accepted even if both judges happen to say PASS.
    has_critical = any(
        getattr(f, "severity", None) == Severity.CRITICAL for f in all_findings
    )
    accepted = (
        cto_verdict == "PASS"
        and coo_verdict == "PASS"
        and cto_result.score >= 50
        and coo_result.score >= 50
        and not has_critical
    )
    combined_score = (cto_result.score + coo_result.score) / 2.0

    # Update bookbag with review results
    update_bookbag(
        bead,
        cto_verdict=cto_verdict,
        coo_verdict=coo_verdict,
        findings=[f.to_dict() for f in all_findings],
        accepted=accepted,
        lens=f"cto({cto_verdict})+coo({coo_verdict})",
    )

    sys.stderr.write(
        f"[director] Two-judge review: CTO={cto_verdict} (score={cto_result.score:.0f}), "
        f"COO={coo_verdict} (score={coo_result.score:.0f}) → "
        f"{'ACCEPTED' if accepted else 'REJECTED'}\n"
    )

    return {
        "cto_verdict": cto_verdict,
        "coo_verdict": coo_verdict,
        "cto_score": cto_result.score,
        "coo_score": coo_result.score,
        "combined_score": combined_score,
        "findings": [f.to_dict() for f in all_findings],
        "accepted": accepted,
    }


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


def run_task(
    prompt: str,
    domain: str = "_default",
    difficulty: str = "easy",
    force_agent: str = None,
    store: ScoreStore = None,
    system_prompt: str = None,
    session_id: Optional[str] = None,
    skip_review: bool = False,
    repo: str = "__global__",
) -> dict:
    """Route task to the specialized role for this domain. One role = one attempt.
    If the role fails, escalate to A2A fallback.

    Args:
        skip_review: If True, skip the two-judge CTO+COO review and write the
                     bookbag with empty verdicts. Used by Phase 2 async dispatch
                     where teachers review bookbags in their own worktree terminals.
                     The caller is responsible for waiting for teacher verdicts
                     via ``wait_for_verdicts()`` and scoring via ``evaluate_and_update()``.
    """
    if store is None:
        store = ScoreStore()

    # Determine the role: force_agent overrides domain mapping
    if force_agent:
        role = force_agent
    else:
        role = get_role_for_domain(domain)

    if role not in COMBO_MAP:
        return {"status": "error", "domain": domain, "difficulty": difficulty,
                "agent": role, "error": f"Unknown role '{role}' — not in COMBO_MAP"}

    # Build system prompt: role-specific prompt > domain-specific > default
    if not system_prompt:
        if role in ROLE_SYSTEM_PROMPTS:
            system_prompt = ROLE_SYSTEM_PROMPTS[role]
        else:
            system_prompt = SYSTEM_PROMPTS.get(domain, DEFAULT_SYSTEM_PROMPT)

    # Inject vault context (includes past bookbag feedback for this role)
    context_blob = enrich_prompt(domain, prompt)
    if context_blob:
        system_prompt = system_prompt + context_blob
        get_decision_log().log(
            DecisionType.CONTEXT_RETRIEVED,
            agent=role,
            context={"domain": domain, "prompt_length": len(prompt)},
            choice={"context_injected": True, "context_length": len(context_blob)},
            expected="Vault context should improve response quality",
        )

    # Inject semantic anchors from the AnchorRegistry (constraint + domain-specific)
    anchor_str = _anchor_context(role)
    if anchor_str:
        system_prompt = system_prompt + "\n\n---\n### Semantic Anchors\n" + anchor_str + "\n---"
        get_decision_log().log(
            DecisionType.CONTEXT_RETRIEVED,
            agent=role,
            context={"domain": domain},
            choice={"anchors_injected": True},
            expected="Semantic anchors should improve constraint adherence",
        )

    # Auto-sleep check
    if session_id is not None and _should_auto_sleep(session_id):
        sys.stderr.write(f"[director] Auto-sleep: session {session_id} timed out ({SLEEP_TIMEOUT_MINUTES}min)\n")
        sleep(
            session_id=session_id,
            agent=role,
            store=store,
        )

    # Gate check: is this role qualified for this difficulty?
    if difficulty not in GATES:
        raise ValueError(f"Invalid difficulty '{difficulty}'")

    role_score = store.get_score(role, domain)
    gate_threshold = GATES.get(difficulty, 0)

    if role_score < gate_threshold and not force_agent:
        return {"status": "blocked", "domain": domain, "difficulty": difficulty,
                "agent": role, "role_score": role_score, "gate_threshold": gate_threshold}

    # Readiness check (skip if only one candidate — readiness prompt is unreliable)
    if not force_agent and len([role]) > 1:
        confidence = _check_readiness(role, domain, difficulty, prompt)
        if confidence < _get_threshold(domain, difficulty):
            _escalation_log.log(
                agent=role, domain=domain, difficulty=difficulty,
                confidence=confidence, threshold=_get_threshold(domain, difficulty),
                escalated_to="a2a_fallback",
            )
            sys.stderr.write(f"[director] {role} not ready for {domain}/{difficulty} (confidence={confidence:.1f})\n")
            return {"status": "blocked", "domain": domain, "difficulty": difficulty,
                    "agent": role, "reason": f"readiness check failed (confidence={confidence:.1f})"}

    # Execute the task
    old_score = store.get_score(role, domain)
    error = None
    response = ""

    get_log().start_task(
        agent=role, domain=domain, difficulty=difficulty,
        role=_agent_role(role, role_score),
        prompt_preview=prompt[:80],
    )

    try:
        response = call_model(role, prompt, system_prompt=system_prompt)
    except Exception as e:
        error = str(e)
        # Don't penalize yet — A2A fallback may still succeed

    traj_path = capture_trajectory(
        domain=domain, difficulty=difficulty, agent=role,
        prompt=prompt, system_prompt=system_prompt,
        response=response, task_score=0.0 if error else None,
        old_score=old_score, new_score=store.get_score(role, domain) if error else None,
        error=error,
    )

    if error:
        # Try A2A fallback
        if "openhands" in COMBO_MAP:
            sys.stderr.write(f"[director] {role} failed, A2A fallback...\n")
            try:
                response = call_model("openhands", prompt, system_prompt=system_prompt)
                error = None
                role = "openhands"
            except Exception as e2:
                error = str(e2)

        if error:
            # Both primary role and A2A failed — NOW penalize the role
            store.update_score(role, domain, 0.0)
            get_log().task_error(agent=role, domain=domain, error=error)
            return {"status": "error", "domain": domain, "difficulty": difficulty,
                    "agent": role, "error": error, "old_score": old_score,
                    "new_score": store.get_score(role, domain), "trajectory": traj_path}

    get_log().finish_task(
        agent=role, domain=domain,
        score=store.get_score(role, domain), success=True,
    )

    # ── Bookbag + Two-Judge Review ──
    # Student output goes into a bookbag. CTO+COO review the bookbag.
    # Both must PASS for the work to be accepted.
    import uuid
    bead = f"{role}-{domain}-{uuid.uuid4().hex[:8]}"

    write_bookbag(
        bead,
        student=role,
        domain=domain,
        difficulty=difficulty,
        task=prompt[:200],
        output=response,
        repo=repo,
    )

    if skip_review:
        # Phase 2 async dispatch: only LLM call + bookbag, no review.
        # Teachers (in persistent worktrees) will review the bookbag
        # asynchronously. The caller must poll for verdicts and score.
        result = {
            "status": "success",
            "domain": domain,
            "difficulty": difficulty,
            "agent": role,
            "prompt": prompt,
            "response": response,
            "error": None,
            "old_score": old_score,
            "new_score": store.get_score(role, domain),
            "task_score": 0.0,  # Will be set after teacher review
            "trajectory": traj_path,
            "bookbag": str(bead_path(bead)),
            "bead": bead,
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
        sys.stderr.write(
            f"[director] Async dispatch: bead={bead} role={role} "
            f"→ awaiting teacher review\n"
        )
        return result

    try:
        review = _run_two_judge_review(
            bead=bead,
            output=response,
            task={"title": prompt[:100], "body": prompt, "domain": domain, "difficulty": difficulty},
            codebase_context=context_blob or "",
            role="reviewer",
        )
    except OrcaUnavailableError as e:
        # Hard fail: Orca sandbox is required for executable domains.
        # Return a clean error instead of crashing the conductor.
        sys.stderr.write(f"[director] Orca unavailable: {e}\n")
        store.update_score(role, domain, 0.0)
        return {
            "status": "error", "domain": domain, "difficulty": difficulty,
            "agent": role, "error": f"Orca sandbox unavailable: {e}",
            "old_score": old_score, "new_score": store.get_score(role, domain),
            "trajectory": traj_path,
        }

    # Score reflects review: accepted → high score, rejected → penalty.
    # Note: callers (autonomous_loop, issue_bridge) call evaluate_and_update()
    # after run_task() — do NOT call store.update_score() here to avoid double-EMA.
    if review["accepted"]:
        task_score = max(60, review["combined_score"])
    else:
        task_score = min(40, review["combined_score"])

    return {
        "status": "success",
        "domain": domain,
        "difficulty": difficulty,
        "agent": role,
        "prompt": prompt,
        "response": response,
        "error": None,
        "old_score": old_score,
        "new_score": store.get_score(role, domain),
        "task_score": task_score,
        "trajectory": traj_path,
        "bookbag": str(bead_path(bead)),
        "bead": bead,
        "review": review,
    }


def evaluate_and_update(
    result: dict,
    task_score: float,
    evaluation: str = None,
    store: ScoreStore = None,
) -> dict:
    if store is None:
        store = ScoreStore()

    if result.get("status") == "blocked":
        return result

    agent = result["agent"]
    domain = result["domain"]

    if result.get("status") == "error":
        task_score = 0.0

    old = store.get_score(agent, domain)
    new = store.update_score(agent, domain, task_score)
    old_gate = store.gate_for_score(old)
    new_gate = store.gate_for_score(new)

    crossed = None
    from scoring import GATES
    for gname, gthr in sorted(GATES.items(), key=lambda x: x[1]):
        if old < gthr <= new:
            crossed = gname

    trajectory_path = result.get("trajectory")
    if trajectory_path:
        import json
        with open(trajectory_path) as f:
            traj = json.load(f)
        traj["task_score"] = task_score
        traj["old_score"] = old
        traj["new_score"] = new
        traj["evaluation"] = evaluation
        with open(trajectory_path, "w") as f:
            json.dump(traj, f, indent=2, ensure_ascii=False)

        if engram_available():
            old_obs_id = traj.get("engram_obs_id")
            if old_obs_id:
                delete_observation(old_obs_id)
            new_obs_id = engram_save(traj, trajectory_path)
            if new_obs_id:
                traj["engram_obs_id"] = new_obs_id
                with open(trajectory_path, "w") as f:
                    json.dump(traj, f, indent=2, ensure_ascii=False)

    result["old_score"] = old
    result["new_score"] = new
    result["gate_crossed"] = crossed
    result["task_score"] = task_score
    # Log activity
    if crossed:
        get_log().gate_cross(
            agent=agent, domain=domain,
            from_gate=old_gate, to_gate=new_gate,
            score=new,
        )
    get_log().finish_task(
        agent=agent, domain=domain,
        score=new, success=(task_score >= 40),
        gate_crossed=crossed,
    )
    return result


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
            cocoindex_available=False,
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
