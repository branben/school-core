#!/usr/bin/env python3
"""
Unit tests for ce_runner.py — Compound Engineering (CE) workflow loop.

Tests cover:
  - CE-enabled execution produces artifacts on disk
  - CE-disabled execution behaves as before (no artifacts, no ce_phases)
  - Result dict contains ce_phases when ce_enabled=True
  - Offline mode works (mocked LLM calls)
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.ce_runner import run_ce_loop


def test_ce_loop_produces_artifacts():
    """Test that CE loop produces artifacts in docs/solutions/<task-id>/."""
    with patch.dict(os.environ, {"OFFLINE_MODE": "true"}):
        result = run_ce_loop(
            task_prompt="Test task",
            domain="python-coding",
            role="coder",
            difficulty="easy",
        )
    
    assert result["status"] == "success"
    assert "ce_phases" in result
    assert "task_id" in result

    task_id = result["task_id"]
    # ce_runner writes artifacts to <repo>/docs/solutions/<task_id> (absolute
    # path derived from __file__), so assert there — the temp_docs_dir fixture
    # does not override that path.
    artifacts_dir = Path(__file__).parent.parent / "docs" / "solutions" / task_id

    assert artifacts_dir.exists(), f"Artifacts directory {artifacts_dir} does not exist"
    
    # Check that all expected artifacts are created
    expected_artifacts = [
        "01-brainstorm.md",
        "02-plan.md",
        "03-work.md",
        "04-simplify.md",
        "05-review.md",
        "06-compound.md",
    ]
    
    for artifact in expected_artifacts:
        artifact_path = artifacts_dir / artifact
        assert artifact_path.exists(), f"Artifact {artifact} does not exist"


def test_ce_loop_phases_tracking():
    """Test that CE loop tracks phases executed."""
    with patch.dict(os.environ, {"OFFLINE_MODE": "true"}):
        result = run_ce_loop(
            task_prompt="Test task",
            domain="python-coding",
            role="coder",
            difficulty="easy",
        )
    
    assert result["status"] == "success"
    assert "ce_phases" in result
    assert len(result["ce_phases"]) >= 5  # brainstorm, plan, work, simplify, review
    assert "compound" in result["ce_phases"]  # Score >= 50 in offline mode


def test_ce_loop_iteration_on_low_score():
    """Test that CE loop retries on low score (score < 50)."""
    with patch("scripts.ce_runner._evaluate_review", return_value=40):
        with patch.dict(os.environ, {"OFFLINE_MODE": "false"}):
            result = run_ce_loop(
                task_prompt="Test task",
                domain="python-coding",
                role="coder",
                difficulty="easy",
            )
    
    assert result["status"] == "success"
    assert "ce_phases" in result
    assert "plan_retry" in result["ce_phases"]  # Should loop back to plan


def test_ce_disabled_behavior():
    """Test that CE-disabled execution behaves as before (no ce_phases)."""
    from director import run_task
    
    with patch("director.call_model", return_value="Mocked response"):
        result = run_task(
            prompt="Test task",
            domain="python-coding",
            force_agent="coder",
            ce_enabled=False,
        )
    
    assert "ce_phases" not in result
    assert result["status"] == "success"


def test_ce_enabled_phases_in_result():
    """Test that CE-enabled execution includes ce_phases in result."""
    from director import run_task
    
    with patch("scripts.ce_runner.run_ce_loop") as mock_ce_loop:
        mock_ce_loop.return_value = {
            "status": "success",
            "ce_phases": ["brainstorm", "plan", "work", "simplify", "review", "compound"],
            "task_id": "test123",
        }
        
        result = run_task(
            prompt="Test task",
            domain="python-coding",
            force_agent="coder",
            ce_enabled=True,
        )
    
    assert "ce_phases" in result
    assert result["ce_phases"] == ["brainstorm", "plan", "work", "simplify", "review", "compound"]