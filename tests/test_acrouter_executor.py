"""Tests for the ACRouter wiring in executor.call_model / director.run_task.

These verify two things:
  1. The cold-start path still honours the static COMBO_MAP (no regression).
  2. executor.select_combo / record_routing_outcome round-trip through the
     persistent RouterExperience so run_task's feedback actually teaches the
     router which combo to use next time.
"""

import os
from pathlib import Path

import pytest

import src.crew.executor as executor
from src.core.router_experience import RouterExperience, combo_candidates_from


def test_cold_start_honours_combo_map(monkeypatch, tmp_path):
    """With no experience file, select_combo falls back to COMBO_MAP."""
    monkeypatch.setattr(executor, "_ROUTER", None)
    monkeypatch.setattr(executor, "_ROUTER_PATH", str(tmp_path / "exp.json"))
    for role, combo in executor.COMBO_MAP.items():
        assert executor.select_combo(role) == combo


def test_record_outcome_round_trips_through_router(monkeypatch, tmp_path):
    """Recording an outcome makes that combo preferred on the next selection."""
    path = tmp_path / "exp.json"
    monkeypatch.setattr(executor, "_ROUTER", None)
    monkeypatch.setattr(executor, "_ROUTER_PATH", str(path))
    monkeypatch.setattr(executor, "_LAST_SELECTED_COMBO", {})

    candidates = ["auto/best-free", "oc/deepseek-v4-flash-free"]
    # Force the candidate set narrow for determinism.
    monkeypatch.setattr(
        executor, "_get_router",
        lambda: RouterExperience(
            candidates=candidates,
            default_resolver=lambda r: "auto/best-free",
            file_path=str(path),
            exploration_rate=0.0,
        ),
    )

    # Teach: oc/... is great, auto/... is bad for the "coder" role.
    executor.select_combo("coder")  # arms _LAST_SELECTED_COMBO with the default
    executor.record_routing_outcome("coder", success=False, quality=0.2)
    # Re-arm and record the good combo explicitly.
    executor._LAST_SELECTED_COMBO["coder"] = "oc/deepseek-v4-flash-free"
    executor.record_routing_outcome("coder", success=True, quality=0.9)

    assert executor.select_combo("coder") == "oc/deepseek-v4-flash-free"


def test_unknown_agent_raises(monkeypatch, tmp_path):
    """select_combo for an unmapped agent yields None → call_model raises."""
    monkeypatch.setattr(executor, "_ROUTER", None)
    monkeypatch.setattr(executor, "_ROUTER_PATH", str(tmp_path / "exp.json"))
    assert executor.select_combo("not-a-real-role") is None


def test_resolve_api_key_fails_when_unset(monkeypatch):
    """Env-only credential: no OMNIROUTE_API_KEY → ExecutorError, never an empty Bearer.

    Both sources are cleared: the module attribute AND the ``.env`` candidates.
    Clearing only the attribute used to be enough when the attribute was the
    sole source; now that ``_resolve_api_key`` also reads the files, an
    uncleared ``.env`` would supply a key and the test would pass for the
    wrong reason.
    """
    monkeypatch.setattr(executor, "API_KEY", "")
    monkeypatch.setattr(executor, "_ENV_FILE_CANDIDATES", ())
    with pytest.raises(executor.ExecutorError, match="OMNIROUTE_API_KEY"):
        executor._resolve_api_key()


def test_resolve_api_key_falls_back_to_env_file_without_polluting(monkeypatch, tmp_path):
    """A key present only in a ``.env`` file is still found — and ``os.environ`` is untouched.

    This is the regression guard for school-core-3r4n: the old code called
    ``load_dotenv`` at import time, which mutated ``os.environ`` process-wide
    and made pytest's collection order decide whether
    ``test_omni_relay_live.py``'s skipif saw a key the developer never
    exported. Reading the file directly must not have that effect.
    """
    monkeypatch.setattr(executor, "API_KEY", "")
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OMNIROUTE_API_KEY=from-dotenv\n")
    monkeypatch.setattr(executor, "_ENV_FILE_CANDIDATES", (env_file,))

    assert executor._resolve_api_key() == "from-dotenv"
    assert "OMNIROUTE_API_KEY" not in os.environ, (
        "reading a .env file must not mutate os.environ"
    )


def test_resolve_api_key_returns_configured_value(monkeypatch):
    """With a key configured, _resolve_api_key returns it for the Authorization header."""
    monkeypatch.setattr(executor, "API_KEY", "test-key")
    assert executor._resolve_api_key() == "test-key"
