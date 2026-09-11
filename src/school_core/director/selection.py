"""Task selection logic and issue prioritization for the Director.

Owns role resolution, anchor selection, session/sleep tracking, and the
context-injection phase of the run_task pipeline.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from activity_log import get_log
from anchor_loader import AnchorRegistry
from context_orchestrator import DEFAULT_VAULT, enrich_prompt
from decision_log import get_decision_log, DecisionType
from executor import COMBO_MAP, get_role_for_domain
from pipeline_metrics import PipelineMetrics
from repo_reader import CACHE_DIR as _REPO_CACHE_DIR
from school_core.director.scoring import GATES
from school_core.director.scoring import _check_readiness, _get_threshold  # noqa: F401
from school_core.paths import REPO_ROOT
from sleep_state import execute_sleep
from training.lora_pipeline import has_adapter

# ── Module state ──────────────────────────────────────────────────────────

# Session state tracking for sleep/wake
_accepting_tasks = True
_last_activity = None
_active_sessions: dict = {}  # session_id -> {agent, building, task_queue, ...}

# Sleep configuration
SLEEP_TIMEOUT_MINUTES = 15
SLEEP_CONTEXT_PRESSURE_THRESHOLD = 0.70  # 70% of context window

# Role -> primary domain mapping for anchor selection.
ROLE_ANCHOR_DOMAINS = {
    "searcher": ["debugging"],
    "executor": ["git-operations"],
    "reviewer": ["code-review"],
    "browser": [],  # No domain-specific anchors yet
    "coder": ["code-implementation", "python-testing"],
}

# Lazy-loaded singleton for the AnchorRegistry
_anchor_registry: Optional[AnchorRegistry] = None

# Escalation log (lazy-loaded)
_escalation_log = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_escalation_log():
    global _escalation_log
    if _escalation_log is None:
        from escalation_log import EscalationLog
        _escalation_log = EscalationLog()
    return _escalation_log


# ── Session tracking ───────────────────────────────────────────────────────

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


# ── Anchor helpers ─────────────────────────────────────────────────────────

def _get_anchor_registry() -> Optional[AnchorRegistry]:
    """Get or create the AnchorRegistry singleton."""
    global _anchor_registry
    if _anchor_registry is None:
        try:
            _anchor_registry = AnchorRegistry()
        except Exception:
            return None
    return _anchor_registry


def _anchor_context(role: str, domains: Optional[list[str]] = None) -> Optional[str]:
    """Load role-relevant semantic anchors from the registry and format them
    as a compact context block for the system prompt.
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


# ── Role resolution ────────────────────────────────────────────────────────

def resolve_role(
    force_agent: Optional[str],
    domain: str,
    store,  # ScoreStore
) -> str:
    """Determine which role (agent) to use for a task.

    - force_agent overrides domain mapping (with allowlist check).
    - Otherwise, uses domain -> role mapping from executor.
    - Prefers LoRA-tuned role if a trained adapter exists for this domain.
    """
    if force_agent:
        canonical_role = get_role_for_domain(domain)
        from resilience import force_agent_allowed
        if force_agent_allowed(force_agent, canonical_role,
                                lora_twin=f"lora-{domain}"):
            return force_agent
        else:
            sys.stderr.write(
                f"[director] force_agent '{force_agent}' denied: not the "
                f"capability profile for domain '{domain}' (expected "
                f"'{canonical_role}'); falling back to domain role\n"
            )
            return canonical_role
    role = get_role_for_domain(domain)
    if has_adapter(domain):
        lora_role = f"lora-{domain}"
        if store.get_score(lora_role, domain) == 0.0:
            store.set_score(
                lora_role, domain,
                store.get_score(role, domain),
            )
        return lora_role
    return role


# ── System prompt construction ─────────────────────────────────────────────

# Default system prompt
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful coding assistant. Provide clear, correct, and concise answers."
)

# Domain-specific system prompts
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

# Shared preamble for the "BEFORE responding, reason step-by-step" block
_VERIFICATION_PREAMBLE = (
    "\n"
    "BEFORE responding, reason step-by-step about whether your "
    "{ROLE_TERM} ACTUALLY SOLVES the problem \u2014 stop and think, do not rush.\n"
    "Verify that your approach is the RIGHT tool for the problem, "
    "not just A tool that works. For example:\n"
)

# Role-specific system prompts
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
        "- If the task asks for 'a grep command to find TODO comments', verify:\n"
        "  Does this command actually find ONLY comments? Or does it also match "
        "strings, variable names, and other non-comment occurrences?\n"
        "- If the task asks for 'the CSS selector for buttons inside a form', verify:\n"
        "  Does this selector actually work? What edge cases might break it?\n"
        "- If the task asks for 'a bash one-liner to count lines', verify:\n"
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
        "[OUTPUT FORMAT \u2014 CRITICAL]\n"
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


def build_system_prompt(role: str, domain: str, system_prompt: Optional[str]) -> str:
    """Build system prompt: role-specific > domain-specific > default."""
    if system_prompt:
        return system_prompt
    if role in ROLE_SYSTEM_PROMPTS:
        return ROLE_SYSTEM_PROMPTS[role]
    return SYSTEM_PROMPTS.get(domain, DEFAULT_SYSTEM_PROMPT)


# ── Context injection ───────────────────────────────────────────────────────

def _resolve_repo_path(
    repo: str,
    *,
    explicit_path: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the checkout for a task without silently crossing repositories."""
    REPO_GLOBAL = "__global__"

    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.exists() and (candidate / ".git").exists():
            return candidate
        return None

    if not repo or repo == REPO_GLOBAL:
        return None

    cached = _REPO_CACHE_DIR / repo.replace("/", "__")
    if cached.exists() and (cached / ".git").exists():
        return cached

    try:
        from repo_default import default_repo
        if repo == default_repo():
            return REPO_ROOT
    except Exception:
        pass

    return None


def inject_context(
    role: str,
    domain: str,
    prompt: str,
    system_prompt: str,
    session_id: Optional[str],
    repo: str,
    repo_path: Optional[Path],
    pipeline_metrics: Optional[PipelineMetrics],
) -> tuple[str, str, Optional[Path]]:
    """Enrich the system prompt with vault context and semantic anchors.

    Returns (system_prompt, context_blob, resolved_repo_path).
    """
    resolved_repo_path = _resolve_repo_path(repo, explicit_path=repo_path)

    if pipeline_metrics is not None:
        with pipeline_metrics.stage("context"):
            context_blob = enrich_prompt(
                domain, prompt,
                vault_path=DEFAULT_VAULT,
                session_id=session_id,
                repo_path=resolved_repo_path,
                metrics=pipeline_metrics,
            )
    else:
        context_blob = enrich_prompt(
            domain, prompt,
            vault_path=DEFAULT_VAULT,
            session_id=session_id,
            repo_path=resolved_repo_path,
        )

    if pipeline_metrics is not None:
        pipeline_metrics.record_context("vault", hit=bool(context_blob))
    if context_blob:
        system_prompt = system_prompt + context_blob
        get_decision_log().log(
            DecisionType.CONTEXT_RETRIEVED,
            agent=role,
            context={"domain": domain, "prompt_length": len(prompt)},
            choice={"context_injected": True, "context_length": len(context_blob)},
            expected="Vault context should improve response quality",
        )

    # Inject semantic anchors from the AnchorRegistry
    anchor_str = _anchor_context(role)
    if anchor_str:
        system_prompt = (
            system_prompt + "\n\n---\n### Semantic Anchors\n" + anchor_str + "\n---"
        )
        get_decision_log().log(
            DecisionType.CONTEXT_RETRIEVED,
            agent=role,
            context={"domain": domain},
            choice={"anchors_injected": True},
            expected="Semantic anchors should improve constraint adherence",
        )

    return system_prompt, context_blob, resolved_repo_path


# ── Auto-sleep check ───────────────────────────────────────────────────────

def auto_sleep_check(
    session_id: Optional[str],
    role: str,
    store,  # ScoreStore
) -> None:
    """Auto-sleep check (mutates the session via sleep()) if applicable."""
    if session_id is not None and _should_auto_sleep(session_id):
        sys.stderr.write(
            f"[director] Auto-sleep: session {session_id} timed out "
            f"({SLEEP_TIMEOUT_MINUTES}min)\n"
        )
        sleep(session_id=session_id, agent=role, store=store)


# ── Gate check + readiness + A2A escalation ────────────────────────────────

def gate_and_readiness(
    role: str,
    domain: str,
    difficulty: str,
    prompt: str,
    role_score: float,
    force_agent: Optional[str],
    skip_readiness: bool,
    store,  # ScoreStore
    pipeline_metrics: Optional[PipelineMetrics] = None,
) -> Optional[dict]:
    """Run gate check + readiness + A2A escalation.

    Returns a result dict if the task should be blocked/escalated early,
    or None if execution should continue.
    """
    from routing import route_task

    if difficulty not in GATES:
        raise ValueError(f"Invalid difficulty '{difficulty}'")

    if not force_agent:
        route = route_task(store, domain, difficulty)
        if route.blocked:
            return {
                "status": "blocked", "domain": domain,
                "difficulty": difficulty, "agent": role,
                "role_score": route.score or 0.0,
                "gate_threshold": GATES.get(difficulty, 0),
            }
        role_score = route.score or 0.0

    # Readiness check
    escalated = False
    role_qualifies = role_score >= GATES.get(difficulty, 0)
    if not role_qualifies and not skip_readiness:
        confidence = _check_readiness(role, domain, difficulty, prompt)
        if confidence < _get_threshold(domain, difficulty):
            _get_escalation_log().log(
                agent=role, domain=domain,
                difficulty=difficulty,
                confidence=confidence,
                threshold=_get_threshold(domain, difficulty),
                escalated_to="a2a_fallback",
            )
            sys.stderr.write(
                f"[director] {role} not ready for "
                f"{domain}/{difficulty} (confidence={confidence:.1f}) "
                "— escalating\n"
            )
            esc = _try_a2a_fallback(role, prompt, None)
            if esc is not None:
                role, response, error, escalated = esc
                return {
                    "status": "success", "domain": domain,
                    "difficulty": difficulty, "agent": role,
                    "escalation": escalated, "response": response,
                }
            else:
                return {
                    "status": "blocked", "domain": domain,
                    "difficulty": difficulty, "agent": role,
                    "reason": f"readiness check failed (confidence={confidence:.1f}) "
                              "and A2A fallback unavailable",
                }
    return None


def _try_a2a_fallback(primary_role, prompt, system_prompt=None):
    """Attempt the A2A fallback (openhands) for a low-confidence/primary failure.

    Returns ``(role, response, error, escalated)`` on success, or ``None`` if
    openhands is not available.
    """
    # Late import so tests patching ``director.call_model`` still apply.
    from director import call_model

    if "openhands" not in COMBO_MAP:
        return None
    try:
        response = call_model("openhands", prompt, system_prompt=system_prompt)
        return ("openhands", response, None, True)
    except Exception as e:
        sys.stderr.write(f"[director] A2A fallback failed: {e}\n")
        return None


# ── Sleep/Wake wrappers ────────────────────────────────────────────────────

def sleep(
    session_id: str,
    agent: str,
    store=None,
    building: str = "default",
    task_queue: Optional[list] = None,
    layer_0: Optional[dict] = None,
    episodic_history: Optional[list] = None,
    duration_minutes: float = 0.0,
) -> dict:
    """Execute sleep sequence for a session."""
    if store is None:
        from scoring import ScoreStore
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
    """Execute wake sequence for a session."""
    from sleep_state import execute_wake
    result = execute_wake(session_id=session_id)
    if result.get("state"):
        get_log().agent_wake(agent=result["state"].agent, session_id=session_id)
    return result
