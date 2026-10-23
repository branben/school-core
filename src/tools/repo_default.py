"""Single source of truth for the default target repository.

The framework is repo-agnostic: every entry point accepts an explicit
``repo=`` / ``target_repo=`` argument (a GitHub ``owner/name`` slug or a local
path). When none is supplied, :func:`default_repo` resolves it so the current
checkout *self-configures* — a clone of school-core defaults to
``branben/school-core``, a clone of sound-royale-ny defaults to
``branben/sound-royale-ny``, etc. — with an environment override taking
precedence.

This removes the previously-hardcoded ``branben/school-core`` default, making
the framework portable to any target repo without code changes.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def _remote_slug() -> Optional[str]:
    """Derive ``owner/name`` from the current checkout's ``origin`` remote."""
    here = Path(__file__).parent.expanduser().resolve()
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(here), capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    url = out.stdout.strip()
    # https://github.com/owner/name.git  OR  git@github.com:owner/name.git
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        url = url[4:].replace(":", "/")
    return url.rsplit("github.com/", 1)[-1] if "github.com/" in url else None


def default_repo() -> str:
    """Return the default target repo slug.

    Resolution order:
        1. ``AGENT_SCHOOL_REPO`` env var (explicit override)
        2. ``SCHOOL_REPO`` env var (legacy override, kept for back-compat)
        3. the current checkout's ``origin`` remote slug (self-configuring)
        4. ``__global__`` if nothing resolves (board global namespace)
    """
    override = os.environ.get("AGENT_SCHOOL_REPO") or os.environ.get("SCHOOL_REPO")
    if override:
        return override
    slug = _remote_slug()
    return slug or "__global__"
