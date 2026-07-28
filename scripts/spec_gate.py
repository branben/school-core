#!/usr/bin/env python3
"""spec_gate.py — Definition-of-Done (DOD) gate for Rank 6 (harness-ready).

Rank 6 of the Layer B integration plan.

A spec JSON file declares what "done" means for a task. The spec_gate
evaluates every criterion against the execution result and returns a
pass/fail verdict. When a criterion fails the result's ``accepted`` flag
is set to False so the existing CTO+COO review pipeline treats the
spec-gate failure identically to a normal rejection (no extra wiring).

Spec JSON format (``.hermes/specs/<task-id>.json``):

    {
        "task_id": "bead-abc123",
        "criteria": [
            {"id": "tests-pass", "description": "All tests pass", "required": true},
            {"id": "no-critical-findings", "description": "No CRITICAL findings", "required": true},
            {"id": "response-length", "description": "Response contains 10+ chars", "required": false}
        ]
    }

Each criterion has an ``id``, a human ``description``, and ``required``
(true = hard veto; false = soft warning that still counts as a failure
if unmet, but only for traceability).

The module is fully offline-testable: ``_load_spec`` reads from disk,
``_check_dod`` is a pure function over the spec and result dict. No LLM,
no OmniRoute, no network.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SPEC_DIR = Path(__file__).parent.parent / ".hermes" / "specs"


def _specs_dir() -> Path:
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    return SPEC_DIR


def _find_spec(task_id: str, repo: str = "__global__") -> Optional[Path]:
    """Locate a spec file for a task by task_id. Searches repo-namespaced
    path first, then global fallback."""
    repo_slug = repo.replace("/", "__")
    candidates = [
        SPEC_DIR / repo_slug / f"{task_id}.json",
        SPEC_DIR / f"{task_id}.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_spec(task_id: str, repo: str = "__global__") -> Optional[dict]:
    """Load a spec JSON for a task. Returns None if no spec exists."""
    spec_path = _find_spec(task_id, repo)
    if spec_path is None:
        return None
    try:
        return json.loads(spec_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("spec_gate: failed to load spec %s: %s", spec_path, e)
        return None


# ── Criterion checkers ──────────────────────────────────────────────

def _check_criterion(criterion: dict, result: dict) -> Tuple[bool, str]:
    """Evaluate a single DOD criterion against an execution result.

    Returns (passed: bool, reason: str).
    """
    cid = criterion.get("id", "unknown")
    desc = criterion.get("description", "")
    required = bool(criterion.get("required", True))

    # Built-in criterion IDs with their checks.
    if cid == "tests-pass":
        # Fail if any sub-task failed or the overall status is not success.
        status = result.get("status")
        if status != "success":
            return False, f"status is {status!r}, expected success"
        plan = result.get("plan")
        if plan and not plan.get("all_passed", False):
            return False, "not all sub-tasks passed"
        return True, ""

    if cid == "no-critical-findings":
        findings = result.get("review", {}).get("findings", [])
        for f in findings:
            sev = f.get("severity", "") if isinstance(f, dict) else ""
            if str(sev).upper() == "CRITICAL":
                return False, f"CRITICAL finding present: {f.get('title', f.get('description', 'unspecified'))}"
        return True, ""

    if cid == "response-length":
        text = result.get("response", "") or result.get("error", "") or ""
        if len(text) < 10:
            return False, f"response length {len(text)} < 10 chars"
        return True, ""

    if cid == "no-error":
        if result.get("error") is not None:
            return False, f"error field set: {result['error']}"
        return True, ""

    if cid == "ce-phases-present":
        phases = result.get("ce_phases")
        if not phases:
            return False, "ce_phases absent (CE loop was not run)"
        return True, ""

    if cid == "plan-present":
        if "plan" not in result:
            return False, "plan absent (complex_task was not used)"
        return True, ""

    # Unknown criterion ID — skip (pass) but log so the spec author knows.
    logger.warning("spec_gate: unknown criterion id %r, skipping", cid)
    return True, ""


def _check_dod(spec: dict, result: dict) -> Tuple[bool, List[dict]]:
    """Evaluate all DOD criteria in a spec against an execution result.

    Returns (all_passed: bool, failures: list[{criterion_id, description, reason}]).
    A single failing required criterion makes all_passed=False.
    """
    failures: List[dict] = []
    for crit in spec.get("criteria", []):
        passed, reason = _check_criterion(crit, result)
        if not passed:
            failures.append({
                "criterion_id": crit.get("id", "unknown"),
                "description": crit.get("description", ""),
                "required": crit.get("required", True),
                "reason": reason,
            })

    # Hard veto if any required criterion failed.
    has_required_failure = any(f["required"] for f in failures)
    return not has_required_failure, failures


def check_dod(
    task_id: str,
    result: dict,
    repo: str = "__global__",
    spec_path_override: Optional[str] = None,
) -> dict:
    """Evaluate the DOD gate for a completed task execution.

    Args:
        task_id: The bead/task identifier used to locate the spec file.
        result: The execution result dict from director.run_task() or
            leaf.run_task().
        repo: Repo namespace for spec lookup.
        spec_path_override: Direct path to a spec JSON (for testing or
            when the task_id-to-spec mapping is non-standard).

    Returns:
        {
            "passed": bool,
            "failures": list[dict],
            "spec_path": str|None,
        }
    """
    if spec_path_override:
        spec_file = Path(spec_path_override)
        if not spec_file.exists():
            return {"passed": False, "failures": [], "spec_path": spec_path_override}
        spec = json.loads(spec_file.read_text())
    else:
        spec = _load_spec(task_id, repo)

    if spec is None:
        # No spec → no gate → pass by default.
        return {"passed": True, "failures": [], "spec_path": None}

    passed, failures = _check_dod(spec, result)
    return {
        "passed": passed,
        "failures": failures,
        "spec_path": str(spec_path_override or _find_spec(task_id, repo) or ""),
    }


def write_spec(task_id: str, criteria: list, repo: str = "__global__") -> Path:
    """Write a spec JSON for a task to .hermes/specs/<repo-slug>/<task-id>.json.

    Helper for tests and manual setup.
    """
    spec = {"task_id": task_id, "criteria": criteria}
    spec_dir = _specs_dir() / repo.replace("/", "__")
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{task_id}.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    return spec_path