"""Orca CLI adapter — concrete implementation of OrcaAdapter.

Wraps subprocess calls to the Orca CLI behind the OrcaAdapter interface.
This is the live implementation for production use.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from adapters.base import OrcaAdapter, OrcaUnavailableError


class OrcaCLIAdapter(OrcaAdapter):
    """Concrete Orca adapter that shells out to the `orca` CLI.

    Mirrors the contract of ``OrcaExecutionManager`` from ``orca_executor.py``
    but exposes it through the uniform ``OrcaAdapter`` interface so callers
    can depend on the abstraction rather than the concrete CLI invocation.
    """

    def __init__(self, timeout: int = 15):
        self._default_timeout = timeout

    def _run_orca(self, args: list[str], timeout: int | None = None) -> dict:
        """Run an Orca CLI command with --json and parse the output."""
        cmd = ["orca"] + args + ["--json"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self._default_timeout,
            )
        except subprocess.TimeoutExpired:
            raise OrcaUnavailableError(
                f"Orca CLI timed out ({timeout}s): {' '.join(args)}"
            )
        except FileNotFoundError:
            raise OrcaUnavailableError("Orca CLI not found (is Orca installed?)")

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or str(proc.returncode))[:300]
            raise OrcaUnavailableError(
                f"Orca command failed (exit={proc.returncode}): {detail}"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise OrcaUnavailableError(
                f"Orca JSON parse error: {e}\nRaw output: {proc.stdout[:200]}"
            )

        if isinstance(data, dict) and data.get("error"):
            raise OrcaUnavailableError(f"Orca error: {data['error']}")

        if isinstance(data, dict) and "result" in data:
            return data["result"]

        return data

    def create_worktree(self, name: str, repo_path: Optional[str] = None) -> str:
        """Create a child worktree and return its absolute path."""
        args = ["worktree", "create", "--name", name]
        if repo_path:
            args.extend(["--repo", repo_path])
        result = self._run_orca(args, timeout=45)

        wt_info = result.get("worktree", result)
        wt_id = wt_info.get("id", "")
        if "::" in wt_id:
            path = wt_id.split("::", 1)[1]
        else:
            path = result.get("path", wt_info.get("path", ""))

        if not path:
            raise OrcaUnavailableError(
                f"Failed to create worktree '{name}': {json.dumps(result)[:300]}"
            )
        return path

    def close_worktree(self, path: str) -> bool:
        """Remove a worktree by path. Idempotent."""
        import os
        if not os.path.exists(path):
            return True

        try:
            self._run_orca(
                ["worktree", "rm", "--worktree", f"path:{path}", "--force"],
                timeout=15,
            )
        except OrcaUnavailableError:
            return False
        return not os.path.exists(path)

    def create_terminal(self, title: str = "exec") -> str:
        """Create a terminal and return its handle."""
        result = self._run_orca(
            ["terminal", "create", "--title", title], timeout=15
        )
        terminal = result.get("terminal", result)
        handle = terminal.get("handle", "")
        if not handle:
            raise OrcaUnavailableError(
                f"Failed to get terminal handle: {json.dumps(result)[:200]}"
            )
        return handle

    def close_terminal(self, handle: str) -> None:
        """Close a terminal session. Best-effort."""
        try:
            self._run_orca(
                ["terminal", "close", "--terminal", handle], timeout=10
            )
        except OrcaUnavailableError:
            pass

    def status(self) -> dict:
        """Get Orca runtime status."""
        try:
            return self._run_orca(["status"], timeout=10)
        except OrcaUnavailableError:
            return {"runtime": {"state": "unavailable"}}
