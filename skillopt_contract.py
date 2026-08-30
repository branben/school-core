"""Bounded SkillOpt experiment contract.

SkillOpt is deliberately downstream of the route/outcome join. This module
specifies the controls and reports ``not_run``/``blocked`` until the corpus,
held-out split, fixed route, and evidence fields are present.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Optional


SCHEMA_VERSION = 1
_REQUIRED_OUTCOME_FIELDS = {
    "quality", "failure_edge", "failure_mode", "next_action", "lifecycle",
}


@dataclass(frozen=True)
class ExperimentConfig:
    task_family: str
    baseline_skill: str
    candidate_skill: str
    fixed_route: dict
    training_tasks: tuple[str, ...]
    holdout_tasks: tuple[str, ...]
    cost_budget: int
    min_holdout_tasks: int = 3


def _stable_id(config: ExperimentConfig) -> str:
    payload = "|".join([
        config.task_family,
        config.baseline_skill,
        config.candidate_skill,
        json.dumps(config.fixed_route, sort_keys=True),
        *config.training_tasks,
        *config.holdout_tasks,
    ])
    return "skillopt-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def _validate(config: ExperimentConfig) -> list[str]:
    errors: list[str] = []
    if not config.task_family.strip():
        errors.append("task_family_missing")
    if not config.baseline_skill.strip() or not config.candidate_skill.strip():
        errors.append("skill_variant_missing")
    if config.baseline_skill == config.candidate_skill:
        errors.append("variants_identical")
    if not isinstance(config.fixed_route, dict) or not config.fixed_route:
        errors.append("fixed_route_missing")
    else:
        for key in ("model", "task_role", "profile", "verifier"):
            if not config.fixed_route.get(key):
                errors.append(f"fixed_route_{key}_missing")
    training = set(config.training_tasks)
    holdout = set(config.holdout_tasks)
    if not training:
        errors.append("training_split_missing")
    if not holdout:
        errors.append("holdout_split_missing")
    if training & holdout:
        errors.append("training_holdout_overlap")
    if len(holdout) < max(1, config.min_holdout_tasks):
        errors.append("holdout_too_small")
    if int(config.cost_budget) <= 0:
        errors.append("cost_budget_invalid")
    return errors


def build_contract(config: ExperimentConfig) -> dict:
    """Build a stable contract; invalid controls are explicit and non-runnable."""
    errors = _validate(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": _stable_id(config),
        "status": "blocked" if errors else "ready",
        "blockers": errors,
        "task_family": config.task_family,
        "variants": {"baseline": config.baseline_skill, "candidate": config.candidate_skill},
        "fixed_route": dict(config.fixed_route),
        "training_tasks": list(config.training_tasks),
        "holdout_tasks": list(config.holdout_tasks),
        "cost_budget": int(config.cost_budget),
        "acceptance_metrics": [
            "quality_non_decrease",
            "contract_failure_rate_non_increase",
            "fallback_rate_non_increase",
            "rework_rate_non_increase",
        ],
        "execution_policy": {
            "same_model_route": True,
            "same_verifier": True,
            "same_runtime": True,
            "no_fabricated_lift": True,
            "heldout_required": True,
        },
    }


def _metric_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"count": 0, "quality": None, "contract_failure_rate": None, "fallback_rate": None, "rework_rate": None}

    def _rate(predicate):
        return round(sum(1 for row in rows if predicate(row)) / len(rows), 4)

    quality = [float(row["outcome"]["quality"]) for row in rows]
    return {
        "count": len(rows),
        "quality": round(mean(quality), 4),
        "contract_failure_rate": _rate(lambda row: row["outcome"].get("failure_edge") == "task_contract"),
        "fallback_rate": _rate(lambda row: bool(row.get("fallback_reason") or row["outcome"].get("fallback_reason"))),
        "rework_rate": _rate(lambda row: bool(row.get("rework"))),
    }


def evaluate_results(
    contract: dict,
    results: list[dict],
    *,
    min_holdout_tasks: Optional[int] = None,
) -> dict:
    """Evaluate a completed experiment conservatively; never invent a lift."""
    if not isinstance(contract, dict) or contract.get("status") != "ready":
        return {"status": "blocked", "reason": "invalid_or_blocked_contract"}
    required = set(contract.get("acceptance_metrics") or ())
    if not required:
        return {"status": "blocked", "reason": "acceptance_metrics_missing"}
    if not isinstance(results, list):
        return {"status": "not_run", "reason": "results_missing"}

    needed_holdout = max(1, int(min_holdout_tasks or len(contract.get("holdout_tasks") or [])))
    holdout = [row for row in results if isinstance(row, dict) and row.get("split") == "holdout"]
    if len(holdout) < needed_holdout:
        return {
            "status": "blocked",
            "reason": "heldout_evidence_missing",
            "holdout_count": len(holdout),
            "required_holdout_count": needed_holdout,
        }

    usable = []
    for row in results:
        if not isinstance(row, dict) or row.get("variant") not in {"baseline", "candidate"}:
            continue
        outcome = row.get("outcome")
        if not isinstance(outcome, dict) or not _REQUIRED_OUTCOME_FIELDS <= set(outcome):
            return {"status": "blocked", "reason": "outcome_join_missing"}
        usable.append(row)
    baseline = _metric_rows([row for row in usable if row["variant"] == "baseline"])
    candidate = _metric_rows([row for row in usable if row["variant"] == "candidate"])
    if not baseline["count"] or not candidate["count"]:
        return {"status": "blocked", "reason": "variant_evidence_missing"}

    deltas = {
        key: round(candidate[key] - baseline[key], 4)
        for key in ("quality", "contract_failure_rate", "fallback_rate", "rework_rate")
    }
    acceptance = {
        "quality_non_decrease": deltas["quality"] >= 0,
        "contract_failure_rate_non_increase": deltas["contract_failure_rate"] <= 0,
        "fallback_rate_non_increase": deltas["fallback_rate"] <= 0,
        "rework_rate_non_increase": deltas["rework_rate"] <= 0,
    }
    return {
        "status": "accepted" if all(acceptance.values()) else "rejected",
        "baseline": baseline,
        "candidate": candidate,
        "deltas": deltas,
        "acceptance": acceptance,
        "heldout_count": len(holdout),
        "no_fabricated_lift": True,
    }


def write_contract(path: str | Path, contract: dict) -> Path:
    """Atomically write a contract artifact for review and reproducibility."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(contract, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return target


__all__ = ["ExperimentConfig", "build_contract", "evaluate_results", "write_contract"]
