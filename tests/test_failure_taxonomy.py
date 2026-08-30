"""Tests for the normalized school-loop outcome contract."""

from failure_taxonomy import FAILURE_EDGES, FAILURE_MODES, NEXT_ACTIONS, normalize_outcome


def test_clean_accepted_run_is_completed_and_closes():
    outcome = normalize_outcome(
        status="success",
        review={"accepted": True, "combined_score": 92},
    )

    assert outcome == {
        "lifecycle": "completed",
        "quality": 0.92,
        "failure_edge": "none",
        "failure_mode": "none",
        "fallback_reason": None,
        "next_action": "close",
        "retry_attempt": 0,
    }


def test_zero_score_is_not_replaced_by_a_secondary_score():
    outcome = normalize_outcome(
        status="error",
        task_result={"task_score": 80},
        review={"accepted": False, "combined_score": 0},
    )

    assert outcome["quality"] == 0.0


def test_timeout_remains_runtime_failure_even_when_direct_fallback_succeeds():
    outcome = normalize_outcome(
        status="success",
        review={"accepted": True, "combined_score": 80},
        fallback_reason="timeout",
    )

    assert outcome["lifecycle"] == "completed"
    assert outcome["quality"] == 0.8
    assert outcome["failure_edge"] == "runtime"
    assert outcome["failure_mode"] == "timeout"
    assert outcome["next_action"] == "repair_runtime"


def test_syntax_failure_is_attributed_to_model_not_skill_or_verifier():
    outcome = normalize_outcome(
        status="error",
        error="SyntaxError: invalid syntax",
        review={"accepted": False, "combined_score": 64},
    )

    assert outcome["lifecycle"] == "failed"
    assert outcome["quality"] == 0.64
    assert outcome["failure_edge"] == "model"
    assert outcome["failure_mode"] == "syntax"
    assert outcome["next_action"] == "change_capability"


def test_judge_disagreement_requires_escalation():
    outcome = normalize_outcome(
        status="error",
        review={
            "accepted": False,
            "cto_verdict": "PASS",
            "coo_verdict": "FAIL",
            "combined_score": 74.5,
        },
    )

    assert outcome["failure_edge"] == "judge"
    assert outcome["failure_mode"] == "disagreement"
    assert outcome["next_action"] == "escalate"


def test_artifact_evidence_failure_is_contract_repair():
    outcome = normalize_outcome(
        status="failed",
        fallback_reason="artifact_evidence_missing",
    )

    assert outcome["failure_edge"] == "task_contract"
    assert outcome["failure_mode"] == "missing_evidence"
    assert outcome["next_action"] == "repair_contract"


def test_retry_and_blocked_lifecycles_are_not_quality_successes():
    retry = normalize_outcome(status="retry", error="gateway unavailable", retry_attempt=1)
    blocked = normalize_outcome(status="crew_in_flight", fallback_reason="crew_in_flight")

    assert retry["lifecycle"] == "retry"
    assert retry["next_action"] == "retry_same"
    assert blocked["lifecycle"] == "blocked"
    assert blocked["next_action"] == "human_decision"


def test_output_uses_only_bounded_contract_values():
    outcome = normalize_outcome(
        status="something-new",
        error="a very long diagnostic " * 100,
        fallback_reason="a reason with spaces and punctuation!",
    )

    assert set(outcome) == {
        "lifecycle", "quality", "failure_edge", "failure_mode",
        "fallback_reason", "next_action", "retry_attempt",
    }
    assert outcome["fallback_reason"] == "a_reason_with_spaces_and_punctuation"
    assert len(outcome["fallback_reason"]) <= 48
    assert outcome["failure_edge"] in FAILURE_EDGES
    assert outcome["failure_mode"] in FAILURE_MODES
    assert outcome["next_action"] in NEXT_ACTIONS
