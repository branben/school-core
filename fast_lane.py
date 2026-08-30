"""Deterministic solo-developer fast-lane policy.

The fast lane removes only orchestration overhead for work that is already
bounded and low risk. It never removes the task authority or assurance checks.
This module is pure policy/measurement; callers still perform bd claim/close,
project verification, Entire, bookbag, and evidence persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REQUIRED_CHECKS = (
    "bd_claim",
    "project_verify",
    "entire",
    "bookbag",
    "bd_close_evidence",
)

_DISQUALIFIERS = (
    ("high_risk", "risk is not low"),
    ("multi_file", "more than one file is expected to change"),
    ("architecture", "architectural routing is required"),
    ("security", "security-sensitive work is required"),
    ("external_side_effect", "external or production side effects are possible"),
    ("human_gate", "a human approval gate is required"),
    ("uncertain", "task boundaries are uncertain"),
    ("checks_unavailable", "required local checks are unavailable"),
)


@dataclass(frozen=True)
class FastLaneDecision:
    admitted: bool
    reason: str
    required_checks: tuple[str, ...] = REQUIRED_CHECKS
    disqualifiers: tuple[str, ...] = ()


def _truthy(task: dict, key: str) -> bool:
    return bool(task.get(key, False))


def decide_fast_lane(task: dict[str, Any]) -> FastLaneDecision:
    """Admit only explicitly bounded, low-risk, fully checkable work.

    A bd identity is mandatory. Missing facts fail closed instead of being
    interpreted as low risk. ``checks_available`` may be supplied by the
    caller after local preflight and must include project verification and
    Entire availability.
    """
    if not isinstance(task, dict):
        return FastLaneDecision(False, "invalid_task", disqualifiers=("uncertain",))
    if not task.get("bd_id"):
        return FastLaneDecision(False, "bd_identity_missing", disqualifiers=("uncertain",))
    if task.get("risk") != "low":
        return FastLaneDecision(False, "high_risk", disqualifiers=("high_risk",))
    if task.get("expected_files") != 1 or task.get("single_file") is not True:
        return FastLaneDecision(False, "multi_file", disqualifiers=("multi_file",))

    disqualifiers = [name for name, _ in _DISQUALIFIERS if _truthy(task, name)]
    checks = set(task.get("checks_available") or ())
    missing_checks = [check for check in REQUIRED_CHECKS if check not in checks]
    if missing_checks:
        disqualifiers.append("checks_unavailable")
    if disqualifiers:
        return FastLaneDecision(
            False,
            disqualifiers[0],
            disqualifiers=tuple(dict.fromkeys(disqualifiers)),
        )
    return FastLaneDecision(True, "low_risk_bounded", disqualifiers=())


def summarize_lane_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare fast/full lanes only when both have comparable observations.

    ``rework`` is a bounded boolean supplied by the caller (for example a
    retry, school rejection, or follow-up bead). The function reports
    ``comparable=False`` until both lanes have observations, preventing a
    one-sided fast-lane launch from claiming a throughput win.
    """
    buckets = {"fast": [], "full": []}
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict) or run.get("lane") not in buckets:
            continue
        buckets[run["lane"]].append(run)

    def _stats(items: list[dict]) -> dict[str, Any]:
        if not items:
            return {"count": 0, "rework_count": 0, "rework_rate": None, "median_overhead_ms": None}
        rework = sum(1 for item in items if bool(item.get("rework")))
        overhead = sorted(
            max(0, int(item.get("overhead_ms", 0) or 0))
            for item in items
            if item.get("overhead_ms") is not None
        )
        median = overhead[len(overhead) // 2] if overhead else None
        return {
            "count": len(items),
            "rework_count": rework,
            "rework_rate": round(rework / len(items), 4),
            "median_overhead_ms": median,
        }

    fast = _stats(buckets["fast"])
    full = _stats(buckets["full"])
    comparable = fast["count"] > 0 and full["count"] > 0
    return {
        "schema_version": 1,
        "comparable": comparable,
        "fast": fast,
        "full": full,
        "rework_rate_delta": (
            round(fast["rework_rate"] - full["rework_rate"], 4)
            if comparable else None
        ),
        "overhead_reduction_ms": (
            full["median_overhead_ms"] - fast["median_overhead_ms"]
            if comparable and fast["median_overhead_ms"] is not None and full["median_overhead_ms"] is not None
            else None
        ),
        "promotion_safe": bool(
            comparable
            and fast["rework_rate"] <= full["rework_rate"]
            and fast["median_overhead_ms"] is not None
            and full["median_overhead_ms"] is not None
            and fast["median_overhead_ms"] < full["median_overhead_ms"]
        ),
    }


__all__ = ["FastLaneDecision", "REQUIRED_CHECKS", "decide_fast_lane", "summarize_lane_metrics"]
