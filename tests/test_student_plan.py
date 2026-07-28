#!/usr/bin/env python3
"""Tests for student_plan.py (Rank 5 — Plan Mode for Student Decomposition).

All offline.  ``execute_plan()`` accepts an injectable ``run_leaf_fn``
so tests stub sub-task dispatch without real Orca/LLM calls.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.student_plan import (
    estimate_complexity,
    is_complex,
    generate_plan,
    execute_plan,
    COMPLEXITY_THRESHOLD,
)


# ── estimate_complexity / is_complex ──────────────────────────

def test_simple_task_complexity_is_1():
    assert estimate_complexity("Write a function that returns True.") == 1


def test_numbered_list_highlights_complexity():
    prompt = "1. Set up the environment\n2. Implement the core logic\n3. Write tests"
    assert estimate_complexity(prompt) == 3


def test_complexity_above_threshold_is_complex():
    prompt = "1. A\n2. B\n3. C\n4. D"
    assert is_complex(prompt) is True


def test_below_threshold_is_not_complex():
    assert is_complex("Write a palindrome function.") is False


# ── generate_plan writes .hermes/plans/<id>.md ─────────────────

def test_generate_plan_writes_file(tmp_path: Path):
    plans_dir = tmp_path / ".hermes" / "plans"
    with patch("scripts.student_plan.PLANS_DIR", plans_dir):
        result = generate_plan(
            "A very complex multi-step task with many parts", task_id="abc123"
        )
    assert result["task_id"] == "abc123"
    assert result["complexity"] > 0
    plan_file = plans_dir / "abc123.md"
    assert plan_file.exists()
    content = plan_file.read_text()
    assert "# Plan: abc123" in content
    assert "Sub-tasks" in content


def test_generate_plan_returns_sub_tasks_list():
    plan = generate_plan(
        "1. Step one\n2. Step two\n3. Step three\n4. Step four", task_id="t1"
    )
    assert len(plan["sub_tasks"]) >= 2
    assert all(isinstance(s, str) and len(s) > 0 for s in plan["sub_tasks"])


def test_generate_plan_uses_explicit_steps():
    prompt = "1. Scaffold the module\n2. Write the implementation\n3. Add tests\n4. Refactor\n5. Verify"
    plan = generate_plan(prompt, task_id="t2")
    assert plan["sub_tasks"][0] == "Scaffold the module"
    assert plan["sub_tasks"][1] == "Write the implementation"


def test_sub_tasks_respect_max():
    # 10 explicit steps capped at MAX_SUBTASKS (8).
    steps = "\n".join(f"{i}. Step {i}" for i in range(1, 11))
    plan = generate_plan(steps, task_id="t3")
    assert len(plan["sub_tasks"]) <= 8


def test_sub_tasks_padded_to_minimum():
    prompt = "Single step task."
    plan = generate_plan(prompt, task_id="t4")
    assert len(plan["sub_tasks"]) >= 2  # MIN_SUBTASKS


# ── execute_plan: per-sub-task CE loop ────────────────────────

def test_execute_plan_runs_sub_tasks_and_collects_results(tmp_path: Path):
    plans_dir = tmp_path / ".hermes" / "plans"
    plan = {
        "task_id": "plan-1",
        "plan_path": str(plans_dir / "plan-1.md"),
        "sub_tasks": ["Sub-task alpha", "Sub-task beta"],
        "complexity": 2,
    }
    call_log = []

    def fake_run_leaf(**kwargs):
        call_log.append(kwargs)
        return {"status": "success", "bead": "bead-" + kwargs.get("task_prompt", "?")[:6]}

    result = execute_plan(
        plan,
        role="coder",
        domain="python-coding",
        difficulty="easy",
        store=MagicMock(),
        run_leaf_fn=fake_run_leaf,
    )
    assert result["task_id"] == "plan-1"
    assert len(result["sub_task_results"]) == 2
    assert result["all_passed"] is True
    assert len(call_log) == 2
    # Each sub_task run_leaf call has complex_task=True for its own CE loop.
    assert all(kwargs.get("complex_task") is True for kwargs in call_log)


def test_execute_plan_stops_on_first_failure(tmp_path: Path):
    plan = {
        "task_id": "plan-2",
        "plan_path": "/tmp/plan-2.md",
        "sub_tasks": ["OK sub", "FAIL sub", "Never reached"],
        "complexity": 3,
    }
    call_log = []

    def fake_run_leaf(**kwargs):
        call_log.append(kwargs)
        if "FAIL" in kwargs.get("task_prompt", ""):
            return {"status": "failed", "error": "boom"}
        return {"status": "success"}

    result = execute_plan(
        plan,
        role="coder",
        domain="python-coding",
        difficulty="easy",
        store=MagicMock(),
        run_leaf_fn=fake_run_leaf,
    )
    assert len(call_log) == 2  # halts after failure, never reaches third
    assert result["all_passed"] is False