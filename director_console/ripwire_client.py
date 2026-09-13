"""Subprocess wrapper for ripwire — the 'ripgrep of AI context'.

Gracefully degrades when the ripwire binary is missing: callers get
None back instead of an exception, so the queue can still render.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional


RIPWIRE_BIN = shutil.which("ripwire") or "~/.local/bin/ripwire"


def _resolve_bin() -> Optional[str]:
    """Return the ripwire binary path if it exists, else None."""
    import os

    path = os.path.expanduser(RIPWIRE_BIN)
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return shutil.which("ripwire")


def blast_radius(
    repo_path: str,
    query: str,
    top_k: int = 5,
    timeout_s: int = 15,
) -> Optional[str]:
    """Return a ranked symbol map for *query* inside *repo_path*.

    Returns None when ripwire is missing or errors out — callers must
    handle the None case (the queue renders without context).
    """
    bin_path = _resolve_bin()
    if bin_path is None:
        return None

    cmd = [
        bin_path,
        repo_path,
        f"--top-k={top_k}",
        "--format=candidates",
        "--query",
        query,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip() or None
