"""Focused coverage for pure issue-bridge orchestration seams."""

from bridge_seams import (
    build_shadow_routing_packet,
    observability_fields,
    strict_gate_failure,
)


def test_observability_fields_preserves_only_bounded_evidence_keys():
    capability = {"task_role": "coder", "allowed_tools": ["python"]}
    evidence = {"accepted": True}

    assert observability_fields({
        "capability": capability,
        "teacher_evidence": evidence,
        "response": "must not persist",
        "prompt": "must not persist",
    }) == {
        "capability": capability,
        "teacher_evidence": evidence,
    }


def test_shadow_packet_seam_rebounds_oversized_legacy_history():
    history = [{"agent": "old", "score": index, "status": "success"} for index in range(400)]

    packet = build_shadow_routing_packet(
        {"agent": "current", "status": "success"},
        {"difficulty": "medium"},
        80.0,
        0,
        history,
        ["current"],
    )

    assert packet["samples"] == 256
    assert packet["mode"] == "shadow"
    assert packet["live_routing_unchanged"] is True


def test_strict_gate_failure_seam_preserves_fail_closed_contract():
    result = strict_gate_failure("toolchain unavailable")

    assert result["passed"] is False
    assert result["skipped"] is False
    assert result["strict_escalated"] is True
    assert result["ran"] == 0
    assert "toolchain unavailable" in result["failures"][0]["stderr"]
    assert "compiler-before-critic" in result["failures"][0]["stderr"]
