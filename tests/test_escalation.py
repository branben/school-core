"""
Tests for U8: "I Don't Know" escalation pre-dispatch confidence check.

Run: python -m pytest tests/test_escalation.py -v
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from director import run_task, _check_readiness, _get_threshold, _load_escalation_thresholds
from escalation_log import EscalationLog
from scoring import ScoreStore


@pytest.fixture
def tmp_scores(tmp_path):
    # Seed a REAL executor role (coder) so run_task's call_model role
    # validation passes. (agent-a/agent-b are not valid COMBO_MAP roles.)
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(json.dumps({
        "coder": {"_default": 55.0, "python-testing": 60.0},
        "reviewer": {"_default": 50.0, "python-testing": 55.0},
    }))
    return ScoreStore(file_path=str(scores_file))


@pytest.fixture
def tmp_escalation_log(tmp_path, monkeypatch):
    log_path = tmp_path / "escalation_log.json"
    monkeypatch.setattr("escalation_log.LOG_PATH", log_path)
    return str(log_path)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    config_path = tmp_path / "escalation_thresholds.yaml"
    config_path.write_text(
        "thresholds:\n"
        "  easy: 3\n"
        "  medium: 5\n"
        "  hard: 7\n"
        "  diploma: 8\n"
        "domain_overrides:\n"
        "  code-review:\n"
        "    hard: 8\n"
        "    diploma: 9\n"
    )
    return config_path


class TestCheckReadiness:
    def test_high_confidence(self):
        with patch("director.call_model", return_value="8"):
            result = _check_readiness("agent-a", "_default", "easy", "some prompt")
            assert result == 8.0

    def test_numeric_with_text(self):
        with patch("director.call_model", return_value="7 out of 10"):
            result = _check_readiness("agent-a", "_default", "easy", "some prompt")
            assert result == 7.0

    def test_invalid_response(self):
        with patch("director.call_model", return_value="I don't know"):
            result = _check_readiness("agent-a", "_default", "easy", "some prompt")
            assert result == 0.0

    def test_timeout_or_exception(self):
        with patch("director.call_model", side_effect=Exception("timeout")):
            result = _check_readiness("agent-a", "_default", "easy", "some prompt")
            assert result == 0.0


class TestThresholdConfig:
    def test_default_thresholds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "director.Path",
            lambda *args, **kwargs: tmp_path if "escalation" in str(args) else Path(*args, **kwargs)
        )
        monkeypatch.setattr("director._load_escalation_thresholds", lambda: {"easy": 3, "medium": 5, "hard": 7, "diploma": 8})
        assert _get_threshold("_default", "easy") == 3.0
        assert _get_threshold("_default", "medium") == 5.0
        assert _get_threshold("_default", "hard") == 7.0
        assert _get_threshold("_default", "diploma") == 8.0

    def test_domain_overrides(self, tmp_config, monkeypatch):
        import director
        original_path = director.Path

        def mock_path(*args, **kwargs):
            if "escalation_thresholds.yaml" in str(args):
                return tmp_config
            return original_path(*args, **kwargs)

        monkeypatch.setattr("director.Path", mock_path, raising=False)
        assert _get_threshold("code-review", "hard") == 8.0
        assert _get_threshold("code-review", "diploma") == 9.0

    def test_overrides_dont_affect_other_domains(self, tmp_config, monkeypatch):
        import director
        original_path = director.Path

        def mock_path(*args, **kwargs):
            if "escalation_thresholds.yaml" in str(args):
                return tmp_config
            return original_path(*args, **kwargs)

        monkeypatch.setattr("director.Path", mock_path, raising=False)
        assert _get_threshold("python-testing", "hard") == 7.0


class TestDispatchHappyPath:
    def test_confidence_above_threshold_dispatched(self, tmp_scores, tmp_escalation_log):
        with patch("director.call_model", side_effect=["9", "task response"]):
            result = run_task(
                prompt="Write a function",
                domain="_default",
                difficulty="hard",
                store=tmp_scores,
            )
        assert result["status"] == "success"

    def test_confidence_equal_to_threshold_dispatched(self, tmp_scores):
        with patch("director.call_model", side_effect=["7", "task response"]):
            result = run_task(
                prompt="Write a function",
                domain="_default",
                difficulty="hard",
                store=tmp_scores,
            )
        assert result["status"] == "success"


class TestDispatchEscalation:
    def test_low_confidence_skips_to_next(self, tmp_scores):
        # coder (score=55) below diploma gate (75) + force_agent bypasses
        # route_task → readiness runs and coder declines (4 < 7) → escalates
        def mock_call_model(agent, prompt, system_prompt=None, timeout=None):
            if "confident" in prompt.lower():
                if agent == "coder":
                    return "4"
                return "8"
            return "task response"

        with patch("director.call_model", side_effect=mock_call_model):
            result = run_task(
                prompt="Write a function",
                domain="_default",
                difficulty="diploma",
                store=tmp_scores,
                force_agent="coder",
            )
        assert result["status"] == "success"
        assert result["agent"] == "openhands"
        assert result["escalation"] is True

    def test_all_decline_falls_to_a2a(self, tmp_scores):
        # coder (score=55) below diploma gate (75) → readiness runs → escalation
        def mock_call_model(agent, prompt, system_prompt=None, timeout=None):
            if agent == "openhands":
                return "a2a response"
            if "confident" in prompt.lower():
                return "1"
            return "normal response"

        with patch("director.call_model", side_effect=mock_call_model):
            result = run_task(
                prompt="Write a function",
                domain="_default",
                difficulty="diploma",
                store=tmp_scores,
                force_agent="coder",
            )
        assert result["status"] == "success"
        assert result["agent"] == "openhands"
        assert result["escalation"] is True

    def test_qualified_agent_skips_readiness_check(self, tmp_scores):
        """When the agent qualifies for the difficulty gate (score >= gate),
        the readiness check is skipped — no escalation even if mock would
        return low confidence."""
        def mock_call_model(agent, prompt, system_prompt=None, timeout=None):
            if "confident" in prompt.lower():
                return "1"  # would fail if checked
            return "task response"

        with patch("director.call_model", side_effect=mock_call_model):
            result = run_task(
                prompt="Write a function",
                domain="_default",
                difficulty="hard",
                store=tmp_scores,
            )
        assert result["status"] == "success"
        # coder (score=55) qualifies for hard (gate=50), so readiness is
        # skipped — the low readiness mock value is never called.
        assert result["agent"] == "coder"
        assert result["escalation"] is False


class TestEscalationLogUnit:
    def test_log_event(self, tmp_escalation_log):
        log = EscalationLog(log_path=tmp_escalation_log)
        log.log("agent-a", "_default", "hard", 4.0, 7.0, "agent-b")
        entries = log.all_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["agent"] == "agent-a"
        assert entry["domain"] == "_default"
        assert entry["difficulty"] == "hard"
        assert entry["confidence"] == 4.0
        assert entry["threshold"] == 7.0
        assert entry["escalated_to"] == "agent-b"
        assert "timestamp" in entry

    def test_rate_calculation(self, tmp_escalation_log):
        log = EscalationLog(log_path=tmp_escalation_log)
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        for i in range(5):
            entry = {
                "timestamp": (now - timedelta(days=i)).isoformat(),
                "agent": "agent-a",
                "domain": "_default",
                "difficulty": "hard",
                "confidence": 3.0,
                "threshold": 7.0,
                "escalated_to": "next_candidate",
            }
            log._entries.append(entry)
        log._entries.append({
            "timestamp": now.isoformat(),
            "agent": "other",
            "domain": "_default",
            "difficulty": "easy",
            "confidence": 8.0,
            "threshold": 3.0,
            "escalated_to": "none",
        })
        rate = log.get_rate("agent-a", days=7)
        assert rate == 5 / 6

    def test_all_rates(self, tmp_escalation_log):
        log = EscalationLog(log_path=tmp_escalation_log)
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        for _ in range(3):
            log._entries.append({
                "timestamp": now.isoformat(),
                "agent": "agent-a",
                "domain": "_default",
                "difficulty": "hard",
                "confidence": 3.0,
                "threshold": 7.0,
                "escalated_to": "next",
            })
        for _ in range(2):
            log._entries.append({
                "timestamp": now.isoformat(),
                "agent": "agent-b",
                "domain": "_default",
                "difficulty": "medium",
                "confidence": 4.0,
                "threshold": 5.0,
                "escalated_to": "next",
            })
        rates = log.get_all_rates(days=7)
        assert rates["agent-a"] == 0.6
        assert rates["agent-b"] == 0.4
