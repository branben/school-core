"""Regression tests: per-student turn budget sanity.

Verifies the timeout budget for each difficulty is sane — i.e. it actually
fits inside the crew + job ceilings (crew default 900s, job 30 min = 1800s)
and the _TURNS map matches the documented values (easy=1, medium=3, hard=5,
diploma=8).

History: orca_executor._TURNS was buggily set to medium=16/hard=16/diploma=20
with HERMES_TIMEOUT_PER_TURN_MS=120000, so a medium task booked 16*120s =
1920s — which exceeds the 900s crew cap and the 1800s job cap, guaranteeing
the crew/job timeout before the student could finish. This is why pilots
"timed out twice" (fc7.3) and why the fc7.6 pilot never used the intended
8 turns (hermes-fm-wrapper defaults FM_AGENT_MAX_TURNS to 16, and the pilot
omitted the cap).

"""
from __future__ import annotations

import pytest

from orca_executor import OrcaExecutionManager


# ── Budget ceilings (documented, not arbitrary) ────────────────────────────
CREW_CAP_S = 900          # crew_admission.DEFAULT_TIMEOUT default (s)
JOB_CAP_S = 1800          # school-loop.yml execute job timeout-minutes: 30


@pytest.fixture
def mgr():
    return OrcaExecutionManager.__new__(OrcaExecutionManager)


# ── Values under test ────────────────────────────────────────────────────────
ANSWER = {
    "HERMES_TIMEOUT_PER_TURN_MS": 90000,
    "_TURNS": {"easy": 1, "medium": 3, "hard": 5, "diploma": 8},
}


# ── RED: current code is wrong ──────────────────────────────────────────────

def test_timeout_per_turn_is_90s_not_120s(mgr):
    """120 s/turn overshoots the crew cap (900 s): 8 * 120 = 960 > 900."""
    assert mgr.HERMES_TIMEOUT_PER_TURN_MS == ANSWER["HERMES_TIMEOUT_PER_TURN_MS"], (
        f"HERMES_TIMEOUT_PER_TURN_MS={mgr.HERMES_TIMEOUT_PER_TURN_MS} "
        f"(should be {ANSWER['HERMES_TIMEOUT_PER_TURN_MS']}=90s). "
        "At 120s, 8 turns = 960s > 900s crew cap; a correctly capped 8-turn "
        "diploma task would still clip the crew timeout."
    )


@pytest.mark.parametrize(
    "difficulty, expected_cpu",
    [
        ("easy", 1),
        ("medium", 3),
        ("hard", 5),
        ("diploma", 8),
    ],
)
def test_turns_map_matches_docstring(mgr, difficulty, expected_cpu):
    actual = mgr._TURNS.get(difficulty, 1)
    assert actual == expected_cpu, (
        f"_TURNS[{difficulty}]={actual} (should be {expected_cpu}). "
        "16 was a temporary curb for 'killed mid-generation' but overshoots "
        "the budget and contradicts the docstring (medium=3, hard=5, "
        "diploma=8) at orca_executor.py line 1002."
    )


# ── GREEN: budget sanity (only after the fix) ───────────────────────────────

@pytest.mark.parametrize(
    "difficulty, expected_cpu",
    [
        ("easy", 1),
        ("medium", 3),
        ("hard", 5),
        ("diploma", 8),
    ],
)
def test_budget_fits_under_crew_and_job_caps(mgr, difficulty, expected_cpu):
    per_turn_ms = mgr.HERMES_TIMEOUT_PER_TURN_MS
    total_ms = expected_cpu * per_turn_ms
    total_s = total_ms / 1000.0
    assert total_s <= CREW_CAP_S, (
        f"{difficulty}: {expected_cpu} turns * {per_turn_ms}ms = {total_s:.0f}s "
        f"> CREW_CAP {CREW_CAP_S}s — this crew would be killed by the crew cap "
        f"before finishing."
    )
    assert total_s <= JOB_CAP_S, (
        f"{difficulty}: {total_s:.0f}s > JOB_CAP {JOB_CAP_S}s — the CI job would "
        f"time out before the student can finish."
    )
