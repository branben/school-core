"""Tests for the additive plan/bead/runtime evidence join."""

import json

from evidence_join import build_evidence_join


def test_join_preserves_plan_bead_runtime_and_review_identity():
    packet = build_evidence_join(
        control={
            "route_id": "route-1",
            "bd_id": "school-core-abc",
            "plan_id": "docs/plans/example.md",
            "plan_unit": "U4",
            "wayfinder_id": "school-core-wayfinder-v1",
            "primary_workflow": "ce-work",
        },
        runtime={
            "dispatcher": "direct-orca",
            "orca_worktree_id": "study-coder-1234",
            "hermes_session_id": "hermes-1",
        },
        artifact={
            "repository": "owner/repo",
            "trajectory_ref": "data/trajectories/run.json",
            "changed_files": ["package.json", "project_verify.yaml"],
        },
        verification={
            "project_gate": "pass",
            "entire_status": "findings",
            "entire_finding_count": 2,
        },
        judgment={
            "cto_verdict": "PASS",
            "coo_verdict": "PASS",
            "accepted": True,
            "score": 91,
        },
        outcome={
            "lifecycle": "completed",
            "quality": 0.91,
            "failure_edge": "none",
            "failure_mode": "none",
            "next_action": "close",
        },
    )

    assert packet["schema_version"] == 1
    assert packet["control"]["bd_id"] == "school-core-abc"
    assert packet["control"]["plan_unit"] == "U4"
    assert packet["runtime"]["orca_worktree_id"] == "study-coder-1234"
    assert packet["artifact"]["changed_files"] == ["package.json", "project_verify.yaml"]
    assert packet["verification"]["entire_finding_count"] == 2
    assert packet["judgment"]["accepted"] is True
    assert packet["outcome"]["quality"] == 0.91


def test_join_is_fixed_shape_and_redacts_sensitive_values():
    packet = build_evidence_join(
        control={"bd_id": "bd-1", "plan_id": "/Users/brandon/private-plan.md"},
        runtime={"hermes_session_id": "Bearer secret-token-value"},
        artifact={
            "trajectory_ref": "/home/brandon/secret.json",
            "changed_files": ["project_verify.yaml", "/Users/brandon/secret.txt"],
        },
    )

    assert set(packet) == {
        "schema_version", "control", "runtime", "artifact",
        "verification", "judgment", "outcome",
    }
    serialized = json.dumps(packet)
    assert "secret-token-value" not in serialized
    assert "/Users/" not in serialized
    assert "/home/" not in serialized
    assert packet["control"]["bd_id"] == "bd-1"
    assert packet["artifact"]["changed_files"] == ["project_verify.yaml", "~/secret.txt"]
    assert packet["control"]["plan_unit"] is None
    assert packet["judgment"]["accepted"] is None
