"""Tests for the U7 trust envelope (scripts/trust_envelope.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from trust_envelope import classify  # noqa: E402


def test_low_risk_ready_for_agent_auto_applies():
    decision = classify("Add missing dark-mode CSS variable", ["ready-for-agent"])
    assert decision["decision"] == "auto-apply"
    assert decision["risk"] == "low"


def test_high_risk_pauses_even_when_ready():
    decision = classify(
        "Add auth rate limiting to the login endpoint",
        ["ready-for-agent"],
    )
    assert decision["decision"] == "human-approve"
    assert decision["risk"] == "high"


def test_security_title_is_high_risk():
    decision = classify("[security] bump react-router to v7", ["ready-for-agent"])
    assert decision["risk"] == "high"
    assert decision["decision"] == "human-approve"


def test_not_ready_for_agent_is_paused():
    decision = classify("Fix toolbar layout", [])
    assert decision["decision"] == "human-approve"
    assert "ready" in decision["reason"]


def test_labels_are_case_insensitive():
    decision = classify("Refactor banner component", ["Ready-For-Agent", "t-bug"])
    assert decision["decision"] == "auto-apply"


def test_body_not_required():
    decision = classify("Bump lodash on dev deps", ["ready-for-agent"], body="")
    assert decision["decision"] == "auto-apply"


def test_curriculum_keyword_is_high_risk():
    decision = classify("Modify curriculum grading thresholds", ["ready-for-agent"])
    assert decision["risk"] == "high"
    assert decision["decision"] == "human-approve"