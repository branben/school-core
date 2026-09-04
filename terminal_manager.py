from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

from orca_executor import OrcaUnavailableError

class TerminalManager:
    def __init__(self, _run_orca_fn):
        self._run_orca = _run_orca_fn

    def create_terminal(self, title: str = "exec") -> str:
        """Create a terminal in the current project context and return its handle.

        No --worktree flag is passed, so Orca associates the terminal with
        the current project's worktree (visible in Orca's UI sidebar).

        Args:
            title: Terminal title shown in Orca's UI.

        Returns:
            Terminal handle string for subsequent commands.

        Raises:
            OrcaUnavailableError: If terminal cannot be created.
        """
        result = self._run_orca([
            "terminal", "create",
            "--title", title,
        ], timeout=15)

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
            self._run_orca(["terminal", "close", "--terminal", handle], timeout=10)
        except Exception:
            pass

    def _read_terminal_tail(self, handle: str, cursor: Optional[str] = None) -> dict:
        """Read the tail of an Orca terminal's output.

        Orca's read API returns lines in the 'tail' field (not 'lines').
        Items are plain strings. Response structure (after _run_orca
        unwraps 'result'): {'terminal': {'tail': [...], 'nextCursor': 'N', ...}}

        Returns empty tail on any error (never raises).
        """
        args = ["terminal", "read", "--terminal", handle, "--limit", "500"]
        if cursor is not None:
            args.extend(["--cursor", cursor])

        try:
            result = self._run_orca(args, timeout=10)
            terminal_info = result.get("terminal", result)
            return terminal_info
        except Exception:
            return {"tail": [], "nextCursor": "0", "latestCursor": "0"}
