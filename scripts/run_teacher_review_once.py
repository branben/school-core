#!/usr/bin/env python3
"""One-shot teacher review for a scheduled Orca automation.

Invoked by an Orca automation (--provider hermes) on a trigger (e.g. every
5 min). Runs exactly ONE pass over un-reviewed bookbags for this teacher's
lens, then exits — Orca owns the schedule, so there is no while-True pane
and no per-boot terminal spray (the old run_teacher_loop.py anti-pattern).

Mirrors the principal migration: the persistent teacher worktree is created
once (rediscover-or-create); the review cycle is driven by Orca's automation
scheduler, not a Python process parked in a terminal.

Usage (emitted by conductor._boot_teachers):
    python3 scripts/run_teacher_review_once.py <role>
"""
import sys
import os
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from teacher import TeacherWorktree
from bookbag import REPO_GLOBAL


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: run_teacher_review_once.py <role> [repo]", file=sys.stderr)
        sys.exit(2)
    role = sys.argv[1]
    # Repo namespace (multi-repo isolation). Falls back to:
    #   1. explicit 2nd CLI arg, 2. SCHOOL_REPO env var, 3. __global__.
    repo = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.environ.get("SCHOOL_REPO", REPO_GLOBAL)
    )
    teacher = TeacherWorktree(role, repo=repo)
    # Rediscover-or-create the persistent worktree (boot is idempotent).
    teacher.boot()
    reviewed = 0
    # Drain any pending bookbags for this lens, then exit.
    while True:
        n = teacher.review_cycle()
        if n == 0:
            break
        reviewed += n
        time.sleep(0.2)
    print(f"[teacher:{role}] reviewed {reviewed} bookbag(s) this tick")


if __name__ == "__main__":
    main()
