"""Structural checks for the GitHub Actions school-loop workflow."""

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "school-loop.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_school_loop_serializes_runs_without_cancelling_active_work():
    workflow = _workflow()
    assert workflow["concurrency"] == {
        "group": "school-loop",
        "cancel-in-progress": False,
    }


def test_blocker_alert_is_isolated_from_gate_and_board_publish():
    workflow = _workflow()
    alert = workflow["jobs"]["pipeline-alert"]
    assert alert["needs"] == "gate"
    assert alert["continue-on-error"] is True
    assert "needs.gate.outputs.runner_online == 'false'" in alert["if"]
    assert workflow["jobs"]["loop"]["if"] == "always()"


def test_missing_verify_toolchain_fails_execute_job():
    workflow = _workflow()
    step = next(
        step
        for step in workflow["jobs"]["execute"]["steps"]
        if step.get("name") == "Verify toolchain preflight (nix + verifyShell)"
    )
    assert "::error::nix not found" in step["run"]
    assert "::error::flake.nix#verifyShell did not evaluate" in step["run"]
    assert step["run"].count("exit 1") == 2


def test_workflow_preflight_is_distinct_from_library_soft_skip():
    workflow = _workflow()
    step = next(
        step
        for step in workflow["jobs"]["execute"]["steps"]
        if step.get("name") == "Verify toolchain preflight (nix + verifyShell)"
    )
    assert "cannot run verify gate" in step["run"]
    steps = workflow["jobs"]["execute"]["steps"]
    preflight_index = next(
        i for i, item in enumerate(steps)
        if item.get("name") == "Verify toolchain preflight (nix + verifyShell)"
    )
    bridge_index = next(
        i for i, item in enumerate(steps)
        if item.get("name") == "Run bridge loop (executes issues)"
    )
    assert preflight_index < bridge_index
    # The hosted board job does not depend on execute success.
    assert workflow["jobs"]["loop"]["if"] == "always()"
