"""School Core — multi-agent AI education framework.

Path setup: when this package is imported from a root-level script (e.g.
teacher.py, leaf.py), we ensure the src/ directory is on sys.path so
that ``import school_core`` works without a pip install. This eliminates
the need for sys.path.insert hacks in individual root modules.
"""

from __future__ import annotations

__version__ = "0.1.0"

import sys
from pathlib import Path

# ── Path setup for root-level imports ───────────────────────────────────────
_pkg_init = Path(__file__).resolve()
_src_dir = _pkg_init.parent.parent
_repo_root = _src_dir.parent
for _p in (_src_dir, _repo_root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── Re-export shared utilities ─────────────────────────────────────────────
# These are the most commonly used exports that root-level scripts need.
from school_core.soul import load_soul  # noqa: F401
from school_core.paths import (  # noqa: F401
    REPO_ROOT,
    PACKAGE_DIR,
    CONFIG_DIR,
    PROFILES_DIR,
    ROLES_DIR,
    ANCHORS_PATH,
    ESCALATION_PATH,
    GITHUB_CONFIG_PATH,
    DATA_DIR,
    soul_path,
    home_soul_path,
    role_path,
)
