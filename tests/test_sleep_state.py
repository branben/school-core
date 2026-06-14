"""
Tests for sleep_state.py — Sleep/Wake State Schema.

Run: python -m pytest tests/test_sleep_state.py -v
"""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from sleep_state import (
    SleepState,
    ConsolidationArtifact,
    SessionNotFoundError,
    SessionCorruptedError,
    save_session,
    load_session,
    save_consolidation,
    load_consolidation,
    append_library_log,
    read_library_log,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect SESSIONS_DIR to a temp directory."""
    sessions_dir = tmp_path / "sessions"
    consolidation_dir = sessions_dir / "consolidation"
    monkeypatch.setattr("sleep_state.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("sleep_state.CONSOLIDATION_DIR", consolidation_dir)
    monkeypatch.setattr("sleep_state.LIBRARY_LOG_PATH", sessions_dir / "library_log.yaml")
    return sessions_dir


@pytest.fixture
def sample_sleep_state():
    return SleepState(
        session_id="ses_test_001",
        agent="foundry-coder-7b",
        building="default",
        task_queue=["task_1", "task_2"],
        scores_snapshot={"python-testing": 65.4, "code-review": 40.5},
        layer_0={"identity": "Student", "curriculum_status": "python-testing: medium"},
        layer_2_summary="Completed 3 tasks in python-testing. Used pytest parametrize.",
        timestamp="2026-06-13T10:00:00+00:00",
    )


@pytest.fixture
def sample_consolidation():
    return ConsolidationArtifact(
        session_id="ses_test_001",
        agent="foundry-coder-7b",
        duration_minutes=35.0,
        tasks_completed=3,
        domains_visited=["python-testing", "git-operations"],
        key_decisions=["Chose pytest parametrize over unittest subTest"],
        patterns_observed=["Forgets mock setup after 3+ consecutive test runs"],
        failed_approaches=["Attempted to use unittest.mock.call_args on non-mock"],
        compressed_output_size_tokens=820,
    )


# ── SleepState Roundtrip ─────────────────────────────────────────────────────

class TestSleepStateRoundtrip:
    def test_roundtrip_no_data_loss(self, tmp_sessions_dir, sample_sleep_state):
        """SleepState serializes to JSON and back without data loss."""
        save_session(sample_sleep_state)
        loaded = load_session("ses_test_001")
        assert loaded.session_id == sample_sleep_state.session_id
        assert loaded.agent == sample_sleep_state.agent
        assert loaded.building == sample_sleep_state.building
        assert loaded.task_queue == sample_sleep_state.task_queue
        assert loaded.scores_snapshot == sample_sleep_state.scores_snapshot
        assert loaded.layer_0 == sample_sleep_state.layer_0
        assert loaded.layer_2_summary == sample_sleep_state.layer_2_summary
        assert loaded.timestamp == sample_sleep_state.timestamp

    def test_json_output_is_valid(self, tmp_sessions_dir, sample_sleep_state):
        """Saved JSON file is valid and parseable."""
        filepath = save_session(sample_sleep_state)
        with open(filepath) as f:
            data = json.load(f)
        assert data["session_id"] == "ses_test_001"
        assert data["agent"] == "foundry-coder-7b"

    def test_default_values(self):
        """SleepState has correct default values for optional fields."""
        state = SleepState(session_id="ses_default", agent="test-agent")
        assert state.building == "default"
        assert state.task_queue == []
        assert state.scores_snapshot == {}
        assert state.layer_0 == {}
        assert state.layer_2_summary == ""
        assert state.timestamp == ""


# ── Consolidation Artifact ───────────────────────────────────────────────────

class TestConsolidationArtifact:
    def test_yaml_output_has_all_fields(self, tmp_sessions_dir, sample_consolidation):
        """ConsolidationArtifact produces valid YAML with all required fields."""
        filepath = save_consolidation(sample_consolidation)
        with open(filepath) as f:
            data = yaml.safe_load(f)
        assert data["session_id"] == "ses_test_001"
        assert data["agent"] == "foundry-coder-7b"
        assert data["duration_minutes"] == 35.0
        assert data["tasks_completed"] == 3
        assert "python-testing" in data["domains_visited"]
        assert len(data["key_decisions"]) == 1
        assert data["compressed_output_size_tokens"] == 820

    def test_roundtrip(self, tmp_sessions_dir, sample_consolidation):
        """ConsolidationArtifact serializes to YAML and back without data loss."""
        save_consolidation(sample_consolidation)
        loaded = load_consolidation("ses_test_001")
        assert loaded.session_id == sample_consolidation.session_id
        assert loaded.agent == sample_consolidation.agent
        assert loaded.duration_minutes == sample_consolidation.duration_minutes
        assert loaded.tasks_completed == sample_consolidation.tasks_completed
        assert loaded.domains_visited == sample_consolidation.domains_visited
        assert loaded.key_decisions == sample_consolidation.key_decisions
        assert loaded.patterns_observed == sample_consolidation.patterns_observed
        assert loaded.failed_approaches == sample_consolidation.failed_approaches
        assert loaded.compressed_output_size_tokens == sample_consolidation.compressed_output_size_tokens

    def test_default_values(self):
        """ConsolidationArtifact has correct default values for list fields."""
        artifact = ConsolidationArtifact(session_id="ses_default", agent="test-agent")
        assert artifact.duration_minutes == 0.0
        assert artifact.tasks_completed == 0
        assert artifact.domains_visited == []
        assert artifact.key_decisions == []
        assert artifact.patterns_observed == []
        assert artifact.failed_approaches == []
        assert artifact.compressed_output_size_tokens == 0


# ── Error Handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_session_raises_not_found(self, tmp_sessions_dir):
        """Missing file on load raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError):
            load_session("nonexistent_session")

    def test_corrupted_json_raises_corrupted_error(self, tmp_sessions_dir):
        """Corrupted JSON raises SessionCorruptedError with session_id in message."""
        sessions_dir = tmp_sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)
        bad_file = sessions_dir / "corrupt_session.json"
        bad_file.write_text("{invalid json content!!!")
        with pytest.raises(SessionCorruptedError, match="corrupt_session"):
            load_session("corrupt_session")

    def test_missing_consolidation_raises_not_found(self, tmp_sessions_dir):
        """Missing consolidation file raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError):
            load_consolidation("nonexistent_session")


# ── Directory Creation ───────────────────────────────────────────────────────

class TestDirectoryCreation:
    def test_save_creates_sessions_dir(self, tmp_sessions_dir):
        """save_session creates data/sessions/ if it doesn't exist."""
        assert not tmp_sessions_dir.exists()
        state = SleepState(session_id="ses_create_dir", agent="test-agent")
        save_session(state)
        assert tmp_sessions_dir.exists()

    def test_save_creates_consolidation_dir(self, tmp_sessions_dir):
        """save_consolidation creates consolidation/ if it doesn't exist."""
        consolidation_dir = tmp_sessions_dir / "consolidation"
        assert not consolidation_dir.exists()
        artifact = ConsolidationArtifact(session_id="ses_create_dir", agent="test-agent")
        save_consolidation(artifact)
        assert consolidation_dir.exists()


# ── Library Log ──────────────────────────────────────────────────────────────

class TestLibraryLog:
    def test_append_and_read(self, tmp_sessions_dir):
        """append_library_log adds entries that are parseable."""
        append_library_log({
            "session_id": "ses_log_001",
            "agent": "foundry-coder-7b",
            "event": "sleep",
            "details": "Consolidated 3 tasks",
        })
        entries = read_library_log()
        assert len(entries) == 1
        assert entries[0]["session_id"] == "ses_log_001"
        assert entries[0]["event"] == "sleep"

    def test_multiple_appends(self, tmp_sessions_dir):
        """Multiple log entries are all preserved."""
        append_library_log({
            "session_id": "ses_log_002",
            "agent": "foundry-coder-7b",
            "event": "sleep",
        })
        append_library_log({
            "session_id": "ses_log_002",
            "agent": "foundry-coder-7b",
            "event": "wake",
        })
        entries = read_library_log()
        assert len(entries) == 2
        assert entries[0]["event"] == "sleep"
        assert entries[1]["event"] == "wake"

    def test_read_empty_log(self, tmp_sessions_dir):
        """Reading a non-existent log returns empty list."""
        entries = read_library_log()
        assert entries == []
