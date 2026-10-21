"""serena_adapter.py — Thin MCP stdio client for one-shot Serena queries.

Serena (`oraios/serena`_) is an LSP-backed MCP server for code intelligence
(symbol search, references, rename, diagnostics). This adapter starts Serena
as a short-lived subprocess, sends a single MCP request, and returns the
parsed result — following the same "shell out" pattern used by
``engram_adapter`` and ``_cocoindex_context``.

Design:
- **One-shot**: starts Serena, sends initialize → tools/call, reads response,
  shuts down. No long-lived process.
- **Non-blocking**: all failures return ``None`` or empty list.
- **Minimal**: only ``find_symbol`` and ``find_referencing_symbols`` — the two
  tools most useful for pre-execution context enrichment (Layer 1).

.. _oraios/serena: https://github.com/oraios/serena
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


def serena_available() -> bool:
    """True if the ``serena`` CLI is on PATH."""
    return shutil.which("serena") is not None


# ---------------------------------------------------------------------------
# MCP stdio client core
# ---------------------------------------------------------------------------

def _call_serena_tool(
    tool_name: str,
    arguments: dict[str, Any],
    project_path: Optional[Path] = None,
    timeout: int = 20,
) -> Optional[dict[str, Any]]:
    """Start Serena, call a single MCP tool, return the parsed result.

    Starts ``serena start-mcp-server`` as a subprocess, performs the
    MCP handshake (initialize → initialized notification), sends a
    ``tools/call`` request, reads the response, and shuts down.

    Returns the parsed JSON content from the MCP tool result, or
    ``None`` on any failure.
    """
    if not serena_available():
        return None

    cwd = str(project_path) if project_path else os.getcwd()

    try:
        proc = subprocess.Popen(
            ["serena", "start-mcp-server", "--project-from-cwd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
    except (FileNotFoundError, OSError) as exc:
        sys.stderr.write(f"[serena] failed to start: {exc}\n")
        return None

    try:
        # ── Step 1: MCP initialize ──
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "school-core",
                            "version": "0.1.0",
                        },
                    },
                }
            )
            + "\n"
        )
        proc.stdin.flush()

        # Read lines until we get the initialize response (skip log/status
        # lines that some MCP servers emit on stdout before JSON-RPC).
        init_response = _read_jsonrpc_response(proc, expected_id=1, timeout=timeout)
        if init_response is None:
            sys.stderr.write("[serena] no initialize response\n")
            return None
        if "error" in init_response:
            sys.stderr.write(
                f"[serena] initialize error: {init_response['error']}\n"
            )
            return None

        # ── Step 2: Send initialized notification ──
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
        )
        proc.stdin.flush()

        # ── Step 3: Call the tool ──
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }
            )
            + "\n"
        )
        proc.stdin.flush()

        # ── Step 4: Read the tool response ──
        tool_response = _read_jsonrpc_response(proc, expected_id=2, timeout=timeout)
        if tool_response is None:
            sys.stderr.write(f"[serena] no response for tool '{tool_name}'\n")
            return None
        if "error" in tool_response:
            sys.stderr.write(
                f"[serena] tool error: {tool_response['error']}\n"
            )
            return None

        result = tool_response.get("result", {})

        # MCP tool results are wrapped in a ``content`` array of
        # ``{type: "text", text: "..."}`` blocks. Unwrap the first one.
        content = result.get("content", [])
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": text}

        return result

    except (BrokenPipeError, OSError) as exc:
        sys.stderr.write(f"[serena] communication error: {exc}\n")
        return None
    finally:
        _shutdown_proc(proc)


def _read_jsonrpc_response(
    proc: subprocess.Popen,
    expected_id: int,
    timeout: int,
) -> Optional[dict[str, Any]]:
    """Read lines from the subprocess stdout until a JSON-RPC message
    with the expected ID is found (or timeout). Non-JSON lines are
    silently skipped.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            # Process closed its stdout — give it a moment for stderr
            # to settle, then check if it exited with an error.
            return None
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Debug/log line from the MCP server on stdout — skip.
            continue
        if msg.get("id") == expected_id:
            return msg
        # Could be a notification or a response with a different ID —
        # keep reading.
    return None


def _shutdown_proc(proc: subprocess.Popen) -> None:
    """Best-effort shutdown of the Serena subprocess."""
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_symbol(
    name: str,
    project_path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Resolve a symbol name to its definition location via Serena's LSP.

    Returns a dict with keys like ``name``, ``kind``, ``file``, ``line``,
    ``container_name``, or ``None`` if the symbol is not found or Serena
    is unavailable.
    """
    return _call_serena_tool(
        "find_symbol",
        {"name_path_pattern": name, "include_body": False},
        project_path=project_path,
    )


def find_referencing_symbols(
    name: str,
    project_path: Optional[Path] = None,
) -> Optional[list[dict[str, Any]]]:
    """Find all symbols that reference the given symbol name.

    Returns a list of dicts with ``name``, ``kind``, ``file``, ``line``,
    or ``None`` on failure.
    """
    result = _call_serena_tool(
        "find_referencing_symbols",
        {"name_path_pattern": name},
        project_path=project_path,
    )
    if result is None:
        return None
    # Result may be a list directly or wrapped in a dict.
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        items = result.get("symbols") or result.get("references") or []
        if isinstance(items, list):
            return items
    return []
