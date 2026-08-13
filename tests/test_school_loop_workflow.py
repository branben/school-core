"""Structural checks for the GitHub Actions school-loop workflow."""

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "school-loop.yml"
CI_WORKFLOW = WORKFLOW.with_name("ci.yml")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _ci_workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_hosted_matrix_excludes_live_tests_by_marker():
    ci = _ci_workflow()
    test_step = next(step for step in ci["jobs"]["test"]["steps"] if step.get("name") == "Run tests")

    assert '-m "not live"' in test_step["run"]


def test_self_hosted_integration_selects_live_tests_explicitly():
    ci = _ci_workflow()
    integration = ci["jobs"]["integration"]
    test_step = next(
        step for step in integration["steps"]
        if step.get("name") == "Run live-Orca + OmniRoute integration tests"
    )

    assert integration["env"]["ORCA_LIVE_TESTS"] == "1"
    assert "-m live" in test_step["run"]
    assert "test_orca_execution.py" in test_step["run"]


def test_school_loop_serializes_runs_without_cancelling_active_work():
    workflow = _workflow()
    assert workflow["concurrency"] == {
        "group": "school-loop",
        "cancel-in-progress": False,
    }


def test_live_orca_jobs_share_a_cross_workflow_lock():
    """CI integration and School Loop execute must never use Orca together."""
    school_loop = _workflow()
    ci = _ci_workflow()
    expected = {
        "group": "school-core-live-orca",
        "cancel-in-progress": False,
    }
    assert school_loop["jobs"]["execute"]["concurrency"] == expected
    assert ci["jobs"]["integration"]["concurrency"] == expected


def test_default_github_tokens_are_read_only_and_writes_are_job_scoped():
    school_loop = _workflow()
    ci = _ci_workflow()

    assert ci["permissions"] == {"contents": "read"}
    assert school_loop["permissions"] == {"contents": "read"}
    assert school_loop["jobs"]["execute"]["permissions"] == {
        "contents": "write",
        "issues": "write",
    }
    assert school_loop["jobs"]["loop"]["permissions"] == {
        "contents": "write",
        "issues": "read",
    }


def _paths_containing(value, needle: str, path=()):
    """Return parsed-YAML paths whose string values contain ``needle``."""
    if isinstance(value, dict):
        matches = []
        for key, child in value.items():
            matches.extend(_paths_containing(child, needle, path + (str(key),)))
        return matches
    if isinstance(value, list):
        matches = []
        for index, child in enumerate(value):
            matches.extend(_paths_containing(child, needle, path + (str(index),)))
        return matches
    if isinstance(value, str) and needle in value:
        return [path]
    return []


def test_runner_admin_token_is_used_only_by_runner_liveness_gates():
    school_loop = _workflow()
    ci = _ci_workflow()
    expected = "${{ secrets.RUNNER_ADMIN_TOKEN }}"

    ci_gate = ci["jobs"]["integration-gate"]["steps"][0]
    school_gate = school_loop["jobs"]["gate"]["steps"][0]
    assert ci_gate["env"]["GH_TOKEN"] == expected
    assert school_gate["env"]["GH_TOKEN"] == expected
    assert "actions/runners" in ci_gate["run"]
    assert "actions/runners" in school_gate["run"]

    # Keep the secret confined to the two explicit GH_TOKEN bindings. This
    # recursively scans parsed YAML rather than only checking selected job
    # environments, so a future step-level leak is caught.
    assert _paths_containing(ci, expected) == [
        ("jobs", "integration-gate", "steps", "0", "env", "GH_TOKEN"),
    ]
    assert _paths_containing(school_loop, expected) == [
        ("jobs", "gate", "steps", "0", "env", "GH_TOKEN"),
    ]


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


def test_verify_preflight_eval_uses_full_devshell_attr_path():
    """Regression (2026-08-12): the preflight eval must use the full attr path.

    verifyShell is a devShell (devShells.aarch64-darwin.verifyShell). The
    `.#verifyShell.name` shorthand resolves for `nix develop` but NOT for
    `nix eval`, so the old check always returned empty and hard-failed every
    live cycle after the U3 preflight landed.
    """
    workflow = _workflow()
    step = next(
        step
        for step in workflow["jobs"]["execute"]["steps"]
        if step.get("name") == "Verify toolchain preflight (nix + verifyShell)"
    )
    assert "#devShells.aarch64-darwin.verifyShell.name" in step["run"]
    assert ".#verifyShell.name" not in step["run"]


def test_checkpoint_sanitizes_and_stages_only_owned_consolidations():
    workflow = _workflow()
    step = next(
        step
        for step in workflow["jobs"]["execute"]["steps"]
        if step.get("name") == "Sanitize + commit board state (durable, PII-free)"
    )
    command = step["run"]
    assert "--trim-consolidations" in command
    assert "data/sessions/consolidation/*/*.yaml" in command
    assert "unexpected non-YAML file under data/sessions/consolidation" in command
    assert "git add -f data/last_run.json" in command
    assert "git add -f data/sessions/consolidation\n" not in command


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


def test_u8_crew_dispatch_wired_into_execute_job():
    """U8: crew path is enabled in the live workflow, bounded per cycle.

    The bridge reads CREW_ENABLED once per cycle; the execute job must set it
    so the FirstMate -> Orca crew path runs before the direct model path, with
    a per-cycle cap that fits the 30-min job timeout.
    """
    workflow = _workflow()
    env = workflow["jobs"]["execute"]["env"]
    assert env.get("CREW_ENABLED") in ("1", 1), "crew dispatch must be on"
    assert int(env.get("CREW_MAX_PER_CYCLE", 1)) >= 1


def test_u8_crew_registry_is_checkpointed():
    """U8: data/crew_runs.json must survive the fresh checkout each cycle.

    The registry powers the cross-cycle in-flight skip + stale sweep; if it is
    not sanitized + committed, every cycle starts empty and an interrupted
    crew can double-spawn next cycle.
    """
    workflow = _workflow()
    step = next(
        step
        for step in workflow["jobs"]["execute"]["steps"]
        if step.get("name") == "Sanitize + commit board state (durable, PII-free)"
    )
    command = step["run"]
    assert "data/crew_runs.json" in command  # sanitizer input
    assert "data/crew_runs.json" in command.split("git add -f", 1)[1]  # staged
