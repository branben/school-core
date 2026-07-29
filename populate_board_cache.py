#!/usr/bin/env python3
"""
populate_board_cache.py — Populate data/issues_cache.json from live GitHub issues.

Uses the ``gh`` CLI (GitHub CLI) to fetch open issues for a repository and
writes a board-compatible cache file at ``data/issues_cache.json``.

Stdlib only: json, subprocess, pathlib, os, sys.

Usage::

    python populate_board_cache.py [repo]

If *repo* is omitted it is read from the ``SCHOOL_REPO`` environment variable.
If neither is provided, :func:`repo_default.default_repo` resolves it — which
self-configures from the current checkout's ``origin`` remote (so a clone of
school-core defaults to ``branben/school-core``), overridable via
``AGENT_SCHOOL_REPO``.

The output is a JSON list of dicts, each with keys ``issue_number``, ``title``,
``domain``, ``difficulty``, ``state`` — the minimum shape required by
:func:`board.assign_column` and ``activity_server._load_board_data``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from repo_default import default_repo

CACHE_PATH = Path("data") / "issues_cache.json"


def populate_cache(repo: Optional[str] = None) -> list[dict]:
    """Fetch open issues via ``gh issue list`` and write them to *CACHE_PATH*.

    Parameters
    ----------
    repo : str, optional
        GitHub repository in ``owner/name`` format. Defaults to
        :func:`repo_default.default_repo` (self-configuring from the current
        checkout's ``origin`` remote, overridable via ``AGENT_SCHOOL_REPO``).

    Returns
    -------
    list[dict]
        The list of issue dicts that was written to disk.  Each dict has keys
        ``issue_number``, ``title``, ``domain``, ``difficulty``, ``state``.

    Notes
    -----
    - Runs ``gh issue list --repo <repo> --state open --json number,title,labels,body``.
    - If ``gh`` is unavailable or the command fails, writes an empty list.
    - Writes atomically (temp file + ``os.replace``).
    """
    if repo is None:
        repo = default_repo()
    issues: list[dict] = []

    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,title,labels,body",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0 and result.stdout.strip():
            try:
                raw = json.loads(result.stdout)
            except json.JSONDecodeError:
                raw = []

            for item in raw:
                issues.append(
                    {
                        "issue_number": item["number"],
                        "title": item["title"],
                        "domain": "_default",
                        "difficulty": "medium",
                        "state": "open",
                    }
                )
    except FileNotFoundError:
        sys.stderr.write(
            "[populate_board_cache] gh CLI not found — writing empty cache.\n"
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            "[populate_board_cache] gh command timed out — writing empty cache.\n"
        )
    except OSError as e:
        sys.stderr.write(
            f"[populate_board_cache] OS error running gh: {e} — writing empty cache.\n"
        )
    else:
        # gh ran but exited non-zero (e.g. auth/scope failure) — surface it.
        if result.returncode != 0:
            sys.stderr.write(
                f"[populate_board_cache] gh exited {result.returncode}: "
                f"{result.stderr.strip()[:200]} — writing empty cache.\n"
            )

    # Atomic write: temp → os.replace
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(issues, indent=2))
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        sys.stderr.write(
            f"[populate_board_cache] Failed to write cache: {e}\n"
        )
        return issues

    print(
        f"[populate_board_cache] Wrote {len(issues)} issue(s) to {CACHE_PATH}"
    )
    return issues


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SCHOOL_REPO")
    populate_cache(repo)
