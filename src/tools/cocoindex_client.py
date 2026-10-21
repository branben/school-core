#!/usr/bin/env python3
"""cocoindex client for prior-solution retrieval (Rank 8).

Thin, offline-safe wrapper around the ``prior_solutions`` cocoindex app in
``scripts/prior_solutions/``. The app is a *service/app* (not an importable
library in this repo's runtime), so retrieval is done by shelling out to
``uv run`` in the app directory. All failures degrade gracefully — callers
must treat the returned list as a hint, not a requirement.

Design notes:
- Availability = ``cocoindex`` CLI on PATH AND the app dir present. We do NOT
  import cocoindex here (it is only resolvable inside the app's uv venv).
- Retrieval returns up to ``k`` (task_id, score, snippet) tuples parsed from
  the app's query.py stdout markers.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

APP_DIR = Path(__file__).parent / "prior_solutions"
QUERY_SCRIPT = APP_DIR / "query.py"

_KNOWN_TASKS: List[Tuple[str, float, str]] = []


def cocoindex_available() -> bool:
    """True if retrieval can be attempted (CLI + app present)."""
    return bool(shutil.which("cocoindex")) and APP_DIR.is_dir() and QUERY_SCRIPT.is_file()


def retrieve_prior_solutions(task_prompt: str, k: int = 3) -> List[Tuple[str, float, str]]:
    """Return up to ``k`` prior solutions similar to ``task_prompt``.

    Returns a list of (task_id, score, snippet) tuples, empty on any failure.
    Never raises.
    """
    if not cocoindex_available():
        return []
    try:
        proc = subprocess.run(
            ["uv", "run", "--project", str(APP_DIR), "python", str(QUERY_SCRIPT), task_prompt, str(k)],
            capture_output=True, text=True, timeout=90,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # uv not installed or the app venv is broken — degrade gracefully.
        import logging
        logging.getLogger(__name__).warning("[cocoindex_client] retrieval failed: %s", exc)
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    return _parse_query_output(proc.stdout)


def _parse_query_output(stdout: str) -> List[Tuple[str, float, str]]:
    """Parse query.py's ``TASK:..\tSCORE:..`` + ``----``/``====`` markers."""
    results: List[Tuple[str, float, str]] = []
    lines = stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("TASK:"):
            try:
                meta = line.split("\t")
                task_id = meta[0][len("TASK:"):]
                score = float(meta[1][len("SCORE:"):])
            except (IndexError, ValueError):
                i += 1
                continue
            # Snippet spans from after the next "----" to the "====" marker.
            i += 1
            snippet_lines: List[str] = []
            while i < len(lines) and lines[i] != "====":
                if lines[i] != "----":
                    snippet_lines.append(lines[i])
                i += 1
            results.append((task_id, score, "\n".join(snippet_lines).strip()))
        i += 1
    return results
