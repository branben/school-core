"""Tests for the ACRouter wiring in executor.call_model / director.run_task.

These verify two things:
  1. The cold-start path still honours the static COMBO_MAP (no regression).
  2. executor.select_combo / record_routing_outcome round-trip through the
     persistent RouterExperience so run_task's feedback actually teaches the
     router which combo to use next time.
"""

from pathlib import Path

import executor
from router_experience import RouterExperience, combo_candidates_from


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
