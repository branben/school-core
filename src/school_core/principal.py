"""principal.py — Principal orchestration: dispatch, routing, doubt cycle.

Holds the Principal's domain-to-role mapping, agent resolution, issue-ref
parsing, and the Rank-3/Rank-4 dispatch entrypoint (doubt cycle + CE router)
that the sync/async loops call per round.
"""

from __future__ import annotations

import re
from typing import Optional

from director import evaluate_and_update
from principal_doubt import run_doubt_cycle
from scripts.ce_router import classify_task, route_decision

from school_core.soul import load_soul

# Map domain -> role for dispatch. Roles MUST exist in executor.COMBO_MAP
# (searcher, executor, reviewer, browser, coder, openhands, a2a-agent);
# "student"/"tester"/"debugger" are NOT valid dispatch roles, so unknown
# domains fall back to "coder" (the universal code role) rather than a
# nonexistent agent.
DOMAIN_ROLE = {
    "code-search": "searcher",
    "terminal": "executor",
    "code-review": "reviewer",
    "web-automation": "browser",
    "python-coding": "coder",
    "python-testing": "coder",
    "debugging": "coder",
    "code-implementation": "coder",
    "_default": "coder",
}


# Model aliases that map to roles in COMBO_MAP. When --agent passes a model
# name (e.g. "foundry-coder-7b"), resolve to the role (e.g. "coder") so the
# score-store gate check and role dispatch work correctly. Without this, the
# conductor treats the model name as a role and rejects it as "Unknown role".
# Populated lazily from executor.COMBO_MAP to avoid import cycles at module
# load time (executor imports conductor for callback hooks).
AGENT_TO_ROLE_CACHE: Optional[dict] = None


def _agent_to_role(agent: str) -> Optional[str]:
    """Resolve a --agent value to a valid role name.

    Accepts both role names (coder, searcher, etc.) and model aliases
    (foundry-coder-7b, north-coding, auto/best-free). Returns None if the
    agent is not recognized.
    """
    global AGENT_TO_ROLE_CACHE
    if AGENT_TO_ROLE_CACHE is None:
        AGENT_TO_ROLE_CACHE = {}
        try:
            from executor import COMBO_MAP
            for role, entry in COMBO_MAP.items():
                # COMBO_MAP values are dicts with "default" model or lists
                if isinstance(entry, dict):
                    model = entry.get("default") or entry.get("_default")
                    if model:
                        AGENT_TO_ROLE_CACHE[model] = role
                elif isinstance(entry, str):
                    AGENT_TO_ROLE_CACHE[entry] = role
                elif isinstance(entry, list):
                    for m in entry:
                        if isinstance(m, str):
                            AGENT_TO_ROLE_CACHE[m] = role
        except Exception:
            pass
    return AGENT_TO_ROLE_CACHE.get(agent)


def _resolve_agent(args) -> str:
    """Resolve args.agent to a dispatchable role.

    Priority: explicit role match -> model alias -> domain default.
    """
    import logging
    logger = logging.getLogger(__name__)

    if args.agent:
        # Direct role match (coder, searcher, executor, reviewer, browser)
        try:
            from executor import COMBO_MAP
            if args.agent in COMBO_MAP:
                return args.agent
        except Exception:
            pass
        # Model alias resolution (foundry-coder-7b -> coder)
        resolved = _agent_to_role(args.agent)
        if resolved is not None:
            return resolved
        # If it looks like a model name but isn't in COMBO_MAP, treat as
        # the domain's default role and let the executor route the model.
        logger.warning(
            "Agent %r not found in COMBO_MAP — treating as model name "
            "for the %s role", args.agent, DOMAIN_ROLE.get(args.domain, "coder")
        )
        return DOMAIN_ROLE.get(args.domain, "coder")
    return DOMAIN_ROLE.get(args.domain, "student")


def load_principal_soul() -> str:
    """Load the Principal's SOUL.md (all 8 personas are now live).

    Mirrors the 3-line read pattern in teacher.py / leaf.py. Falls back to a
    minimal system prompt if the profile file is absent.
    """
    return load_soul("principal")


def _parse_issue_ref(ref: str) -> tuple[str, str, int]:
    """Parse a GitHub issue reference into (owner, repo, number).

    Accepts either ``owner/repo#123`` or a full GitHub issue URL
    (https://github.com/owner/repo/issues/123). Raises ValueError if
    the reference can't be parsed.
    """
    ref = ref.strip()
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", ref)
    if not m:
        m = re.search(r"([^/\\s]+)/([^/#\\s]+)#(\\d+)", ref)
    if not m:
        raise ValueError(
            f"Could not parse issue ref {ref!r}. Expected 'owner/repo#123' "
            f"or a full GitHub issue URL."
        )
    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    return owner, repo, number


def _principal_dispatch(
    task: str,
    role: str,
    domain: str,
    difficulty: str,
    store,
    repo: str,
    doubt_enabled: bool = False,
    doubt_fn=None,
    override_reason: Optional[str] = None,
    task_shape: Optional[dict] = None,
    skip_readiness: bool = False,
    repo_path: Optional[Path] = None,
    strict_route_persistence: bool = False,
) -> dict:
    """Rank 3 + Rank 4 — Principal dispatch with DDD doubt cycle and CE router.

    Runs the DDD doubt cycle on the routing decision (gate/role/model) BEFORE
    committing to dispatch. If doubt is enabled and finds an issue, the chosen
    gate is down-shifted (reconcile) and the dispatch uses the reconciled gate.
    The ``doubt_log`` is attached to the returned result dict for traceability.

    Rank 4: the CE router maps the task shape to a Layer B skill (rank) and
    logs ``chosen_skill`` to the bookbag for traceability. The router is
    deterministic and offline — it never blocks dispatch.

    When ``doubt_enabled`` is False (default) the doubt cycle is skipped (no
    ``doubt_log`` key). The router always runs (it is cheap) unless a
    ``task_shape`` is provided that maps to a skill, in which case its choice
    is recorded regardless.
    """
    from pathlib import Path

    from bookbag import BookbagSignal
    from leaf import run_leaf

    from school_core.conductor.dispatch import _persist_issue_route

    gate = difficulty
    if doubt_enabled:
        claim = (
            f"Routing task to {role} ({domain}) via gate {gate} "
            f"through OmniRoute / Orca student leaf."
        )
        extract = {
            "task": task,
            "role": role,
            "domain": domain,
            "gate": gate,
            "model": "omni-route/default",
            "lens": "principal",
        }
        doubt_log = run_doubt_cycle(
            claim=claim,
            extract=extract,
            doubt_fn=doubt_fn,
            max_cycles=1,
            override_reason=override_reason,
        )
        # RECONCILE: if doubt down-shifted the gate, dispatch with the softer gate.
        reconciled_gate = doubt_log["extract"].get("gate", gate)
    else:
        doubt_log = None
        reconciled_gate = gate

    # Rank 4: choose the Layer B skill from the task shape. Default a fresh
    # student task to "new implementation" when the caller doesn't supply one.
    if task_shape is None:
        task_shape = {
            "has_failed_gate": False,
            "is_new_implementation": True,
            "requires_architectural_routing": False,
            "complexity": 1,
            "is_spec_gap": False,
        }
    routing = route_decision(task_shape, bead=None, repo=repo)

    result = run_leaf(
        task_prompt=task, role=role, domain=domain,
        difficulty=reconciled_gate, store=store, repo=repo,
        # CE mode is for complex spec-gated tasks only — direct code-
        # implementation tasks need the student to write actual code,
        # not markdown artifacts in docs/solutions/.
        ce_enabled=routing["chosen_skill"] == "rank5_student_plan",
        complex_task=(routing["chosen_skill"] == "rank5_student_plan"),
        skip_readiness=skip_readiness,
        repo_path=repo_path,
        signal_ready=False,
    )

    # A failed leaf must never become a ready teacher handoff. Keep the
    # route metadata on the returned result for diagnostics, but leave route
    # persistence and signaling to the success path below.
    result["chosen_skill"] = routing["chosen_skill"]
    result["primary_workflow"] = routing["primary_workflow"]
    result["overlays"] = routing["overlays"]
    result["discarded_overlays"] = routing["discarded_overlays"]
    result["curiosity_required"] = routing["curiosity_required"]
    result["human_gate_required"] = routing["human_gate_required"]
    if result.get("status") != "success":
        return result
    if doubt_log is not None:
        result["doubt_log"] = doubt_log
    # Persist the full route contract and signal only after it is durable.
    bead = result.get("bead")
    if bead:
        routing = _persist_issue_route(
            bead,
            repo,
            task_shape,
            strict=strict_route_persistence,
        )
        result.update({
            "chosen_skill": routing["chosen_skill"],
            "primary_workflow": routing["primary_workflow"],
            "overlays": routing["overlays"],
            "discarded_overlays": routing["discarded_overlays"],
            "curiosity_required": routing["curiosity_required"],
            "human_gate_required": routing["human_gate_required"],
        })
        BookbagSignal(bead, repo=repo).ready()
    return result
