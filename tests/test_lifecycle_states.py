"""Behavioral tests for canonical lifecycle mapping."""

import pytest

from lifecycle_states import LifecycleState, map_task_state, project_board_state


def test_success_and_legacy_resolved_map_to_success():
    assert map_task_state("success") is LifecycleState.SUCCESS
    assert map_task_state("resolved") is LifecycleState.SUCCESS


def test_retry_failure_and_blocked_states_remain_distinct():
    assert map_task_state("retry") is LifecycleState.RETRY
    assert map_task_state("failed") is LifecycleState.FAILED
    assert map_task_state("blocked") is LifecycleState.BLOCKED


def test_unknown_task_state_is_rejected():
    with pytest.raises(ValueError, match="unknown task state"):
        map_task_state("totally-new")


def test_board_projection_distinguishes_merge_and_unknown():
    assert project_board_state(
        task_state="success", candidate_state="current", merge_state="confirmed", beads_state="closed"
    ) == "merged"
    assert project_board_state(
        task_state="success", candidate_state="stale", merge_state="unknown", beads_state="open"
    ) == "stale"
    assert project_board_state(
        task_state="success", candidate_state="current", merge_state="unknown", beads_state="open"
    ) == "unknown"
