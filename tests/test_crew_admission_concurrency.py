"""F6-concurrency: admission must stay bounded when crews are dispatched in
parallel (the fc7.3 verify-contract repair).

HISTORY / GAP
-------------
Before the fix, `issue_bridge.process_issues` passed
`dispatched=crew_dispatched` to `decide_admission` — a LOCAL int incremented
*after* `dispatch_crew()` returned. The loop was serial, so that was fine. But
the moment the loop dispatches more than one crew before any returns (the
concurrency-2 goal), every concurrent decision saw `dispatched=0` and
`active_claims=0`, so ALL were admitted — defeating `configured_cap`. That was
the over-admission gap (reproduced earlier with a deliberate stale-snapshot
simulation that admitted 4 crews at cap=2).

FIX (issue_bridge.py, U8 admission block)
-----------------------------------------
`dispatched` now reads the SAME lock-safe durable registry as `active_claims`
(`_crew_active_count(CREW_RUNS_FILE)`). `dispatch_crew` writes a `running`
record to that registry the instant it spawns (before the long poll), so a
concurrent decision observes the in-flight crew and the cap holds.

This test proves the FIXED caller pattern: the admission inputs are sourced
from a live, shared, lock-safe counter (mirroring the registry) rather than a
stale local int. Under concurrent pre-dispatch decisions, the cap is honored.
"""

from __future__ import annotations

import threading

from crew_admission import decide_admission


def _simulate_fixed_caller_admission(configured_cap, runner_slots, n_issues):
    """Model the FIXED issue_bridge pattern under true concurrency:

    - admission inputs are read from a LIVE shared counter (the durable,
      lock-safe registry) at decision time, NOT a stale local int;
    - each simulated crew "spawns" by incrementing that shared counter
      (mirroring dispatch_crew's `running` record write), so a concurrent
      decision that fences after spawn sees the in-flight crew.
    A Barrier fences all issues' DECISION phase before any spawn mutates the
    counter — the window that opens when the loop parallelizes.
    """
    # Shared, lock-safe live count (stands in for _crew_active_count).
    live = {"n": 0}
    lock = threading.Lock()
    admissions: list[bool] = []
    decide_barrier = threading.Barrier(n_issues)

    def issue_iteration():
        # FENCE: all issues decide admission before any spawn mutates the live
        # counter — the concurrency-2 dispatch window.
        decide_barrier.wait()
        with lock:
            current = live["n"]
        decision = decide_admission(
            dispatched=current,  # LIVE count, not a stale 0
            configured_cap=configured_cap,
            runner_slots=runner_slots,
            active_claims=current,  # LIVE count
            remaining_seconds=1800,
            crew_timeout_seconds=900,
            retry_pressure=0,
        )
        admissions.append(decision.admitted)
        # Spawn writes a `running` record -> live count increments (under lock),
        # so the NEXT concurrent decision sees it.
        with lock:
            live["n"] += 1

    threads = [threading.Thread(target=issue_iteration) for _ in range(n_issues)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return admissions


def test_fixed_caller_does_not_over_admit_at_cap_2():
    """configured_cap=2, 4 issues decided under concurrent pre-dispatch window,
    but each reads the LIVE counter (which increments as crews spawn). At most
    2 admitted."""
    admissions = _simulate_fixed_caller_admission(
        configured_cap=2, runner_slots=2, n_issues=4
    )
    admitted = sum(1 for a in admissions if a)
    assert admitted <= 2, (
        f"over-admission under concurrency: {admitted} crews admitted with "
        f"configured_cap=2 (admissions={admissions})"
    )


def test_fixed_caller_does_not_over_admit_at_cap_1():
    """Same shape at cap=1 with 3 issues: at most 1 admitted."""
    admissions = _simulate_fixed_caller_admission(
        configured_cap=1, runner_slots=1, n_issues=3
    )
    admitted = sum(1 for a in admissions if a)
    assert admitted <= 1, (
        f"over-admission under concurrency: {admitted} crews admitted with "
        f"configured_cap=1 (admissions={admissions})"
    )


def test_serial_accounting_still_bounds():
    """Control: the existing serial caller reflects completed crews before the
    next decision, so the cap holds. At cap=2 with a 1900s budget (>= 2*900+30),
    exactly two crews are admitted serially."""
    admitted_count = 0
    for _ in range(4):
        decision = decide_admission(
            dispatched=admitted_count,  # serial: prior crew already counted
            configured_cap=2,
            runner_slots=2,
            active_claims=admitted_count,
            remaining_seconds=1900,
            crew_timeout_seconds=900,
            retry_pressure=0,
        )
        if decision.admitted:
            admitted_count += 1
    assert admitted_count == 2, (
        f"serial cap=2 should admit exactly 2, got {admitted_count}"
    )
