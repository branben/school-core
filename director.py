import re
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scoring import ScoreStore, GATES
from sleep_state import execute_sleep, execute_wake, load_session, SessionNotFoundError
from executor import call_model, COMBO_MAP, ExecutorError, cloud_available, is_local_agent
from trajectory import capture_trajectory, trajectories_for_training
from engram_adapter import engram_available, save_trajectory as engram_save, delete_observation
from context_orchestrator import enrich_prompt
from triage_classifier import classify_issue
from prompt_composer import compose_prompt
from activity_log import get_log
from decision_log import get_decision_log, DecisionType
from escalation_log import EscalationLog

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

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful coding assistant. Provide clear, correct, and concise answers."
)

# Degraded mode state
_degraded_mode = False

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
    """Map agent score to role for activity logging."""
    if score >= 75:
        return "Faculty"
    elif score >= 50:
        return "Teacher"
    elif score >= 25:
        return "Senior Student"
    return "Student"


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


def set_degraded_mode(enabled: bool) -> None:
    """Enable/disable degraded mode (cloud unavailable)."""
    global _degraded_mode
    _degraded_mode = enabled
    sys.stderr.write(f"[director] Degraded mode {'ON' if enabled else 'OFF'}\n")


def is_degraded() -> bool:
    return _degraded_mode


def check_cloud_and_set_mode() -> bool:
    """Ping OmniRoute. If unreachable, auto-enable degraded mode. Returns True if cloud is available."""
    available = cloud_available()
    if not available and not _degraded_mode:
        set_degraded_mode(True)
    elif available and _degraded_mode:
        set_degraded_mode(False)
    return available


def _filter_local_candidates(candidates):
    """In degraded mode, prefer local (Foundry GPU) agents. Fall back to all if none qualify."""
    if not _degraded_mode:
        return candidates
    local = [a for a in candidates if is_local_agent(a)]
    return local if local else candidates


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


def run_task(
    prompt: str,
    domain: str = "_default",
    difficulty: str = "easy",
    force_agent: str = None,
    store: ScoreStore = None,
    system_prompt: str = None,
    session_id: Optional[str] = None,
) -> dict:
    """Try candidates in score order. In degraded mode, only local agents are used."""
    if store is None:
        store = ScoreStore()

    if not system_prompt:
        use_semantic_anchors = True
        is_local = is_local_agent(force_agent) if force_agent else True
        is_blocker = difficulty == "blocker"
        if use_semantic_anchors:
            system_prompt = compose_prompt(
                domain=domain,
                difficulty=difficulty,
                agent=force_agent or "default",
                store=store,
                domain_prompts=SYSTEM_PROMPTS,
                default_prompt=DEFAULT_SYSTEM_PROMPT,
                is_local=is_local,
                is_blocker=is_blocker,
            )
            # Log which anchors were chosen
            from prompt_composer import DOMAIN_ANCHORS, DIFFICULTY_ANCHORS, ROLE_ANCHORS
            chosen_anchors = []
            da = DOMAIN_ANCHORS.get(domain, {})
            chosen_anchors.extend(da.get("anchors", []))
            di = DIFFICULTY_ANCHORS.get(difficulty, {})
            chosen_anchors.extend(di.get("anchors", []))
            role_key = "student" if is_local else "teacher"
            if is_blocker:
                role_key = "faculty"
            ra = ROLE_ANCHORS.get(role_key, {})
            chosen_anchors.extend(ra.get("anchors", []))
            if chosen_anchors:
                get_decision_log().log(
                    DecisionType.ANCHOR_CHOSEN,
                    agent=force_agent or "unknown",
                    context={"domain": domain, "difficulty": difficulty, "is_local": is_local},
                    choice={"anchors": chosen_anchors, "strategy": "semantic_anchors"},
                    expected=f"Anchors should improve {domain} output quality",
                )
        else:
            system_prompt = SYSTEM_PROMPTS.get(domain, DEFAULT_SYSTEM_PROMPT)
            get_decision_log().log(
                DecisionType.STRATEGY_SELECTED,
                agent=force_agent or "unknown",
                context={"domain": domain, "difficulty": difficulty},
                choice={"strategy": "baseline", "prompt_only": True},
                expected="Baseline prompt without anchors",
            )

    # Inject vault context into system prompt
    context_blob = enrich_prompt(domain, prompt)
    if context_blob:
        system_prompt = system_prompt + context_blob
        # Log that context was retrieved (the "library" visit)
        get_decision_log().log(
            DecisionType.CONTEXT_RETRIEVED,
            agent=force_agent or "unknown",
            context={"domain": domain, "prompt_length": len(prompt)},
            choice={"context_injected": True, "context_length": len(context_blob)},
            expected="Vault context should improve response quality and relevance",
        )

    # Auto-check cloud availability on first call
    check_cloud_and_set_mode()

    # Auto-sleep check: if session timed out, trigger sleep before new task
    if session_id is not None and _should_auto_sleep(session_id):
        sys.stderr.write(f"[director] Auto-sleep: session {session_id} timed out ({SLEEP_TIMEOUT_MINUTES}min)\n")
        sleep(
            session_id=session_id,
            agent=store.list_agents()[0] if store.list_agents() else "unknown",
            store=store,
        )

    # Build candidate list sorted by score descending
    if force_agent:
        if force_agent not in store.list_agents():
            raise ValueError(f"Unknown agent '{force_agent}'")
        candidates = [force_agent]
    else:
        if difficulty == "blocker":
            eligible = store.qualifying_agents(domain, "diploma")
            if not eligible:
                sys.stderr.write("[director] Blocker — no diploma agent. Falling through to A2A.\n")
                candidates = []
            else:
                candidates = ["owl-alpha"]
        else:
            if difficulty not in GATES:
                raise ValueError(f"Invalid difficulty '{difficulty}'")
            eligible = store.qualifying_agents(domain, difficulty)
            if not eligible:
                return {"status": "blocked", "domain": domain, "difficulty": difficulty}
            candidates = sorted(eligible, key=lambda a: store.get_score(a, domain), reverse=True)

    # In degraded mode, filter to local-only agents
    candidates = _filter_local_candidates(candidates)

    if not force_agent and candidates:
        threshold = _get_threshold(domain, difficulty)
        approved = []
        for cand in candidates:
            confidence = _check_readiness(cand, domain, domain, prompt)
            if confidence >= threshold:
                approved.append(cand)
            else:
                _escalation_log.log(
                    agent=cand, domain=domain, difficulty=difficulty,
                    confidence=confidence, threshold=threshold,
                    escalated_to="next_candidate",
                )
        candidates = approved

    last_error = None

    def _try_agent(agent_name: str) -> Optional[dict]:
        nonlocal last_error
        try:
            response = call_model(agent_name, prompt, system_prompt=system_prompt)
            error = None
        except Exception as e:
            response = ""
            error = str(e)

        old_score = store.get_score(agent_name, domain)
        new_score = None

        if error:
            new_score = store.update_score(agent_name, domain, 0.0)

        traj_path = capture_trajectory(
            domain=domain,
            difficulty=difficulty,
            agent=agent_name,
            prompt=prompt,
            system_prompt=system_prompt,
            response=response,
            task_score=0.0 if error else None,
            old_score=old_score,
            new_score=new_score,
            error=error,
        )

        if error:
            last_error = error
            return None

        return {
            "status": "success",
            "domain": domain,
            "difficulty": difficulty,
            "agent": agent_name,
            "prompt": prompt,
            "response": response,
            "error": None,
            "old_score": old_score,
            "new_score": new_score,
            "trajectory": traj_path,
        }

    for agent in candidates:
        act = get_log().start_task(
            agent=agent, domain=domain, difficulty=difficulty,
            role=_agent_role(agent, store.get_score(agent, domain)),
            prompt_preview=prompt[:80],
        )
        result = _try_agent(agent)
        if result:
            result["degraded"] = _degraded_mode
            # Log completion
            if result.get("new_score") is not None:
                old_gate = store.gate_for_score(result.get("old_score", 0))
                new_gate = store.gate_for_score(result["new_score"])
                crossed = new_gate if old_gate != new_gate else None
                get_log().finish_task(
                    agent=agent, domain=domain,
                    score=result["new_score"], success=True,
                    gate_crossed=crossed,
                )
                if crossed:
                    get_log().gate_cross(
                        agent=agent, domain=domain,
                        from_gate=old_gate, to_gate=new_gate,
                        score=result["new_score"],
                    )
            return result

    # All normal candidates failed — try A2A as the last resort
    if "openhands" in COMBO_MAP:
        sys.stderr.write("[director] A2A fallback: trying openhands (antigravity)\n")
        result = _try_agent("openhands")
        if result:
            result["escalation"] = True
            result["degraded"] = _degraded_mode
            return result

    # Log the failure
    failed_agent = candidates[-1] if candidates else (force_agent or "unknown")
    get_log().task_error(agent=failed_agent, domain=domain, error=last_error or "all candidates failed")
    return {
        "status": "error",
        "domain": domain,
        "difficulty": difficulty,
        "agent": failed_agent,
        "prompt": prompt,
        "response": "",
        "error": last_error or "all candidates failed",
        "old_score": None,
        "new_score": None,
        "trajectory": None,
        "degraded": _degraded_mode,
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
