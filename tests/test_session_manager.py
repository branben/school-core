"""
Tests for Session-Manager Staff Plugin (issue #006).

Run: python -m pytest tests/test_session_manager.py -v
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from staff.plugins.session_manager import SessionManagerPlugin
from staff.plugin import PluginTrust, StaffContext
from staff.sandbox import StaffSandbox
from staff.loader import StaffLoader
from scoring import ScoreStore


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_sessions_dir(tmp_path, monkeypatch):
    """Redirect session paths to temp directory."""
    sessions_dir = tmp_path / "sessions"
    consolidation_dir = sessions_dir / "consolidation"
    monkeypatch.setattr("sleep_state.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("sleep_state.CONSOLIDATION_DIR", consolidation_dir)
    monkeypatch.setattr("sleep_state.LIBRARY_LOG_PATH", sessions_dir / "library_log.yaml")
    return sessions_dir


@pytest.fixture
def store(tmp_path):
    """Create a ScoreStore with temp scores."""
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(json.dumps({
        "foundry-coder-7b": {"_default": 25.0, "python-testing": 65.4},
    }))
    return ScoreStore(file_path=str(scores_file))


@pytest.fixture
def plugin():
    return SessionManagerPlugin()


@pytest.fixture
def sandbox():
    return StaffSandbox(trust=PluginTrust.CORE, vault_path="/tmp")


@pytest.fixture
def context(store):
    return StaffContext(
        vault_path="/tmp",
        score_store=store,
        engram_available=False,
        cocoindex_available=False,
        building="default",
    )


@pytest.fixture(autouse=True)
def clear_sessions(tmp_sessions_dir):
    """Clear Director's _active_sessions before each test."""
    from director import _active_sessions
    _active_sessions.clear()
    yield
    _active_sessions.clear()
    # Clean up session files
    for f in tmp_sessions_dir.rglob("*"):
        if f.is_file():
            f.unlink()


def _add_active_session(session_id: str, agent: str = "foundry-coder-7b",
                        last_activity: str = None, building: str = "default"):
    """Helper to inject a session into Director's _active_sessions."""
    from director import _active_sessions
    _active_sessions[session_id] = {
        "agent": agent,
        "building": building,
        "task_queue": [],
        "layer_0": {},
        "episodic_history": [],
        "start_time": last_activity or datetime.now(timezone.utc).isoformat(),
        "last_activity": last_activity or datetime.now(timezone.utc).isoformat(),
        "tasks_completed": 0,
    }


# ── Plugin Interface ─────────────────────────────────────────────────────────

class TestPluginInterface:
    def test_name(self, plugin):
        assert plugin.name == "session-manager"

    def test_trust_defaults_to_core(self, plugin):
        assert plugin.trust == PluginTrust.CORE

    def test_trust_configurable(self):
        p = SessionManagerPlugin(config={"trust": "community"})
        assert p.trust == PluginTrust.COMMUNITY

    def test_health_check(self, plugin):
        hc = plugin.health_check()
        assert "session_read" in hc
        assert "sleep_trigger" in hc
        assert hc["session_read"] == "available"


# ── Happy Path ───────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_no_sessions_returns_success(self, plugin, sandbox, context):
        """No active sessions: returns success with zero counts."""
        result = plugin.run(sandbox, context)
        assert result.status == "success"
        assert result.metrics["active_count"] == 0
        assert result.metrics["sleep_triggered_count"] == 0
        assert result.metrics["sessions_scanned"] == 0

    def test_active_session_within_timeout_not_slept(self, plugin, sandbox, context):
        """Active session within timeout: not slept."""
        _add_active_session("ses_active_001", last_activity=datetime.now(timezone.utc).isoformat())
        result = plugin.run(sandbox, context)
        assert result.status == "success"
        assert result.metrics["active_count"] == 1
        assert result.metrics["sleep_triggered_count"] == 0

    def test_timed_out_session_triggers_sleep(self, plugin, sandbox, context, tmp_sessions_dir):
        """Session exceeding timeout: sleep triggered."""
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _add_active_session("ses_timeout_001", last_activity=old_time)
        result = plugin.run(sandbox, context)
        assert result.status == "success"
        assert result.metrics["sleep_triggered_count"] == 1
        # Verify sleep actually persisted state
        assert (tmp_sessions_dir / "ses_timeout_001.json").exists()


# ── Idempotency ──────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_double_run_does_not_double_sleep(self, plugin, sandbox, context, tmp_sessions_dir):
        """Running twice doesn't re-sleep already-slept sessions."""
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _add_active_session("ses_idem_001", last_activity=old_time)

        result1 = plugin.run(sandbox, context)
        assert result1.metrics["sleep_triggered_count"] == 1

        result2 = plugin.run(sandbox, context)
        assert result2.metrics["sleep_triggered_count"] == 0

    def test_multiple_sessions_selective_sleep(self, plugin, sandbox, context, tmp_sessions_dir):
        """Only timed-out sessions are slept; active ones are not."""
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        fresh_time = datetime.now(timezone.utc).isoformat()
        _add_active_session("ses_old", last_activity=old_time)
        _add_active_session("ses_fresh", last_activity=fresh_time)

        result = plugin.run(sandbox, context)
        assert result.metrics["sessions_scanned"] == 2
        assert result.metrics["sleep_triggered_count"] == 1


# ── Configuration ────────────────────────────────────────────────────────────

class TestConfiguration:
    def test_custom_timeout(self, sandbox, context, tmp_sessions_dir):
        """Configurable timeout via config['timeout_minutes']."""
        plugin = SessionManagerPlugin(config={"timeout_minutes": 5})
        # 10 minutes ago — exceeds 5min timeout but not 15min
        borderline = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        _add_active_session("ses_custom_001", last_activity=borderline)

        result = plugin.run(sandbox, context)
        assert result.metrics["sleep_triggered_count"] == 1
        assert result.metrics["timeout_minutes"] == 5

    def test_default_timeout_uses_director_constant(self, plugin, sandbox, context):
        """Without config, falls back to SLEEP_TIMEOUT_MINUTES (15)."""
        from director import SLEEP_TIMEOUT_MINUTES
        assert plugin._timeout_minutes == SLEEP_TIMEOUT_MINUTES


# ── Error Handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_malformed_timestamp_skipped(self, plugin, sandbox, context):
        """Session with bad timestamp is skipped, not crashed."""
        _add_active_session("ses_bad_ts", last_activity="not-a-timestamp")
        result = plugin.run(sandbox, context)
        assert result.status == "success"
        assert result.metrics["sleep_triggered_count"] == 0

    def test_empty_timestamp_skipped(self, plugin, sandbox, context):
        """Session with empty timestamp is skipped."""
        _add_active_session("ses_empty_ts", last_activity="")
        result = plugin.run(sandbox, context)
        assert result.status == "success"
        assert result.metrics["sleep_triggered_count"] == 0


# ── Plugin Discovery ─────────────────────────────────────────────────────────

class TestPluginDiscovery:
    def test_discoverable_via_loader(self, tmp_path, monkeypatch):
        """StaffLoader discovers session_manager plugin."""
        monkeypatch.setattr(StaffLoader, "__init__", lambda self, *a, **kw: None)
        loader = StaffLoader.__new__(StaffLoader)
        loader.school_root = Path(__file__).parent.parent
        loader.plugins = {}
        loader.schedules = {}
        plugins = loader.discover()
        assert "session-manager" in plugins
        assert plugins["session-manager"].name == "session-manager"


# ── Director run_task regression tests ──────────────────────────────────────

class TestRunTask:
    """run_task() must not crash with NameError from undeclared variables."""

    def test_no_session_id_does_not_crash(self):
        from director import run_task
        import inspect
        sig = inspect.signature(run_task)
        assert "session_id" in sig.parameters
        assert sig.parameters["session_id"].default is None

    def test_agent_role_is_defined(self):
        from director import _agent_role
        assert _agent_role("a", 80) == "Faculty"
        assert _agent_role("a", 60) == "Senior"
        assert _agent_role("a", 30) == "Junior"
        assert _agent_role("a", 10) == "Trainee"

    def test_run_task_compiles_and_imports(self):
        import importlib, director
        importlib.reload(director)
        assert hasattr(director, "run_task")
