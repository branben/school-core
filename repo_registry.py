"""Repo registration management for Orca."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from orca_executor import OrcaUnavailableError


class RepoRegistry:
    """Ensures repo paths are known to Orca and manages repo IDs."""

    def __init__(self, orca_runner):
        """Initialize with an Orca command runner.

        Args:
            orca_runner: Callable that runs Orca CLI commands and returns parsed JSON.
                        Should match the signature of OrcaExecutionManager._run_orca.
        """
        self._run_orca = orca_runner

    def register_repo(self, repo_path: Path) -> Optional[str]:
        """Ensure a repo path is known to Orca; return its repo id.

        Orca requires a repo to be registered (`orca repo add --path`)
        before `worktree create --repo <path>` will accept it. A fresh
        cross-repo clone is not registered, so this is mandatory for
        cross-repo dispatch. Idempotent: if already listed, reuse the id.

        Args:
            repo_path: Absolute path to the local clone.

        Returns:
            The Orca repo id, or None if registration failed.
        """
        try:
            listed = self._run_orca(["repo", "list"], timeout=15)
        except OrcaUnavailableError:
            return None
        repos = listed.get("repos", listed.get("repositories", []))
        for r in repos if isinstance(repos, list) else []:
            if isinstance(r, dict) and Path(str(r.get("path", ""))).resolve() == Path(repo_path).resolve():
                return r.get("id")
        # Not registered — add it.
        try:
            added = self._run_orca(["repo", "add", "--path", str(repo_path)], timeout=30)
        except OrcaUnavailableError:
            return None
        if isinstance(added, dict):
            return added.get("id") or added.get("repo", {}).get("id")
        return None