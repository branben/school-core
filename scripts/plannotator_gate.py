#!/usr/bin/env python3
"""plannotator_gate.py — Human-in-the-loop approval gates via Plannotator.

Two gates:
  gate_plan(plan_path)  → opens Plannotator annotate --gate, waits for decision.
  gate_diff(repo_path)  → opens Plannotator review (since-base diff), waits.

Both return a structured dict matching school-core's existing result format
so spec_gate, scoring, and the verdict pipeline treat them identically to
any teacher verdict.

Design:
  - Thin CLI wrapper — no reimplementation of Plannotator logic.
  - Atomic result-file writes for subagent consumption (no stdout parsing).
  - PLANNOTATOR_GATE_BYPASS=1 returns approved (CI / automated testing).
  - PLANNOTATOR_AI=disabled is set internally so Ask AI doesn't fire.

Usage:
    from scripts.plannotator_gate import gate_plan, gate_diff
    result = gate_plan(".hermes/plans/task-123.md")
    # result == {"decision": "approved"} or {"decision": "annotated", "feedback": "..."}
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Plannotator binary resolution
_PLANNOTATOR_BIN = shutil.which("plannotator") or os.path.expanduser(
    "~/.local/bin/plannotator"
)

# Result file directory (matches school-core's .hermes convention)
RESULT_DIR = Path(__file__).parent.parent / ".hermes" / "plannotator"


def _result_dir() -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    return RESULT_DIR


def _plannotator_available() -> bool:
    return Path(_PLANNOTATOR_BIN).exists() if _PLANNOTATOR_BIN else False


def _run_plannotator(args: list[str], timeout: int = 345600) -> subprocess.CompletedProcess:
    """Run plannotator with AI disabled and PLANNOTATOR_AI=disabled."""
    env = os.environ.copy()
    env["PLANNOTATOR_AI"] = "disabled"
    return subprocess.run(
        [_PLANNOTATOR_BIN] + args,
        capture_output=True,
        timeout=timeout,
        text=True,
        env=env,
        check=False,
    )


def gate_plan(
    plan_path: str | Path,
    *,
    task_id: Optional[str] = None,
    require_approval: bool = True,
) -> Dict:
    """Open a plan in Plannotator for human review and return the decision.

    Args:
        plan_path: Path to the plan Markdown file.
        task_id: Stable identifier (defaults to the file stem).
        require_approval: If True, only explicit approval exits 0 (strict mode).

    Returns:
        Dict with keys:
            - decision: "approved" | "annotated" | "dismissed" | "bypassed" | "error"
            - feedback: str (only when annotated)
            - reason: str (only when error)

    Raises:
        FileNotFoundError: If the plan file does not exist.
    """
    plan = Path(plan_path).resolve()
    if not plan.exists():
        raise FileNotFoundError(f"Plan not found: {plan}")

    if task_id is None:
        task_id = plan.stem

    # Bypass mode for CI / automated testing
    if os.environ.get("PLANNOTATOR_GATE_BYPASS") == "1":
        logger.info("[plannotator_gate] BYPASS — plan %s auto-approved", task_id)
        return {"decision": "bypassed", "feedback": "", "task_id": task_id}

    if not _plannotator_available():
        logger.error(
            "[plannotator_gate] plannotator binary not at %s — bypassing gate. "
            "Install with: curl -fsSL https://plannotator.ai/install.sh | bash",
            _PLANNOTATOR_BIN,
        )
        return {
            "decision": "error",
            "feedback": "",
            "reason": f"plannotator binary not found at {_PLANNOTATOR_BIN}",
            "task_id": task_id,
        }

    result_file = _result_dir() / f"{task_id}.json"
    # Remove stale result file (Plannotator refuses to overwrite)
    if result_file.exists():
        result_file.unlink()

    # Build CLI args
    args = [
        "annotate",
        str(plan),
        "--gate",
        "--json",
        "--result-file",
        str(result_file),
    ]
    if require_approval:
        args.append("--require-approval")

    logger.info("[plannotator_gate] Opening plan %s in Plannotator...", plan.name)

    try:
        proc = _run_plannotator(args)
    except subprocess.TimeoutExpired:
        return {
            "decision": "error",
            "feedback": "",
            "reason": "Plannotator session timed out (4d timeout hit)",
            "task_id": task_id,
        }

    # Read the result file if Plannotator wrote it
    if result_file.exists():
        try:
            result = json.loads(result_file.read_text())
            result["task_id"] = task_id
            logger.info(
                "[plannotator_gate] Plan %s → decision=%s",
                task_id,
                result.get("decision"),
            )
            return result
        except json.JSONDecodeError as e:
            return {
                "decision": "error",
                "feedback": "",
                "reason": f"Malformed result file: {e}",
                "task_id": task_id,
            }

    # No result file written — check stdout as fallback
    if proc.stdout.strip():
        try:
            result = json.loads(proc.stdout)
            result["task_id"] = task_id
            return result
        except json.JSONDecodeError:
            pass

    return {
        "decision": "error",
        "feedback": "",
        "reason": f"Plannotator exited {proc.returncode} with no result file",
        "stdout": proc.stdout.strip()[:500],
        "stderr": proc.stderr.strip()[:500],
        "task_id": task_id,
    }


def gate_diff(
    repo_path: str | Path = ".",
    *,
    task_id: Optional[str] = None,
    require_approval: bool = True,
) -> Dict:
    """Open a local diff in Plannotator for human code review.

    Args:
        repo_path: Path to the git repository.
        task_id: Stable identifier (defaults to "diff-<timestamp>").
        require_approval: If True, only explicit approval exits 0.

    Returns:
        Dict with keys: decision, feedback, reason, task_id.
    """
    repo = Path(repo_path).resolve()

    if task_id is None:
        import time
        task_id = f"diff-{int(time.time())}"

    if os.environ.get("PLANNOTATOR_GATE_BYPASS") == "1":
        logger.info("[plannotator_gate] BYPASS — diff %s auto-approved", task_id)
        return {"decision": "bypassed", "feedback": "", "task_id": task_id}

    if not _plannotator_available():
        return {
            "decision": "error",
            "feedback": "",
            "reason": f"plannotator binary not found at {_PLANNOTATOR_BIN}",
            "task_id": task_id,
        }

    result_file = _result_dir() / f"{task_id}.json"
    if result_file.exists():
        result_file.unlink()

    args = [
        "review",
        "--git",
        "--json",
        "--result-file",
        str(result_file),
    ]
    if require_approval:
        args.append("--require-approval")

    # Run from the repo directory so Plannotator detects the right changes
    logger.info("[plannotator_gate] Opening diff in Plannotator for %s...", repo)

    try:
        proc = _run_plannotator(args)
    except subprocess.TimeoutExpired:
        return {
            "decision": "error",
            "feedback": "",
            "reason": "Plannotator session timed out",
            "task_id": task_id,
        }

    if result_file.exists():
        try:
            result = json.loads(result_file.read_text())
            result["task_id"] = task_id
            logger.info(
                "[plannotator_gate] Diff %s → decision=%s",
                task_id,
                result.get("decision"),
            )
            return result
        except json.JSONDecodeError as e:
            return {
                "decision": "error",
                "feedback": "",
                "reason": f"Malformed result file: {e}",
                "task_id": task_id,
            }

    if proc.stdout.strip():
        try:
            result = json.loads(proc.stdout)
            result["task_id"] = task_id
            return result
        except json.JSONDecodeError:
            pass

    return {
        "decision": "error",
        "feedback": "",
        "reason": f"Plannotator exited {proc.returncode} with no result file",
        "stdout": proc.stdout.strip()[:500],
        "stderr": proc.stderr.strip()[:500],
        "task_id": task_id,
    }


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    """CLI for ad-hoc plan/diff gating."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Plannotator gates for school-core"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Gate a plan file")
    p_plan.add_argument("plan_path", help="Path to Markdown plan")
    p_plan.add_argument("--task-id", default=None)
    p_plan.add_argument("--no-require-approval", action="store_true")

    p_diff = sub.add_parser("diff", help="Gate a local diff")
    p_diff.add_argument("--repo-path", default=".")
    p_diff.add_argument("--task-id", default=None)
    p_diff.add_argument("--no-require-approval", action="store_true")

    args = parser.parse_args()

    if args.command == "plan":
        result = gate_plan(
            args.plan_path,
            task_id=args.task_id,
            require_approval=not args.no_require_approval,
        )
    else:
        result = gate_diff(
            args.repo_path,
            task_id=args.task_id,
            require_approval=not args.no_require_approval,
        )

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("decision") in ("approved", "bypassed") else 1)


if __name__ == "__main__":
    main()
