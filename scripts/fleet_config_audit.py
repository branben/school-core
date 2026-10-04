#!/usr/bin/env python3
"""Fleet configuration audit for the School Core Hermes profiles.

Catches drift that a health check cannot see. The 2026-10-01 outage is the
motivating case: 9 agents were ERRORING on a retired model, but one more agent
(`teacher-coo`) was pinned to the SAME dead model and reported `idle`. An agent
that is idle on a dead model is indistinguishable from a healthy one until it
is dispatched. Fleet `status` reports absence of evidence, not evidence of
health.

Three checks, all read-only:

  1. RETIRED MODELS - any profile pinned to a model whose free tier ended.
     These fail on first dispatch, not on inspection.
  2. HERMES/PAPERCLIP DRIFT - the two systems must name the same model. They are
     configured independently, so they drift silently.
  3. UNKNOWN TOOLSETS - a toolset declared in `platform_toolsets` with no
     matching entry in `mcp_servers`. Emits `Unknown toolsets: X` at runtime and
     silently drops the tool.

Exit codes: 0 clean, 1 problems found (prints them), 2 could not audit.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import yaml

PAPERCLIP = os.environ.get("PAPERCLIP_URL", "http://localhost:3100")
PROFILES = os.path.expanduser("~/.hermes/profiles")

# Free tiers that ended. A model here fails on dispatch, not on inspection.
# Keep this list current -- it is the only reason this check catches anything.
RETIRED = {
    "upstage/solar-pro4",
    "upstage/solar-pro4:free",
}


def load(path: str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def paperclip_models() -> dict[str, str]:
    """agent name -> Paperclip adapterConfig model.

    Built-in toolsets (terminal, file, vision, ...) legitimately have no entry in
    mcp_servers, so only MCP-named toolsets are compared.
    """
    with urllib.request.urlopen(f"{PAPERCLIP}/api/companies", timeout=20) as r:
        companies = json.load(r)
    matches = [c for c in companies if c.get("name") == "School Core"]
    if not matches:
        raise RuntimeError("School Core company not found")
    cid = matches[0]["id"]
    with urllib.request.urlopen(f"{PAPERCLIP}/api/companies/{cid}/agents", timeout=20) as r:
        agents = json.load(r)
    return {
        a["name"]: (a.get("adapterConfig") or {}).get("model") or "" for a in agents
    }


def main() -> int:
    try:
        pc = paperclip_models()
    except (urllib.error.URLError, OSError, RuntimeError, KeyError) as exc:
        print(f"could not reach Paperclip at {PAPERCLIP}: {exc}", file=sys.stderr)
        return 2

    profiles = sorted(p for p in os.listdir(PROFILES) if os.path.isdir(os.path.join(PROFILES, p)))
    problems: list[str] = []

    for name in profiles:
        cfg_path = os.path.join(PROFILES, name, "config.yaml")
        if not os.path.exists(cfg_path):
            continue
        cfg = load(cfg_path)
        model = (cfg.get("model") or {}).get("default")
        servers = cfg.get("mcp_servers") or {}

        # 1. retired free tiers
        if model and model.split(":")[0] in RETIRED or model in RETIRED:
            problems.append(
                f"{name}: pinned to RETIRED model {model!r} -- fails on first dispatch"
            )

        # 2. drift against Paperclip
        if name in pc and model != pc[name]:
            problems.append(
                f"{name}: Hermes={model!r} but Paperclip={pc[name]!r} -- the two will dispatch different models"
            )

        # 3. declared toolsets with no server behind them
        cli = (cfg.get("platform_toolsets") or {}).get("cli") or []
        known = set(servers) | {
            "hermes-cli", "a2a", "browser", "clarify", "code_execution",
            "computer_use", "connections", "cronjob", "delegation", "file",
            "image_gen", "kanban", "memory", "session_search", "skills",
            "terminal", "todo", "tts", "vision", "web", "x_search",
        }
        for toolset in cli:
            if toolset not in known:
                problems.append(
                    f"{name}: toolset {toolset!r} declared but absent from mcp_servers -- emits 'Unknown toolsets' and silently drops the tool"
                )

    if problems:
        print(f"FAIL - {len(problems)} problem(s) across {len(profiles)} profiles:\n")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK - {len(profiles)} profiles: no retired models, no Hermes/Paperclip drift, no unknown toolsets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
