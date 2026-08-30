#!/usr/bin/env python3
"""
Regenerate the per-student turn-budget table from the LIVE orca_executor code.

This is the session's "numbers are backed by code, not asserted" artifact.
Read-only: it imports OrcaExecutionManager and reports the budget table.
It does NOT touch anything.

Run from the school-core root:
  python3 regenerate_budgets.py

Expected output (with the session's orca_executor fixes in place):
  HERMES_TIMEOUT_PER_TURN_MS = 90000   (90 s/turn)
  _TURNS = {'easy': 1, 'medium': 3, 'hard': 5, 'diploma': 8}
  CREW_CAP_S = 900, JOB_CAP_S = 1800

  difficulty   turns   per-turn(s)   total(s)   total(min)   under_crew   under_job   ok?
  easy            1        90            90          1.50       True        True     1
  medium          3        90           270          4.50       True        True     1
  hard            5        90           450          7.50       True        True     1
  diploma         8        90           720         12.00       True        True     1

If the code is the OLD buggy values, this script prints the OLD table and a
warning — that's the point (it makes the discrepancy visible without asserting).
"""
from __future__ import annotations

from orca_executor import OrcaExecutionManager

CREW_CAP_S = 900   # crew_admission DEFAULT_TIMEOUT default (s)
JOB_CAP_S = 1800   # school-loop.yml execute job timeout-minutes: 30 (s)


def main() -> None:
    mgr = OrcaExecutionManager.__new__(OrcaExecutionManager)
    per_turn_ms = int(getattr(mgr, "HERMES_TIMEOUT_PER_TURN_MS", 0))
    turns = dict(getattr(mgr, "_TURNS", {}))
    per_turn_s = per_turn_ms / 1000.0

    header = (
        f"HERMES_TIMEOUT_PER_TURN_MS = {per_turn_ms}   ({per_turn_s:.0f} s/turn)\n"
        f"_TURNS = {turns}\n"
        f"CREW_CAP_S = {CREW_CAP_S}, JOB_CAP_S = {JOB_CAP_S}\n"
    )
    print(header)
    print(
        f"{'difficulty':<10} {'turns':>5} {'per-turn(s)':>12} {'total(s)':>9} "
        f"{'total(min)':>10} {'under_crew':>11} {'under_job':>10} {'ok?':>4}"
    )
    print("-" * 78)

    ok_all = True
    for difficulty in ("easy", "medium", "hard", "diploma"):
        cpu = turns.get(difficulty, 1)
        total_ms = cpu * per_turn_ms
        total_s = total_ms / 1000.0
        total_min = total_s / 60.0
        under_crew = total_s <= CREW_CAP_S
        under_job = total_s <= JOB_CAP_S
        ok = under_crew and under_job
        ok_all = ok_all and ok
        print(
            f"{difficulty:<10} {cpu:>5} {per_turn_s:>12.0f} {total_s:>9.0f} "
            f"{total_min:>10.2f} {str(under_crew):>11} {str(under_job):>10} "
            f"{'1' if ok else '0':>4}"
        )

    print("-" * 78)
    if ok_all:
        print("ALL difficulties fit under BOTH crew cap AND job cap.")
    else:
        print("WARNING: some difficulties exceed crew and/or job caps — "
              "the code is on old buggy values.")


if __name__ == "__main__":
    main()
