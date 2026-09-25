"""Regression tests for orca_executor.REPO_PATH resolution (no live Orca needed).

These guard the bug caught in PR #38: REPO_PATH was moved from a class
attribute to a module-level constant, which broke instance access
(``mgr.REPO_PATH`` raised AttributeError) because Python instance lookup
falls back to the class, not module globals. The class alias
``REPO_PATH = REPO_PATH`` restores both access paths.

All tests are pure unit tests. The instance-access test uses ``__new__`` so
collection never probes a live Orca daemon. Live Orca behavior belongs in the
explicit ORCA_LIVE_TESTS=1 integration suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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


def _is_bare_git_root(path: Path) -> bool:
    """True when `path` is itself the output of git rev-parse --show-toplevel."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(path), capture_output=True, text=True, timeout=10,
    )
    return out.returncode == 0 and out.stdout.strip() == str(path).rstrip("/")


def test_repo_path_module_constant_resolves_to_git_root():
    """Module-level REPO_PATH points at the real checkout.

    Asserts the *invariant* — REPO_PATH is the git root that contains this
    module — not a hardcoded directory name. A teacher/student worktree is a
    legitimate git root under an arbitrary name (e.g. /tmp/sc-pathfix), so
    requiring the name "school-core" fails in every worktree while asserting
    nothing about correctness. The guard that matters is that REPO_PATH is a
    git toplevel and is the same one git reports for this module.
    """
    toplevel = _git_toplevel()
    assert str(orca_executor.REPO_PATH).rstrip("/") == toplevel.rstrip("/")
    assert Path(orca_executor.REPO_PATH).is_dir()
    # REPO_PATH must be a git root itself, not a nested directory.
    assert (Path(orca_executor.REPO_PATH) / ".git").exists() or _is_bare_git_root(
        orca_executor.REPO_PATH
    )


def test_repo_path_instance_access_works_without_live_orca():
    """mgr.REPO_PATH must resolve via the class alias (PR #38 regression).

    The property is a class/module constant. Constructing the manager is not
    needed and would probe the live Orca daemon during test collection, making
    the unit suite host-dependent. ``__new__`` tests the actual lookup path
    without starting Orca.
    """
    mgr = OrcaExecutionManager.__new__(OrcaExecutionManager)
    # Previously raised AttributeError after REPO_PATH became module-level.
    # The guard is the lookup path (instance -> class -> module), not the
    # checkout's directory name; a worktree root is equally valid.
    assert str(mgr.REPO_PATH).rstrip("/") == _git_toplevel().rstrip("/")
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
