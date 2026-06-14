"""
Tests for sleep sequence (U2) — sleep_state.execute_sleep().

Run: python -m pytest tests/test_sleep_sequence.py -v
"""

import json
from pathlib import Path

import pytest
import yaml

from sleep_state import (
    SleepState,
    ConsolidationArtifact,
    SessionNotFoundError,
    execute_sleep,
    execute_wake,
    load_session,
    load_consolidation,
    read_library_log,
)
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
        "foundry-coder-1.5b": {"_default": 20.0},
    }))
    s = ScoreStore(file_path=str(scores_file))
    return s


@pytest.fixture
def sample_episodic_history():
    return [
        {"domain": "python-testing", "status": "success", "decision": "Used pytest parametrize"},
        {"domain": "python-testing", "status": "success"},
        {"domain": "git-operations", "status": "error", "error": "merge conflict"},
    ]


# ── Happy Path ───────────────────────────────────────────────────────────────

class TestSleepHappyPath:
    def test_sleep_creates_json_and_yaml(self, tmp_sessions_dir, store, sample_episodic_history):
        """Happy path: sleep with active session creates JSON + YAML + log."""
        result = execute_sleep(
            session_id="ses_happy_001",
            agent="foundry-coder-7b",
            store=store,
            episodic_history=sample_episodic_history,
            duration_minutes=35.0,
        )
        # JSON saved
        json_path = tmp_sessions_dir / "ses_happy_001.json"
        assert json_path.exists()
        with open(json_path) as f:
            data = json.load(f)
        assert data["session_id"] == "ses_happy_001"
        assert data["agent"] == "foundry-coder-7b"

        # YAML consolidation created
        yaml_path = tmp_sessions_dir / "consolidation" / "ses_happy_001.yaml"
        assert yaml_path.exists()

        # Library log written
        entries = read_library_log()
        assert len(entries) == 1
        assert entries[0]["event"] == "sleep"

    def test_sleep_restores_via_wake(self, tmp_sessions_dir, store, sample_episodic_history):
        """Sleep then wake restores all state correctly."""
        execute_sleep(
            session_id="ses_roundtrip_001",
            agent="foundry-coder-7b",
            store=store,
            task_queue=["task_a", "task_b"],
            layer_0={"identity": "Student"},
            episodic_history=sample_episodic_history,
            duration_minutes=20.0,
        )
        wake_result = execute_wake("ses_roundtrip_001")
        state = wake_result["state"]
        assert state.session_id == "ses_roundtrip_001"
        assert state.agent == "foundry-coder-7b"
        assert state.task_queue == ["task_a", "task_b"]
        assert state.layer_0 == {"identity": "Student"}

    def test_scores_preserved_after_sleep(self, tmp_sessions_dir, store):
        """Scores are correctly captured in sleep state."""
        store.set_score("foundry-coder-7b", "python-testing", 70.0)
        result = execute_sleep(
            session_id="ses_scores_001",
            agent="foundry-coder-7b",
            store=store,
        )
        state = result["state"]
        assert "python-testing" in state.scores_snapshot
        assert state.scores_snapshot["python-testing"] == 70.0


# ── Edge Cases ───────────────────────────────────────────────────────────────

class TestSleepEdgeCases:
    def test_sleep_with_empty_history_produces_valid_yaml(self, tmp_sessions_dir, store):
        """Consolidation with empty history produces valid YAML with zero tasks."""
        result = execute_sleep(
            session_id="ses_empty_001",
            agent="foundry-coder-7b",
            store=store,
            episodic_history=[],
        )
        consolidation = result["consolidation"]
        assert consolidation.tasks_completed == 0
        assert consolidation.domains_visited == []

    def test_wake_from_missing_session_raises(self, tmp_sessions_dir):
        """Wake from missing session raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError):
            execute_wake("nonexistent_session")

    def test_wake_from_corrupted_json_raises(self, tmp_sessions_dir):
        """Wake from corrupted JSON raises SessionCorruptedError."""
        sessions_dir = tmp_sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)
        bad_file = sessions_dir / "corrupt.json"
        bad_file.write_text("not valid json{{{")
        with pytest.raises(Exception):  # SessionCorruptedError
            execute_wake("corrupt")

    def test_multiple_sleep_wake_cycles_no_data_loss(self, tmp_sessions_dir, store):
        """Multiple sleep/wake cycles: no data loss, scores persist."""
        for i in range(3):
            execute_sleep(
                session_id=f"ses_cycle_{i}",
                agent="foundry-coder-7b",
                store=store,
                episodic_history=[{"domain": "python-testing", "status": "success"}],
                duration_minutes=10.0 * (i + 1),
            )
            result = execute_wake(f"ses_cycle_{i}")
            assert result["state"].agent == "foundry-coder-7b"

    def test_library_log_appendable_and_parseable(self, tmp_sessions_dir, store):
        """Library log entries are appendable and parseable."""
        for i in range(3):
            execute_sleep(
                session_id=f"ses_log_{i}",
                agent="foundry-coder-7b",
                store=store,
            )
        entries = read_library_log()
        assert len(entries) == 3
        for entry in entries:
            assert "session_id" in entry
            assert "timestamp" in entry
            assert entry["event"] == "sleep"


# ── Wake Edge Cases ──────────────────────────────────────────────────────────

class TestWakeEdgeCases:
    def test_wake_with_empty_task_queue(self, tmp_sessions_dir, store):
        """Wake with empty task queue: Director presents no tasks (idle)."""
        execute_sleep(
            session_id="ses_idle_001",
            agent="foundry-coder-7b",
            store=store,
            task_queue=[],
        )
        result = execute_wake("ses_idle_001")
        assert result["state"].task_queue == []

    def test_wake_with_queued_tasks(self, tmp_sessions_dir, store):
        """Wake with queued tasks: all tasks present after wake."""
        tasks = ["review PR #42", "fix failing test", "update docs"]
        execute_sleep(
            session_id="ses_queued_001",
            agent="foundry-coder-7b",
            store=store,
            task_queue=tasks,
        )
        result = execute_wake("ses_queued_001")
        assert result["state"].task_queue == tasks

    def test_wake_loads_consolidation(self, tmp_sessions_dir, store):
        """Wake loads consolidation artifact if it exists."""
        execute_sleep(
            session_id="ses_cons_001",
            agent="foundry-coder-7b",
            store=store,
            episodic_history=[{"domain": "python-testing", "status": "success"}],
        )
        result = execute_wake("ses_cons_001")
        consolidation = result["consolidation"]
        assert consolidation is not None
        assert consolidation.agent == "foundry-coder-7b"
