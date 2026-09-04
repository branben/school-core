"""Worktree lifecycle management for Orca."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from orca_executor import OrcaUnavailableError


class WorktreeManager:
    """Manages Orca worktree lifecycle: create, find, close, cleanup."""

    def __init__(self, orca_runner):
        self._run_orca = orca_runner

    def create(self, name: str, repo_path: Optional[Path] = None) -> str:
        """Create a worktree and return its ID."""
        args = ["worktree", "create", "--name", name]
        if repo_path:
            args += ["--repo", str(repo_path)]
        result = self._run_orca(args, timeout=30)
        wt = result.get("worktree", result)
        wt_id = wt.get("id", "")
        if not wt_id:
            raise OrcaUnavailableError(f"Failed to create worktree '{name}'")
        return wt_id

    def find_by_prefix(self, prefix: str) -> list[dict]:
        """Find all worktrees whose name starts with prefix."""
        try:
            result = self._run_orca(["worktree", "list"], timeout=15)
        except OrcaUnavailableError:
            return []
        wts = result.get("worktrees", result.get("result", {}).get("worktrees", []))
        if not isinstance(wts, list):
            return []
        return [wt for wt in wts if isinstance(wt, dict) and wt.get("name", "").startswith(prefix)]

    def close(self, path: str) -> bool:
        """Close a worktree. Returns True on success."""
        try:
            self._run_orca(["worktree", "close", "--path", path], timeout=15)
            return True
        except Exception:
            return False

    def cleanup_by_prefix(self, prefix: str = "study-") -> int:
        """Close all worktrees matching prefix. Returns count cleaned."""
        matching = self.find_by_prefix(prefix)
        cleaned = 0
        for wt in matching:
            path = wt.get("path", "")
            if path and self.close(path):
                cleaned += 1
        return cleaned

    def write_student_brief(self, worktree_path: str, brief) -> Path:
        """Write a StudentBrief JSON to the worktree."""
        brief_path = Path(worktree_path) / ".student-brief.json"
        brief_path.write_text(json.dumps(brief.to_dict() if hasattr(brief, "to_dict") else brief, indent=2))
        return brief_path

    def write_student_output(self, worktree_path: str, bead: str, data: dict) -> Path:
        """Write student output JSON to the worktree."""
        output_path = Path(worktree_path) / f"{bead}.output.json"
        output_path.write_text(json.dumps(data, indent=2))
        return output_path
