#!/usr/bin/env python3
"""Persistent principal loop launcher.

Run inside the principal's Orca terminal by `conductor.py --serve`:
    python3 /path/to/run_principal_loop.py

Keeps the Agent School principal orchestrating indefinitely (booting
teachers, dispatching leaves, polling for two-judge verdicts) so the
orchestrator survives beyond a single foreground `conductor.py`
invocation.

Each iteration dispatches ONE round (a small batch of leaves, not an
eager 999999-worktree burst) and then loops again. This is a genuine
poll loop, not the previous `--rounds 999999` hack, which fed
`range(999999)` directly into `_run_async_loop`'s Step 2a and tried to
create nearly a million leaf worktrees up front before any review.
"""
import sys
import time
from pathlib import Path

# Ensure the repo root (parent of scripts/) is importable so
# `from conductor import main` resolves regardless of the cwd the
# terminal happened to launch from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from conductor import main

# One round per iteration keeps the leaf count bounded per loop turn
# (the async loop boots `args.rounds` leaves up front, so a small value
# is essential). The outer while True is what makes it always-on.
ROUNDS_PER_ITERATION = 1
POLL_INTERVAL_S = 30


def _iteration_argv() -> list[str]:
    return [
        "conductor.py",
        "--loop",
        "--async",
        "--rounds",
        str(ROUNDS_PER_ITERATION),
        "--handoff-timeout",
        "300",
    ]


def main_loop() -> None:
    while True:
        try:
            # Override argv for this dispatch round, then run the pipeline.
            sys.argv = _iteration_argv()
            main()
        except Exception as exc:  # never let one bad round kill the principal
            print(f"[principal] round error (continuing): {exc}")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main_loop()
