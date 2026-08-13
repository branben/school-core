"""F7: growth-aware routing remains observational until proven offline."""

import json

from shadow_routing import build_shadow_evidence, load_shadow_history


def _record(
    agent,
    score,
    difficulty="medium",
    *,
    status="success",
    retry_count=0,
    confidence=None,
    review_packet=None,
    capability=None,
    tool_usage=None,
):
    record = {
        "agent": agent,
        "score": score,
        "status": status,
        "difficulty": difficulty,
        "retry_count": retry_count,
    }
    if confidence is not None:
        record["confidence"] = confidence
    if review_packet is not None:
        record["review_packet"] = review_packet
    if capability is not None:
        record["capability"] = capability
    if tool_usage is not None:
        record["tool_usage"] = tool_usage
    return record


def test_shadow_packet_reports_growth_difficulty_retry_and_confidence_trends():
    history = [
        _record("coder-a", 40, "easy", retry_count=1, confidence=0.4),
        _record("coder-a", 60, "medium", confidence=0.6),
        _record("coder-a", 80, "hard", confidence=0.8),
    ]

    packet = build_shadow_evidence(
        history,
        current=_record("coder-a", 90, "hard", confidence=0.9),
        candidates=["coder-a", "coder-b"],
    )

    assert packet["schema_version"] == 1
    assert packet["mode"] == "shadow"
    assert packet["live_routing_unchanged"] is True
    assert packet["growth"]["samples"] == 4
    assert packet["growth"]["slope"] > 0
    assert packet["difficulty"]["hard"]["attempted"] == 2
    assert packet["difficulty"]["hard"]["succeeded"] == 2
    assert packet["retry"]["retried"] == 1
    assert packet["confidence"]["slope"] > 0
    assert packet["recommendation"]["current_agent"] == "coder-a"


def test_shadow_packet_keeps_sparse_history_safe_and_deterministic():
    packet = build_shadow_evidence(
        [],
        current=_record(
            "coder-a",
            70,
            review_packet={
                "judges": {
                    "cto": {"verdict": "PASS", "score": 75, "findings": []},
                    "coo": {"verdict": "FAIL", "score": 40, "findings": [{"severity": "HIGH"}]},
                }
            },
            capability={"allowed_tools": ["python", "git"]},
        ),
        candidates=["coder-b", "coder-a"],
    )

    assert packet["insufficient_data"] is True
    assert packet["recommendation"]["recommended_agent"] == "coder-a"
    assert packet["skills"]["cto"]["samples"] == 1
    assert packet["skills"]["coo"]["critical_or_high_findings"] == 1
    assert packet["tools"]["offered"] == ["git", "python"]
    assert packet["tools"]["used"] == []
    assert packet["tools"]["usage_evidence"] == "absent"


def test_shadow_packet_never_infers_tool_use_and_redacts_untrusted_text():
    packet = build_shadow_evidence(
        [],
        current=_record(
            "coder-a",
            80,
            capability={"allowed_tools": ["python", "not-allowed", "/Users/private/x"]},
        ),
    )

    assert packet["tools"]["used"] == []
    assert packet["tools"]["usage_evidence"] == "absent"
    assert packet["tools"]["offered"] == ["not-allowed", "python"]
    assert "/Users/private" not in str(packet)
    assert len(packet["tools"]["offered"]) <= 16


def test_shadow_history_is_bounded_and_ignores_malformed_entries(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text(json.dumps([None, *({"score": i} for i in range(300))]))

    history = load_shadow_history(path, limit=999)

    assert len(history) == 256
    assert all(isinstance(item, dict) for item in history)
    assert history[0]["score"] == 44
    assert history[-1]["score"] == 299


def test_shadow_history_malformed_state_is_empty(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text("not json")

    assert load_shadow_history(path) == []


def test_shadow_packet_extracts_proven_tool_invocations_only():
    packet = build_shadow_evidence(
        [],
        current=_record(
            "coder-a",
            80,
            capability={"allowed_tools": ["python", "git"]},
            # Runtime evidence, unlike the capability declaration.
            tool_usage={"proven": True, "used": ["python", "python", "shell"]},
        ),
    )

    assert packet["tools"]["used"] == ["python", "shell"]
    assert packet["tools"]["usage_evidence"] == "proven"
