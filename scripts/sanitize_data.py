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

# Default cap for committed trajectory history (U2). Trajectory filenames are
# timestamp-prefixed (YYYYmmdd_HHMMSS_ffffff), so the newest N by name = the
# last N cycles. Keeps git history bounded while preserving Layer 2 memory.
DEFAULT_TRAJECTORY_KEEP = 60

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


def trim_trajectories(keep: int = DEFAULT_TRAJECTORY_KEEP, traj_dir: Path | None = None) -> int:
    """Delete all but the newest *keep* trajectory files. Returns count removed.

    Trajectory filenames carry a UTC timestamp prefix, so lexicographic order
    == chronological order. Called by the school-loop checkpoint before the
    sanitize + git add -f step (U2), so a fresh checkout sees the last N cycles
    of Layer 2 history without unbounded repo growth.

    *traj_dir* is injectable for tests; defaults to <repo>/data/trajectories.
    """
    # files[:-0] == files[:0] == [] silently keeps everything — a keep=0 (or
    # negative) is a footgun, not a mode. Clamp to a sane minimum.
    keep = max(1, int(keep))
    traj_dir = Path(traj_dir) if traj_dir else Path(__file__).parent.parent / "data" / "trajectories"
    if not traj_dir.exists():
        return 0
    files = sorted(traj_dir.glob("*.json"))
    if len(files) <= keep:
        return 0
    removed = 0
    for f in files[:-keep]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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
    args = list(sys.argv[1:])
    # U2: `--trim-trajectories [N]` trims the trajectory dir to the newest N
    # files (default DEFAULT_TRAJECTORY_KEEP) before sanitizing, then sanitizes
    # each remaining trajectory alongside the explicit file args.
    trim_keep = None
    if "--trim-trajectories" in args:
        idx = args.index("--trim-trajectories")
        del args[idx]
        if idx < len(args) and not args[idx].startswith("--"):
            try:
                trim_keep = int(args[idx])
                del args[idx]
            except ValueError:
                pass
        if trim_keep is None:
            trim_keep = DEFAULT_TRAJECTORY_KEEP
        removed = trim_trajectories(keep=trim_keep)
        print(f"[sanitize] trajectories trimmed: removed {removed}")
        # Sanitize whatever remains so committed trajectories are PII-free.
        traj_dir = Path(__file__).parent.parent / "data" / "trajectories"
        if traj_dir.exists():
            args += sorted(str(p) for p in traj_dir.glob("*.json"))

    if not args:
        print("usage: python scripts/sanitize_data.py [--trim-trajectories [N]] <file.json> [...]")
        return 2
    total = 0
    for arg in args:
        total += sanitize_file(Path(arg))
    print(f"[sanitize] done: {total} paths scrubbed across {len(args)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
