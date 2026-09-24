"""Canonical lifecycle vocabulary for School Core projections."""

from __future__ import annotations

from enum import StrEnum


class LifecycleState(StrEnum):
    SUCCESS = "success"
    RETRY = "retry"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALE = "stale"
    UNKNOWN = "unknown"
    MERGED = "merged"
    CLOSED = "closed"


_TASK_STATES = {
    "success": LifecycleState.SUCCESS,
    "resolved": LifecycleState.SUCCESS,
    "retry": LifecycleState.RETRY,
    "failed": LifecycleState.FAILED,
    "blocked": LifecycleState.BLOCKED,
    "stale": LifecycleState.STALE,
    "unknown": LifecycleState.UNKNOWN,
}


def map_task_state(value: str) -> LifecycleState:
    try:
        return _TASK_STATES[str(value).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown task state: {value}") from exc


def project_board_state(
    *, task_state: str, candidate_state: str, merge_state: str, beads_state: str,
) -> str:
    task = map_task_state(task_state)
    candidate = str(candidate_state).strip().lower()
    merge = str(merge_state).strip().lower()
    beads = str(beads_state).strip().lower()
    if merge in {"merged", "confirmed"} and beads == "closed":
        return LifecycleState.MERGED.value
    if candidate in {"stale", "superseded", "failed", "blocked"} or task in {
        LifecycleState.STALE, LifecycleState.FAILED, LifecycleState.BLOCKED,
    }:
        return LifecycleState.STALE.value
    if task is LifecycleState.SUCCESS and beads == "closed" and merge != "confirmed":
        return LifecycleState.CLOSED.value
    if candidate not in {"current", "created", "approved"} or merge not in {
        "unknown", "requested", "confirmed", "merged", "failed",
    } or beads not in {"open", "closed", "blocked"}:
        return LifecycleState.UNKNOWN.value
    return LifecycleState.UNKNOWN.value


__all__ = ["LifecycleState", "map_task_state", "project_board_state"]
