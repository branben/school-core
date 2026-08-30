"""Tests for the solo-dev fast lane policy."""

from fast_lane import REQUIRED_CHECKS, decide_fast_lane, summarize_lane_metrics


def _task(**overrides):
    task = {
        "bd_id": "school-core-1",
        "risk": "low",
        "expected_files": 1,
        "single_file": True,
        "checks_available": REQUIRED_CHECKS,
    }
    task.update(overrides)
    return task


def test_bounded_work_is_admitted_with_all_assurance_checks():
    decision = decide_fast_lane(_task())

    assert decision.admitted is True
    assert decision.reason == "low_risk_bounded"
    assert decision.required_checks == REQUIRED_CHECKS


def test_missing_bd_identity_fails_closed():
    decision = decide_fast_lane(_task(bd_id=None))

    assert decision.admitted is False
    assert decision.reason == "bd_identity_missing"


def test_risk_and_scope_disqualifiers_are_deterministic():
    assert decide_fast_lane(_task(risk="medium")).reason == "high_risk"
    assert decide_fast_lane(_task(expected_files=2)).reason == "multi_file"
    assert decide_fast_lane(_task(security=True)).reason == "security"
    assert decide_fast_lane(_task(checks_available=["bd_claim"])).reason == "checks_unavailable"


def test_metrics_do_not_claim_a_win_without_both_lanes():
    metrics = summarize_lane_metrics([
        {"lane": "fast", "rework": False, "overhead_ms": 10},
    ])

    assert metrics["comparable"] is False
    assert metrics["promotion_safe"] is False
    assert metrics["rework_rate_delta"] is None


def test_metrics_require_lower_rework_and_lower_overhead():
    metrics = summarize_lane_metrics([
        {"lane": "fast", "rework": False, "overhead_ms": 10},
        {"lane": "fast", "rework": False, "overhead_ms": 12},
        {"lane": "full", "rework": False, "overhead_ms": 30},
        {"lane": "full", "rework": True, "overhead_ms": 40},
    ])

    assert metrics["comparable"] is True
    assert metrics["rework_rate_delta"] == -0.5
    assert metrics["overhead_reduction_ms"] == 28
    assert metrics["promotion_safe"] is True


def test_fast_lane_is_not_promoted_when_rework_is_higher():
    metrics = summarize_lane_metrics([
        {"lane": "fast", "rework": True, "overhead_ms": 10},
        {"lane": "full", "rework": False, "overhead_ms": 30},
    ])

    assert metrics["promotion_safe"] is False
