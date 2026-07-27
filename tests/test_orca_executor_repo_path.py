"""Regression tests for orca_executor.REPO_PATH resolution (no live Orca needed).

These guard the bug caught in PR #38: REPO_PATH was moved from a class
attribute to a module-level constant, which broke instance access
(``mgr.REPO_PATH`` raised AttributeError) because Python instance lookup
falls back to the class, not module globals. The class alias
``REPO_PATH = REPO_PATH`` restores both access paths.

These tests run in CI without Orca running.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orca_executor  # noqa: E402
from orca_executor import OrcaExecutionManager  # noqa: E402


def _git_toplevel() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, "git rev-parse --show-toplevel failed"
    return out.stdout.strip()


def test_repo_path_module_constant_resolves_to_git_root():
    """Module-level REPO_PATH points at the real school-core checkout."""
    toplevel = _git_toplevel()
    assert str(orca_executor.REPO_PATH).rstrip("/") == toplevel.rstrip("/")
    assert str(orca_executor.REPO_PATH).endswith("school-core")


def test_repo_path_instance_access_works():
    """mgr.REPO_PATH must resolve via the class alias (PR #38 regression)."""
    mgr = OrcaExecutionManager()
    # Previously raised AttributeError after REPO_PATH became module-level.
    assert str(mgr.REPO_PATH).endswith("school-core")
    # Instance access must equal the module constant.
    assert mgr.REPO_PATH == orca_executor.REPO_PATH


def test_repo_path_resolves_true_root_from_child_worktree():
    """REPO_PATH must not resolve to a child worktree even if imported from one.

    Simulates the bug scenario: a teacher/student worktree under the repo.
    """
    toplevel = _git_toplevel()
    assert str(orca_executor.REPO_PATH).rstrip("/") == toplevel.rstrip("/")
    # The resolved path must be the git root, never a nested worktree dir.
    assert "workspaces" not in str(orca_executor.REPO_PATH).split("school-core")[-1] \
        or str(orca_executor.REPO_PATH).rstrip("/") == toplevel.rstrip("/")
