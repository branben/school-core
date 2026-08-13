"""F6: adaptive but bounded crew admission policy."""

from crew_admission import decide_admission


def test_default_capacity_admits_first_task():
    decision = decide_admission(
        dispatched=0, configured_cap=1, runner_slots=1,
        active_claims=0, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert decision.admitted is True
    assert decision.reason == "admitted"


def test_configured_cap_and_runner_slots_bound_admission():
    decision = decide_admission(
        dispatched=1, configured_cap=3, runner_slots=2,
        active_claims=0, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert decision.admitted is True
    assert decision.effective_cap == 2

    blocked = decide_admission(
        dispatched=2, configured_cap=3, runner_slots=2,
        active_claims=0, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert blocked.admitted is False
    assert blocked.reason == "crew_cap_reached"


def test_active_claims_and_cycle_budget_fail_closed():
    active = decide_admission(
        dispatched=0, configured_cap=2, runner_slots=2,
        active_claims=2, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert active.admitted is False
    assert active.reason == "crew_in_flight"

    too_late = decide_admission(
        dispatched=0, configured_cap=2, runner_slots=2,
        active_claims=0, remaining_seconds=901,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert too_late.admitted is False
    assert too_late.reason == "insufficient_cycle_time"


def test_zero_capacity_fails_closed():
    no_runner = decide_admission(
        dispatched=0, configured_cap=1, runner_slots=0,
        active_claims=0, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert no_runner.admitted is False
    assert no_runner.reason == "crew_cap_reached"
    assert no_runner.effective_cap == 0

    disabled = decide_admission(
        dispatched=0, configured_cap=0, runner_slots=4,
        active_claims=0, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=0,
    )
    assert disabled.admitted is False
    assert disabled.reason == "crew_cap_reached"
    assert disabled.effective_cap == 0


def test_retry_pressure_reduces_optional_crew_admission():
    decision = decide_admission(
        dispatched=0, configured_cap=2, runner_slots=2,
        active_claims=0, remaining_seconds=1800,
        crew_timeout_seconds=900, retry_pressure=2,
        retry_pressure_limit=2,
    )
    assert decision.admitted is False
    assert decision.reason == "retry_pressure"
