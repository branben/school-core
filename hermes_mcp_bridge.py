#!/usr/bin/env python3
"""
hermes_mcp_bridge.py — MCP server bridging OpenHands to Hermes Bot Mode profiles.

Wraps `hermes -p <bot> chat -Q -q <prompt>` so OpenHands Agent Canvas can
call your Hermes staff (phymora, scribe, student-coder, teacher-cto, etc.)
as tools.

Protocol: MCP (Model Context Protocol) over stdio — zero dependencies.

# Register in OpenHands Agent Canvas settings:
#   MCP Servers → Add → name: hermes-staff, command: python3,
#   args: ["/absolute/path/to/hermes_mcp_bridge.py"]
#
# Or ~/.openhands/mcp_servers.json:
#   { "hermes-stuff": { "command": "python3",
#     "args": ["/absolute/path/to/hermes_mcp_bridge.py"],
#     "transport": "stdio" } }

Tools:
  hermes_list_profiles   — List all available Hermes profiles
  hermes_mention         — Send a message to a specific profile (one-shot)
  hermes_staff           — Send to a role-based staff member (convenience)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HERMES_PROFILES_DIR = os.path.expanduser("~/.hermes/profiles")
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")

# Staff roster — role -> profile name. Maps the dispatch table users already know.
STAFF_ROSTER: dict[str, str] = {
    # Adversarial / review
    "phymora": "phymora",
    "student-reviewer": "student-reviewer",
    "teacher-cto": "teacher-cto",
    "teacher-coo": "teacher-coo",
    "mentor": "mentor",
    # Architecture / docs
    "scribe": "scribe",
    "student-searcher": "student-searcher",
    "principal": "principal",
    # Implementation
    "student-coder": "student-coder",
    "student-executor": "student-executor",
    "student-whymage": "student-whymage",
    "student-ci": "student-ci",
    "student-browser": "student-browser",
    # Personal
    "lucas": "lucas",
    "sentinel": "sentinel",
}

# Timeout per call (seconds). Hermes can be slow on first response.
DEFAULT_TIMEOUT = int(os.environ.get("HERMES_BRIDGE_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# MCP protocol helpers
# ---------------------------------------------------------------------------

MCP_VERSION = "2025-03-26"
SERVER_NAME = "hermes-staff"
SERVER_VERSION = "0.1.0"

_current_request_id: Any = None


def _build_response(request_id: Any, result: dict, is_error: bool = False, wrap: bool = False) -> str:
    """Build a JSON-RPC success response string."""
    payload = result
    if wrap:
        text = json.dumps(result, ensure_ascii=False)
        payload = {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }
    return json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": payload,
    })


def _build_error_response(request_id: Any, code: int, message: str, data: Any = None) -> str:
    """Build a JSON-RPC error response string."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    })


# ---------------------------------------------------------------------------
# Hermes transport
# ---------------------------------------------------------------------------


def _hermes_binary() -> str:
    """Resolve the hermes CLI binary."""
    from shutil import which

    # 1. Check HERMES_BIN env var first (explicit override, launchd-friendly)
    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    # 2. Check PATH
    path = which("hermes")
    if path:
        return path

    # 3. Check hardcoded candidates
    candidates = [
        os.path.expanduser("~/.local/bin/hermes"),
        os.path.expanduser("~/.hermes/bin/hermes"),
        "/usr/local/bin/hermes",
        "/opt/homebrew/bin/hermes",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c

    raise RuntimeError(
        "hermes CLI not found. Set HERMES_BIN env var to the absolute path."
    )


def list_profiles() -> list[str]:
    """List available Hermes profiles."""
    if not os.path.isdir(HERMES_PROFILES_DIR):
        return []
    return sorted(
        d
        for d in os.listdir(HERMES_PROFILES_DIR)
        if os.path.isdir(os.path.join(HERMES_PROFILES_DIR, d))
    )


def profile_exists(name: str) -> bool:
    return os.path.isdir(os.path.join(HERMES_PROFILES_DIR, name))


def call_profile(
    profile: str,
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    conversation: str | None = None,
) -> dict:
    """Execute a one-shot chat against a Hermes profile."""
    if not profile_exists(profile):
        return {
            "success": False,
            "response": "",
            "error": f"Profile '{profile}' not found in {HERMES_PROFILES_DIR}",
            "profile": profile,
        }

    cmd = [_hermes_binary(), "-p", profile, "chat", "-Q", "-q", prompt]
    if conversation:
        cmd.extend(["-c", conversation])

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HERMES_QUIET": "1"},
        )
        duration = time.monotonic() - start
        if result.returncode == 0:
            return {
                "success": True,
                "response": result.stdout.strip(),
                "profile": profile,
                "error": None,
                "duration_seconds": round(duration, 2),
            }
        else:
            return {
                "success": False,
                "response": "",
                "profile": profile,
                "error": result.stderr.strip()[:1000] or f"exit code {result.returncode}",
                "duration_seconds": round(duration, 2),
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "response": "",
            "profile": profile,
            "error": f"timeout after {timeout}s",
            "duration_seconds": float(timeout),
        }
    except Exception as e:
        return {
            "success": False,
            "response": "",
            "profile": profile,
            "error": str(e),
            "duration_seconds": 0.0,
        }


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "hermes_list_profiles",
        "description": (
            "List all available Hermes bot profiles that OpenHands can call. "
            "Use this to see which staff members are online before dispatching."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "hermes_mention",
        "description": (
            "Send a one-shot message to a specific Hermes profile and get its response. "
            "The profile runs its full SOUL.md, memory, and skills. "
            "Use for escalating tasks to a specific agent (e.g., ask phymora for "
            "adversarial review, ask scribe for architecture notes)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Profile name (use hermes_list_profiles to see all).",
                },
                "prompt": {
                    "type": "string",
                    "description": "The message/task to send to the profile.",
                },
                "timeout": {
                    "type": "number",
                    "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT}).",
                },
            },
            "required": ["profile", "prompt"],
        },
    },
    {
        "name": "hermes_staff",
        "description": (
            "Send a task to a role-based staff member using the school-core roster. "
            "Convenience wrapper around hermes_mention — maps role names to profiles. "
            "Roles: phymora (adversarial), scribe (architecture/docs), "
            "student-coder (implementation), teacher-cto (technical review), "
            "teacher-coo (acceptance review), mentor (guidance), principal (routing)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": list(STAFF_ROSTER.keys()),
                    "description": "Staff role to dispatch to.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The message/task to send.",
                },
                "timeout": {
                    "type": "number",
                    "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT}).",
                },
            },
            "required": ["role", "prompt"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _handle_hermes_list_profiles(args: dict) -> dict:
    profiles = list_profiles()
    staff = {role: profile for role, profile in STAFF_ROSTER.items() if profile in profiles}
    return {
        "profiles": profiles,
        "staff": staff,
        "count": len(profiles),
        "has_error": False,
    }


def _handle_hermes_mention(args: dict) -> dict:
    profile = args["profile"]
    prompt = args["prompt"]
    timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
    result = call_profile(profile, prompt, timeout=timeout)
    result["has_error"] = not result["success"]
    return result


def _handle_hermes_staff(args: dict) -> dict:
    role = args["role"]
    prompt = args["prompt"]
    timeout = int(args.get("timeout", DEFAULT_TIMEOUT))

    profile = STAFF_ROSTER.get(role)
    if profile is None:
        return {
            "success": False,
            "response": "",
            "profile": None,
            "error": f"Unknown role '{role}'. Available: {list(STAFF_ROSTER.keys())}",
            "has_error": True,
        }

    result = call_profile(profile, prompt, timeout=timeout)
    result["role"] = role
    result["has_error"] = not result["success"]
    return result


TOOL_HANDLERS: dict[str, Any] = {
    "hermes_list_profiles": _handle_hermes_list_profiles,
    "hermes_mention": _handle_hermes_mention,
    "hermes_staff": _handle_hermes_staff,
}

# ---------------------------------------------------------------------------
# Request dispatch
# ---------------------------------------------------------------------------


def _handle_request(msg: dict) -> str | None:
    """Dispatch a JSON-RPC request. Returns the response JSON string, or None for notifications."""
    request_id = msg.get("id")

    method = msg.get("method", "")
    params = msg.get("params", {}) or {}

    if method == "initialize":
        return _build_response(request_id, {
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _build_response(request_id, {"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return _build_error_response(request_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result = handler(arguments)
            is_error = result.get("has_error", False)
            return _build_response(request_id, result, is_error=is_error, wrap=True)
        except Exception as e:
            tb = traceback.format_exc()
            sys.stderr.write(f"[hermes_bridge] Handler error: {e}\n{tb}\n")
            sys.stderr.flush()
            return _build_error_response(request_id, -32603, str(e), data={"traceback": tb})

    return _build_error_response(request_id, -32601, f"Unknown method: {method}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes MCP Bridge")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run as HTTP server (for cloud OpenHands agents)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port for HTTP mode (default: 8787)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind (default: 0.0.0.0 = all interfaces including Tailscale)",
    )
    args = parser.parse_args()

    if args.http:
        run_http_server(host=args.host, port=args.port)
    else:
        run_stdio_server()


def run_stdio_server():
    """Run the bridge in stdio mode (for local OpenHands agents)."""
    sys.stderr.write(f"[hermes_bridge] {SERVER_NAME} v{SERVER_VERSION} ready\n")
    sys.stderr.write(
        f"[hermes_bridge] {len(list_profiles())} profiles, "
        f"{len(STAFF_ROSTER)} staff roles, {len(TOOL_HANDLERS)} tools\n"
    )
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(
                f"[hermes_bridge] Invalid JSON-RPC: {e}\n  raw: {line[:200]}\n"
            )
            sys.stderr.flush()
            continue

        try:
            response = _handle_request(msg)
            if response is not None:
                sys.stdout.write(response + "\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"[hermes_bridge] Fatal handler error: {e}\n")
            sys.stderr.flush()


def run_http_server(host: str = "0.0.0.0", port: int = 8787):
    """Run the bridge as an HTTP server (for cloud OpenHands agents)."""
    try:
        import uvicorn
        from fastapi import FastAPI, Body
        from fastapi.responses import JSONResponse
    except ImportError:
        sys.stderr.write(
            "[hermes_bridge] HTTP mode requires: pip install fastapi uvicorn\n"
        )
        sys.exit(1)

    app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)

    @app.get("/")
    async def root():
        return {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "profiles": len(list_profiles()),
            "staff": len(STAFF_ROSTER),
            "tools": len(TOOL_HANDLERS),
        }

    @app.get("/mcp")
    async def mcp_sse():
        """SSE endpoint — OpenHands cloud uses this to discover tools."""
        return JSONResponse({"protocol": "mcp", "transport": "sse"})

    @app.post("/mcp")
    async def mcp_call(body: dict = Body(...)):
        """Handle a JSON-RPC MCP request over HTTP."""
        response = _handle_request(body)
        if response is None:
            return JSONResponse({"ok": True})
        return JSONResponse(json.loads(response))

    sys.stderr.write(
        f"[hermes_bridge] HTTP server starting on {host}:{port}\n"
    )
    sys.stderr.flush()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
