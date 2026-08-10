#!/usr/bin/env python3
"""Sanitize runtime data files before they are committed to git.

Rewrites absolute home-directory paths (``/Users/<username>/...``) to the
portable ``~/...`` form so board state can be made durable in git without
leaking PII (the macOS username + home layout).

Why this is safe: the board renderer only uses ``Path(trajectory).name``
(basename) for the session deep-link, and the activity server serves
trajectories by basename only — so a scrubbed path renders and serves
identically to the absolute one.

Usage:
    python scripts/sanitize_data.py data/last_run.json data/issues_cache.json ...
"""

import json
import re
import sys
from pathlib import Path

# 1) Any absolute repo-root prefix ending in /school-core/data/ → relative data/
#    Handles both the local Mac (/Users/<name>/school-core/data/...) and the
#    GitHub runner (/home/runner/work/school-core/school-core/data/...) layouts.
REPO_PREFIX_RE = re.compile(r"(?:/[A-Za-z0-9_.~-]+)+/school-core/data/")

# 2) Any remaining /Users/<username> prefix → ~  (removes username + home layout)
HOME_RE = re.compile(r"/Users/[A-Za-z0-9_.-]+")


def scrub_value(value):
    """Recursively replace home paths in strings inside JSON-like values."""
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, str):
        return HOME_RE.sub("~", REPO_PREFIX_RE.sub("data/", value))
    return value


def sanitize_file(path: Path) -> int:
    """Sanitize one JSON file in place. Returns number of replacements made."""
    try:
        raw = path.read_text()
    except OSError as e:
        print(f"[sanitize] SKIP {path}: {e}")
        return 0

    hits_before = len(HOME_RE.findall(raw)) + len(REPO_PREFIX_RE.findall(raw))
    if hits_before == 0:
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Not JSON we own (e.g. plain-text board artifact) — fall back to
        # line-level replacement, preserving the rest of the file.
        cleaned = HOME_RE.sub("~", REPO_PREFIX_RE.sub("data/", raw))
        path.write_text(cleaned)
        print(f"[sanitize] {path}: text-level scrub {hits_before} -> 0")
        return hits_before

    data = scrub_value(data)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"[sanitize] {path}: {hits_before} home paths scrubbed")
    return hits_before


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/sanitize_data.py <file.json> [...]")
        return 2
    total = 0
    for arg in sys.argv[1:]:
        total += sanitize_file(Path(arg))
    print(f"[sanitize] done: {total} paths scrubbed across {len(sys.argv) - 1} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
