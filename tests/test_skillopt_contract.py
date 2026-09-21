"""Tests for the bounded SkillOpt experiment contract."""

from skillopt_contract import ExperimentConfig, build_contract, evaluate_results


FIXED_ROUTE = {
    "model": "auto/best-free",
    "task_role": "coder",
    "profile": "student-coder",
    "verifier": "school-core/project_verify.yaml",
}


def _config(**overrides):
    values = {
        "task_family": "python-testing-smoke-proof",
        "baseline_skill": "student-coder-v1",
        "candidate_skill": "student-coder-v2",
        "fixed_route": FIXED_ROUTE,
        "training_tasks": ("train-1", "train-2"),
        "holdout_tasks": ("holdout-1", "holdout-2", "holdout-3"),
        "cost_budget": 100,
    }
    values.update(overrides)
    return ExperimentConfig(**values)


def _row(variant, split, quality, *, fallback=None, failure_edge="none", rework=False):
    return {
        "variant": variant,
        "split": split,
        "fallback_reason": fallback,
        "rework": rework,
        "outcome": {
            "lifecycle": "completed",
            "quality": quality,
            "failure_edge": failure_edge,
            "failure_mode": "none" if failure_edge == "none" else "quality",
            "next_action": "close",
            "fallback_reason": fallback,
        },
    }


def test_valid_contract_is_ready_and_reproducibly_identified():
    contract = build_contract(_config())

    assert contract["status"] == "ready"
    assert contract["blockers"] == []
    assert contract["execution_policy"]["same_model_route"] is True
    assert contract["execution_policy"]["no_fabricated_lift"] is True
    assert contract["experiment_id"].startswith("skillopt-")


def test_missing_controls_block_before_any_run():
    contract = build_contract(_config(holdout_tasks=("holdout-1",)))

    assert contract["status"] == "blocked"
    assert "holdout_too_small" in contract["blockers"]
    assert evaluate_results(contract, []) == {
        "status": "blocked",
        "reason": "invalid_or_blocked_contract",
    }


def test_heldout_evidence_is_required_before_claiming_lift():
    contract = build_contract(_config())
    results = [_row("baseline", "training", 0.8), _row("candidate", "training", 0.9)]

    report = evaluate_results(contract, results)

    assert report["status"] == "blocked"
    assert report["reason"] == "heldout_evidence_missing"
    assert "deltas" not in report


def test_candidate_is_accepted_only_when_all_quality_and_failure_metrics_hold():
    contract = build_contract(_config())
    results = [
        _row("baseline", "training", 0.80, fallback="timeout", rework=True),
        _row("candidate", "training", 0.90),
        _row("baseline", "holdout", 0.80, fallback="timeout", rework=True),
        _row("candidate", "holdout", 0.90),
        _row("baseline", "holdout", 0.80),
        _row("candidate", "holdout", 0.90),
        _row("baseline", "holdout", 0.80),
        _row("candidate", "holdout", 0.90),
    ]

    report = evaluate_results(contract, results)

    assert report["status"] == "accepted"
    assert report["deltas"]["quality"] == 0.1
    assert report["acceptance"] == {
        "quality_non_decrease": True,
        "contract_failure_rate_non_increase": True,
        "fallback_rate_non_increase": True,
        "rework_rate_non_increase": True,
    }
    assert report["no_fabricated_lift"] is True


def test_candidate_rejected_when_quality_improves_but_contract_failures_worsen():
    contract = build_contract(_config())
    results = [
        _row("baseline", "holdout", 0.80),
        _row("candidate", "holdout", 0.90, failure_edge="task_contract"),
        _row("baseline", "holdout", 0.80),
        _row("candidate", "holdout", 0.90, failure_edge="task_contract"),
        _row("baseline", "holdout", 0.80),
        _row("candidate", "holdout", 0.90, failure_edge="task_contract"),
    ]

    report = evaluate_results(contract, results)

    assert report["status"] == "rejected"
    assert report["acceptance"]["quality_non_decrease"] is True
    assert report["acceptance"]["contract_failure_rate_non_increase"] is False
