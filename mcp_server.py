#!/usr/bin/env python3
"""
Agent School MCP Server — stdio JSON-RPC for Hermes integration.

Protocol: MCP (Model Context Protocol) over stdin/stdout.
No external dependencies — pure Python 3 stdlib.

Register in ~/.hermes/config.yaml:
    mcp_servers:
      agent-school:
        command: python3
        args: ["/path/to/school-core/mcp_server.py"]
        enabled: true

Tools:
  school_route          — Route a task to best-qualified model (no execution)
  school_execute        — Execute a prompt against a specific agent
  school_evaluate       — Submit task evaluation to update scores
  school_run            — Route + execute + auto-evaluate (convenience)
  school_list_agents    — List all agents with scores and gates
  school_list_domains   — List all known domains and gate counts
  school_get_trajectory — Read a saved trajectory by file path
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Imports from school-core — each guarded so partial failures are visible
# ---------------------------------------------------------------------------

try:
    from scoring import ScoreStore, GATES
except ImportError as e:
    sys.stderr.write(f"[mcp_server] FATAL: cannot import scoring — {e}\n")
    sys.exit(1)

try:
    from routing import route_task, RouteResult
except ImportError as e:
    sys.stderr.write(f"[mcp_server] FATAL: cannot import routing — {e}\n")
    sys.exit(1)

try:
    from director import _resolve_repo_path, run_task, evaluate_and_update
except ImportError as e:
    sys.stderr.write(f"[mcp_server] FATAL: cannot import director — {e}\n")
    sys.exit(1)

try:
    from executor import COMBO_MAP
except ImportError as e:
    sys.stderr.write(f"[mcp_server] FATAL: cannot import executor — {e}\n")
    sys.exit(1)

try:
    from context_orchestrator import DEFAULT_VAULT, enrich_prompt
except ImportError:
    enrich_prompt = None  # degraded — no vault context injection
    DEFAULT_VAULT = None

try:
    from trajectory import capture_trajectory
except ImportError as e:
    capture_trajectory = None  # degraded — no trajectory persistence

try:
    from engram_adapter import engram_available
except ImportError:
    engram_available = lambda: False  # degraded — no Engram

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

store = ScoreStore()
_log = logging.getLogger("mcp_server")
_log.setLevel(logging.WARNING)

MCP_VERSION = "2025-03-26"
SERVER_NAME = "agent-school"
SERVER_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "school_route",
        "description": (
            "Route a task to the best-qualified model. Does NOT execute — only returns "
            "the routing decision. Use when you want to inspect which model would be "
            "chosen before running, or to check if a task is blocked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Task domain. Use school_list_domains to see all domains.",
                },
                "difficulty": {
                    "type": "string",
                    "enum": ["easy", "medium", "hard", "blocker"],
                    "description": "Difficulty gate: easy=0, medium=25, hard=50, blocker=75.",
                },
                "force_agent": {
                    "type": "string",
                    "description": "Optional: bypass routing and force a specific agent.",
                },
            },
            "required": ["domain", "difficulty"],
        },
    },
    {
        "name": "school_execute",
        "description": (
            "Execute a prompt against a specific agent/model. Routes to the correct "
            "backend (OmniRoute, Foundry, Ollama, or A2A) automatically. "
            "Use school_list_agents or school_route first to choose the agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent name from school_list_agents.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task prompt to send to the model.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain for context injection (enriches prompt from vault + trajectories).",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Optional system prompt override. Defaults to domain-specific prompt.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional timeout in seconds. Defaults to model-appropriate value.",
                },
                "repo": {
                    "type": "string",
                    "description": "Optional owner/repo slug (e.g. 'branben/school-core') for Serena LSP symbol enrichment. Uses cached clone.",
                },
            },
            "required": ["agent", "prompt"],
        },
    },
    {
        "name": "school_evaluate",
        "description": (
            "Submit task evaluation to update agent scores. Scores use EMA smoothing "
            "(70% old + 30% new). Failure auto-scores as 0. Gate crossings are detected "
            "and returned. Score updates persist and affect future routing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent that executed the task.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain the task was in.",
                },
                "task_score": {
                    "type": "number",
                    "description": "0-100. Common values: 0=fail, 40=partial, 70=success, 100=perfect.",
                },
                "evaluation": {
                    "type": "string",
                    "description": "Optional human-readable evaluation notes.",
                },
                "trajectory_path": {
                    "type": "string",
                    "description": "Path to trajectory file from school_execute for enrichment.",
                },
            },
            "required": ["agent", "domain", "task_score"],
        },
    },
    {
        "name": "school_run",
        "description": (
            "Route + execute + auto-evaluate in one call. Picks the best-qualified agent "
            "for the domain and difficulty, runs the prompt, and auto-scores: failure=0, "
            "success=70. Trajectory is captured and synced to Engram. "
            "Use this for the common case. Use the primitive tools for manual control."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Task domain. Use school_list_domains to see all domains.",
                },
                "difficulty": {
                    "type": "string",
                    "enum": ["easy", "medium", "hard", "blocker"],
                    "description": "Difficulty gate: easy=0, medium=25, hard=50, blocker=75.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task prompt to send to the model.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Optional system prompt override. Defaults to domain-specific prompt.",
                },
            },
            "required": ["domain", "difficulty", "prompt"],
        },
    },
    {
        "name": "school_list_agents",
        "description": "List all registered agents with their current scores and gate levels across all domains.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "school_list_domains",
        "description": "List all known task domains and how many agents qualify for each gate.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "school_get_trajectory",
        "description": "Read a saved trajectory by its file path. Returns full task trace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to trajectory JSON file.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "school_get_leaderboard",
        "description": "Get the score leaderboard for a specific domain, sorted descending.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Domain. Defaults to '_default'.",
                },
                "limit": {
                    "type": "number",
                    "description": "Max number of results. Defaults to all.",
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _handle_school_route(args: dict) -> dict:
    domain = args["domain"]
    difficulty = args["difficulty"]
    force = args.get("force_agent")

    try:
        result = route_task(store, domain, difficulty, force)
    except ValueError as e:
        return {"error": str(e), "blocked": True}

    chosen_score = result.score
    gate_crossed = None
    if chosen_score is not None:
        gate_crossed = store.gate_for_score(chosen_score)

    return {
        "chosen_agent": result.chosen_agent,
        "score": chosen_score,
        "eligible_count": result.eligible_count,
        "blocked": result.blocked,
        "escalation": result.escalation,
        "gate_crossed": gate_crossed,
        "domain": domain,
        "difficulty": difficulty,
    }


def _handle_school_execute(args: dict) -> dict:
    agent_name = args["agent"]
    prompt = args["prompt"]
    domain = args.get("domain", "_default")
    system_prompt = args.get("system_prompt")
    timeout = args.get("timeout")

    # System prompt: use domain default if none given
    from director import SYSTEM_PROMPTS, DEFAULT_SYSTEM_PROMPT

    if system_prompt is None:
        system_prompt = SYSTEM_PROMPTS.get(domain, DEFAULT_SYSTEM_PROMPT)

    # Context injection from vault + trajectories + Serena LSP symbols.
    # Resolve repo_path for Serena symbol enrichment when available.
    if enrich_prompt is not None:
        repo_path = _resolve_repo_path_for_mcp(args)
        context_blob = enrich_prompt(domain, prompt, vault_path=DEFAULT_VAULT, repo_path=repo_path)
        if context_blob:
            system_prompt = system_prompt + context_blob

    # Call the model
    from executor import call_model, ExecutorError

    try:
        response = call_model(agent_name, prompt, system_prompt=system_prompt, timeout=timeout)
        error = None
    except ExecutorError as e:
        response = ""
        error = str(e)
    except Exception as e:
        response = ""
        error = f"Unexpected error: {e}"

    # Determine backend from COMBO_MAP
    combo = COMBO_MAP.get(agent_name, "unknown")
    if combo.startswith("foundry/"):
        backend = "Foundry"
    elif combo.startswith("a2a/"):
        backend = "A2A"
    else:
        backend = "OmniRoute"

    # Capture trajectory
    trajectory_path = None
    if capture_trajectory is not None:
        try:
            trajectory_path = capture_trajectory(
                domain=domain,
                difficulty="manual",
                agent=agent_name,
                prompt=prompt,
                system_prompt=system_prompt,
                response=response,
                task_score=None,
                error=error,
            )
        except Exception as e:
            _log.warning(f"trajectory capture failed: {e}")

    return {
        "agent": agent_name,
        "response": response,
        "response_length": len(response),
        "backend": backend,
        "trajectory_path": trajectory_path,
        "error": error,
        "has_error": error is not None,
    }


def _handle_school_evaluate(args: dict) -> dict:
    agent = args["agent"]
    domain = args["domain"]
    task_score = float(args["task_score"])
    evaluation = args.get("evaluation")
    trajectory_path = args.get("trajectory_path")

    # Build a mock result for evaluate_and_update
    mock_result = {
        "agent": agent,
        "domain": domain,
        "status": "success",
        "trajectory": trajectory_path,
    }

    old_score = store.get_score(agent, domain)
    updated = evaluate_and_update(mock_result, task_score, evaluation=evaluation, store=store)

    return {
        "agent": agent,
        "domain": domain,
        "old_score": updated["old_score"],
        "new_score": updated["new_score"],
        "delta": (updated["new_score"] or 0) - (updated["old_score"] or 0),
        "old_gate": store.gate_for_score(updated["old_score"]),
        "new_gate": store.gate_for_score(updated["new_score"]),
        "gate_crossed": updated.get("gate_crossed"),
    }


def _handle_school_run(args: dict) -> dict:
    domain = args["domain"]
    difficulty = args["difficulty"]
    prompt = args["prompt"]
    system_prompt = args.get("system_prompt")

    try:
        result = run_task(
            prompt=prompt,
            domain=domain,
            difficulty=difficulty,
            store=store,
            system_prompt=system_prompt,
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    if result["status"] == "blocked":
        return {
            "status": "blocked",
            "domain": domain,
            "difficulty": difficulty,
            "error": f"No agent qualifies for {difficulty} ({GATES.get(difficulty, '?')}) in '{domain}'",
        }

    # All candidates failed with no trajectory = hard failure
    if result["status"] == "error" and not result.get("trajectory"):
        return {
            "status": "error",
            "domain": domain,
            "difficulty": difficulty,
            "agent": result.get("agent"),
            "error": result.get("error", "All candidates failed"),
        }

    # Error with trajectory = auto-scored as failure already by director
    if result.get("error") and result.get("trajectory"):
        return {
            "status": "error",
            "agent": result.get("agent"),
            "response": result.get("response", ""),
            "response_length": len(result.get("response", "")),
            "domain": domain,
            "difficulty": difficulty,
            "old_score": result.get("old_score"),
            "new_score": result.get("new_score"),
            "delta": (result.get("new_score") or 0) - (result.get("old_score") or 0),
            "gate_crossed": result.get("gate_crossed"),
            "trajectory_path": result.get("trajectory"),
            "error": result.get("error"),
        }

    # Success — auto-evaluate with 70
    updated = evaluate_and_update(result, 70, store=store)

    return {
        "status": "success",
        "agent": updated.get("agent"),
        "response": updated.get("response", ""),
        "response_length": len(updated.get("response", "")),
        "domain": domain,
        "difficulty": difficulty,
        "old_score": updated.get("old_score"),
        "new_score": updated.get("new_score"),
        "delta": (updated.get("new_score") or 0) - (updated.get("old_score") or 0),
        "gate_crossed": updated.get("gate_crossed"),
        "trajectory_path": updated.get("trajectory"),
        "backend": _backend_for_agent(updated.get("agent", "")),
        "error": None,
    }


def _handle_school_list_agents(args: dict) -> dict:
    agents = []
    all_scores = store.get_all_scores()
    for name in sorted(all_scores.keys()):
        scores = all_scores[name]
        default_score = scores.get("_default", 0.0)
        agents.append({
            "name": name,
            "scores": {k: v for k, v in scores.items()},
            "default_score": default_score,
            "default_gate": store.gate_for_score(default_score),
        })
    return {"agents": agents, "count": len(agents)}


def _handle_school_list_domains(args: dict) -> dict:
    domains = store.domains()
    gate_counts = {}
    for gate_name in GATES:
        gate_counts[gate_name] = len(store.qualifying_agents("_default", gate_name))
    return {
        "domains": sorted(domains),
        "gates": {name: thr for name, thr in GATES.items()},
        "gate_counts": gate_counts,
    }


def _handle_school_get_trajectory(args: dict) -> dict:
    path = args["path"]
    p = Path(path)
    if not p.exists():
        return {"error": f"Trajectory not found: {path}"}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {"trajectory": data, "path": path}
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"Failed to read trajectory: {e}"}


def _handle_school_get_leaderboard(args: dict) -> dict:
    domain = args.get("domain", "_default")
    limit = args.get("limit")
    lb = store.leaderboard(domain)
    if limit:
        lb = lb[:limit]
    entries = []
    for rank, (agent, score) in enumerate(lb, 1):
        entries.append({
            "rank": rank,
            "agent": agent,
            "score": score,
            "gate": store.gate_for_score(score),
        })
    return {"domain": domain, "entries": entries, "count": len(entries)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_repo_path_for_mcp(args: dict) -> Optional[Path]:
    """Resolve a repo path from MCP tool arguments for Serena enrichment.

    Checks ``repo`` arg (owner/repo slug) via ``director._resolve_repo_path``.
    Returns ``None`` if no clone exists (Serena gracefully skips).
    """
    repo = args.get("repo", "")
    if not repo:
        return None
    return _resolve_repo_path(repo)


def _backend_for_agent(agent_name: str) -> str:
    combo = COMBO_MAP.get(agent_name, "unknown")
    if combo.startswith("foundry/"):
        return "Foundry"
    elif combo.startswith("a2a/"):
        return "A2A"
    return "OmniRoute"


# Map tool names to handlers
TOOL_HANDLERS: dict[str, Callable] = {
    "school_route": _handle_school_route,
    "school_execute": _handle_school_execute,
    "school_evaluate": _handle_school_evaluate,
    "school_run": _handle_school_run,
    "school_list_agents": _handle_school_list_agents,
    "school_list_domains": _handle_school_list_domains,
    "school_get_trajectory": _handle_school_get_trajectory,
    "school_get_leaderboard": _handle_school_get_leaderboard,
}

# ---------------------------------------------------------------------------
# MCP protocol — JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

_current_request_id: int | str | None = None


def _respond(result: Any, is_error: bool = False, wrap: bool = False):
    """Write a JSON-RPC response to stdout."""
    if wrap:
        payload = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2, default=str),
                }
            ],
            "isError": is_error,
        }
    else:
        payload = result

    response = {
        "jsonrpc": "2.0",
        "id": _current_request_id,
        "result": payload,
    }
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def _respond_error(code: int, message: str, data: Any = None):
    """Write a JSON-RPC error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    response = {
        "jsonrpc": "2.0",
        "id": _current_request_id,
        "error": error,
    }
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def _handle_request(msg: dict):
    global _current_request_id
    _current_request_id = msg.get("id")

    method = msg.get("method", "")
    params = msg.get("params", {}) or {}

    if method == "initialize":
        _respond({
            "protocolVersion": MCP_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })
        return

    if method == "notifications/initialized":
        # No response needed for notifications
        return

    if method == "tools/list":
        _respond({"tools": TOOL_DEFINITIONS})
        return

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if handler is None:
            _respond_error(-32601, f"Unknown tool: {tool_name}")
            return

        try:
            result = handler(arguments)
            is_error = result.get("has_error", False) or "error" in result
            _respond(result, is_error=is_error, wrap=True)
        except Exception as e:
            tb = traceback.format_exc()
            _log.error(f"Handler error: {e}\n{tb}")
            _respond_error(-32603, str(e), data={"traceback": tb})
        return

    # Unknown method
    _respond_error(-32601, f"Unknown method: {method}")


def main():
    """Read JSON-RPC requests from stdin and dispatch."""
    # Signal readiness to stderr (not stdout — that's protocol)
    sys.stderr.write(f"[mcp_server] Agent School MCP v{SERVER_VERSION} ready\n")
    sys.stderr.write(f"[mcp_server] {len(store.list_agents())} agents, {len(store.domains())} domains, {len(TOOL_HANDLERS)} tools\n")
    if engram_available():
        sys.stderr.write("[mcp_server] Engram: connected\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            # Can't respond without a valid ID — just log
            sys.stderr.write(f"[mcp_server] Invalid JSON-RPC: {e}\n  raw: {line[:200]}\n")
            sys.stderr.flush()
            continue

        try:
            _handle_request(msg)
        except Exception as e:
            sys.stderr.write(f"[mcp_server] Fatal handler error: {e}\n")
            sys.stderr.flush()
            tb = traceback.format_exc()
            _respond_error(-32603, str(e), data={"traceback": tb})


if __name__ == "__main__":
    main()
