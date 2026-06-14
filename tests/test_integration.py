"""
Tests for U4: Director hooks + CLI sleep/wake commands.

Run: python -m pytest tests/test_integration.py -v
"""

import json
from pathlib import Path

import pytest

from director import sleep, wake, _active_sessions, _track_session_start, _track_session_activity, SLEEP_TIMEOUT_MINUTES
from sleep_state import load_session, read_library_log, SessionNotFoundError
from scoring import ScoreStore


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect all session paths to a temp directory."""
    sessions_dir = tmp_path / "sessions"
    consolidation_dir = sessions_dir / "consolidation"
    monkeypatch.setattr("sleep_state.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("sleep_state.CONSOLIDATION_DIR", consolidation_dir)
    monkeypatch.setattr("sleep_state.LIBRARY_LOG_PATH", sessions_dir / "library_log.yaml")
    return sessions_dir


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Create a ScoreStore with a temp scores.json."""
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(json.dumps({
        "foundry-coder-7b": {"_default": 25.0, "python-testing": 65.4},
    }))
    return ScoreStore(file_path=str(scores_file))


@pytest.fixture(autouse=True)
def clear_sessions(tmp_sessions_dir):
    """Clear active sessions and session files before each test."""
    _active_sessions.clear()
    yield
    _active_sessions.clear()
    # Clean up session files
    for f in tmp_sessions_dir.rglob("*"):
        if f.is_file():
            f.unlink()


# ── Director sleep/wake ──────────────────────────────────────────────────────

class TestDirectorSleepWake:
    def test_sleep_command(self, tmp_sessions_dir, store):
        """sleep command persists state, exits cleanly."""
        result = sleep(session_id="ses_cmd_001", agent="foundry-coder-7b", store=store)
        assert result["state"].session_id == "ses_cmd_001"
        # JSON saved
        assert (tmp_sessions_dir / "ses_cmd_001.json").exists()

    def test_wake_command(self, tmp_sessions_dir, store):
        """wake command restores state, resumes."""
        sleep(session_id="ses_cmd_002", agent="foundry-coder-7b", store=store)
        result = wake(session_id="ses_cmd_002")
        assert result["state"].agent == "foundry-coder-7b"

    def test_wake_no_session_prints_message(self, tmp_sessions_dir):
        """wake with no session prints 'No session found'."""
        with pytest.raises(SessionNotFoundError):
            wake(session_id="nonexistent_session")


# ── Session Tracking ─────────────────────────────────────────────────────────

class TestSessionTracking:
    def test_track_session_start(self):
        """Session tracking registers active session."""
        _track_session_start("ses_track_001", "foundry-coder-7b", "default")
        assert "ses_track_001" in _active_sessions
        assert _active_sessions["ses_track_001"]["agent"] == "foundry-coder-7b"

    def test_track_session_activity(self):
        """Session activity is recorded."""
        _track_session_start("ses_track_002", "foundry-coder-7b")
        _track_session_activity("ses_track_002", {"status": "success", "domain": "python-testing"})
        assert _active_sessions["ses_track_002"]["tasks_completed"] == 1
        assert len(_active_sessions["ses_track_002"]["episodic_history"]) == 1

    def test_multiple_sessions(self):
        """Multiple sessions tracked independently."""
        _track_session_start("ses_multi_a", "foundry-coder-7b")
        _track_session_start("ses_multi_b", "foundry-coder-1.5b")
        assert len(_active_sessions) == 2
        assert _active_sessions["ses_multi_a"]["agent"] == "foundry-coder-7b"
        assert _active_sessions["ses_multi_b"]["agent"] == "foundry-coder-1.5b"


# ── Full Lifecycle ──────────────────────────────────────────────────────────

class TestFullLifecycle:
    def test_sleep_then_wake_then_sleep(self, tmp_sessions_dir, store):
        """Full lifecycle: sleep → wake → sleep again."""
        r1 = sleep(session_id="ses_life_001", agent="foundry-coder-7b", store=store)
        assert r1["state"].session_id == "ses_life_001"

        w1 = wake("ses_life_001")
        assert w1["state"].agent == "foundry-coder-7b"

        # Re-sleep (simulate second cycle)
        r2 = sleep(session_id="ses_life_001", agent="foundry-coder-7b", store=store)
        assert r2["state"].session_id == "ses_life_001"

    def test_library_log_complete(self, tmp_sessions_dir, store):
        """Sleep + wake entries both in library log."""
        sleep(session_id="ses_log_001", agent="foundry-coder-7b", store=store)
        wake("ses_log_001")
        entries = read_library_log()
        events = [e["event"] for e in entries]
        assert "sleep" in events
        assert "wake" in events


# ── Configuration ────────────────────────────────────────────────────────────

class TestConfiguration:
    def test_default_timeout(self):
        """Default sleep timeout is 15 minutes."""
        assert SLEEP_TIMEOUT_MINUTES == 15

    def test_default_pressure_threshold(self):
        """Default context pressure threshold is 70%."""
        from director import SLEEP_CONTEXT_PRESSURE_THRESHOLD
        assert SLEEP_CONTEXT_PRESSURE_THRESHOLD == 0.70
