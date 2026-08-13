"""Score/evidence finalization seam for the director.

The director keeps ``evaluate_and_update`` as its public compatibility façade;
this module owns the durable score, trajectory, compliance, and activity-log
finalization once a task has a result.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from activity_log import get_log
from engram_adapter import delete_observation, engram_available, save_trajectory as engram_save
from scoring import EFCScorer, GATES, ScoreStore


def finalize_score(
    result: dict,
    task_score: float,
    evaluation: Optional[str] = None,
    store: Optional[ScoreStore] = None,
    attach_teacher_evidence: Optional[Callable[[dict], object]] = None,
) -> dict:
    """Finalize a task result while preserving the legacy result schema."""
    if store is None:
        store = ScoreStore()

    if result.get("status") == "blocked":
        return result

    agent = result["agent"]
    domain = result["domain"]

    # Async bridge runs attach teacher review before scoring; synchronous runs
    # attach it above. The injected callback keeps both paths idempotent and
    # avoids a circular import back into director.
    if attach_teacher_evidence is not None:
        attach_teacher_evidence(result)

    if result.get("status") == "error":
        task_score = 0.0

    # ── EFC Scoring (U1) ──
    response = result.get("response", "") or ""
    efc = EFCScorer.score(task_score, response)
    effective_score = efc.composite

    old = store.get_score(agent, domain)
    new = store.update_score(agent, domain, effective_score)
    old_gate = store.gate_for_score(old)
    new_gate = store.gate_for_score(new)

    crossed = None
    for gname, gthr in sorted(GATES.items(), key=lambda x: x[1]):
        if old < gthr <= new:
            crossed = gname

    # ── Compliance Tracking (U4) ──
    had_error = result.get("status") == "error" or result.get("error") is not None
    routed = True
    attempted = len(response.strip()) > 0
    completed = not had_error
    scored = task_score > 0
    compliance_score = round(
        sum(1 for dimension in [routed, attempted, completed, scored] if dimension)
        / 4.0
        * 100,
        2,
    )

    trajectory_path = result.get("trajectory")
    if trajectory_path:
        with open(trajectory_path) as trajectory_file:
            trajectory = json.load(trajectory_file)
        trajectory["task_score"] = task_score
        trajectory["efc"] = {
            "informative": efc.informative,
            "valid": efc.valid,
            "retained": efc.retained,
            "composite": efc.composite,
        }
        trajectory["compliance"] = {
            "routed": routed,
            "attempted": attempted,
            "completed": completed,
            "scored": scored,
            "score": compliance_score,
        }
        trajectory["old_score"] = old
        trajectory["new_score"] = new
        trajectory["evaluation"] = evaluation
        with open(trajectory_path, "w") as trajectory_file:
            json.dump(trajectory, trajectory_file, indent=2, ensure_ascii=False)

        if engram_available():
            old_obs_id = trajectory.get("engram_obs_id")
            if old_obs_id:
                delete_observation(old_obs_id)
            new_obs_id = engram_save(trajectory, trajectory_path)
            if new_obs_id:
                trajectory["engram_obs_id"] = new_obs_id
                with open(trajectory_path, "w") as trajectory_file:
                    json.dump(trajectory, trajectory_file, indent=2, ensure_ascii=False)

    result["old_score"] = old
    result["new_score"] = new
    result["gate_crossed"] = crossed
    result["task_score"] = task_score
    result["efc_score"] = efc.composite
    result["compliance"] = {
        "score": compliance_score,
        "dimensions": {
            "routed": routed,
            "attempted": attempted,
            "completed": completed,
            "scored": scored,
        },
    }
    if crossed:
        get_log().gate_cross(
            agent=agent,
            domain=domain,
            from_gate=old_gate,
            to_gate=new_gate,
            score=new,
        )
    get_log().finish_task(
        agent=agent,
        domain=domain,
        score=new,
        success=(task_score >= 40),
        gate_crossed=crossed,
    )
    return result


__all__ = ["finalize_score"]
