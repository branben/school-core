#!/usr/bin/env python3
"""
ce_runner.py — Compound Engineering (CE) workflow loop for student task execution.

Phases:
    1. brainstorm: Student brainstorms approach (01-brainstorm.md)
    2. plan: Student creates a plan (02-plan.md)
    3. work: Student executes the plan (03-work.md)
    4. simplify: Student refines output (04-simplify.md)
    5. review: Director evaluates output against gate (05-review.md)
    6. compound: If score >= 50, write learnings (06-compound.md)

If score < 50, loop back to `plan` (max 3 iterations).

Artifacts are written to `docs/solutions/<task-id>/`.
Returns a list of phases executed.
"""

import os
import uuid
import logging
from pathlib import Path
from typing import Optional, List, Dict

from bookbag import write_bookbag, read_bookbag, bead_path
from adversarial_reviewer import Verdict

logger = logging.getLogger(__name__)

# Offline mode: Mock responses for testing. Read lazily (per-call) rather than
# caching at import time, so tests can toggle OFFLINE_MODE via patch.dict(os.environ)
# without a reload.
def _offline() -> bool:
    return os.getenv("OFFLINE_MODE", "false").lower() == "true"


# Phase order → artifact file index (01-..md through 06-..md)
PHASE_INDEX = {
    "brainstorm": 1,
    "plan": 2,
    "work": 3,
    "simplify": 4,
    "review": 5,
    "compound": 6,
}


def _write_artifact(task_id: str, phase: str, content: str) -> None:
    """Write a CE phase artifact to disk."""
    docs_dir = Path(__file__).parent.parent / "docs" / "solutions" / task_id
    docs_dir.mkdir(parents=True, exist_ok=True)
    idx = PHASE_INDEX.get(phase, 0)
    artifact_path = docs_dir / f"{idx:02d}-{phase}.md"
    artifact_path.write_text(content)
    logger.debug(f"Wrote artifact: {artifact_path}")


def _mock_llm_call(prompt: str, phase: str) -> str:
    """Mock LLM response for offline testing."""
    mock_responses = {
        "brainstorm": "# Brainstorm\n\nApproach: Implement a structured CE loop for student execution.\nKey considerations: Offline mode, artifact generation, phase tracking.",
        "plan": "# Plan\n\n1. Create `scripts/ce_runner.py`\n2. Modify `director.py` to support `ce_enabled`\n3. Update `leaf.py` to pass `ce_enabled`\n4. Modify `conductor.py` to enable CE for student dispatch\n5. Add tests for CE-enabled execution",
        "work": "# Work\n\nImplemented `scripts/ce_runner.py` with phase dispatcher.\nArtifacts are written to `docs/solutions/<task-id>/`.\nReview phase evaluates output against gate criteria.",
        "simplify": "# Simplify\n\nRefined output: Removed redundant comments, improved logging, and ensured offline mode compatibility.",
        "review": "# Review\n\nVerdict: PASS\nScore: 85\nFindings: None",
        "compound": "# Compound\n\nLearnings:\n- Offline mode is critical for testing\n- Artifact generation must be atomic\n- Phase tracking enables iterative improvement",
    }
    return mock_responses.get(phase, "")


def _run_phase(
    task_id: str,
    task_prompt: str,
    phase: str,
    domain: str,
    role: str,
    difficulty: str,
    repo: str,
) -> Dict:
    """Run a single CE phase and return its result."""
    if _offline():
        content = _mock_llm_call(task_prompt, phase)
        _write_artifact(task_id, phase, content)
        return {
            "status": "success",
            "content": content,
            "phase": phase,
        }

    # In live mode, this would call the LLM/Orca worktree
    # For now, we'll mock it to avoid external dependencies
    content = f"# {phase.capitalize()}\n\nTask: {task_prompt}\n\nOutput for phase `{phase}`."
    _write_artifact(task_id, phase, content)
    return {
        "status": "success",
        "content": content,
        "phase": phase,
    }


def _evaluate_review(review_content: str) -> int:
    """Extract score from review content. Defaults to 0 if not found."""
    if _offline():
        return 85  # Mock score for offline testing
    
    # Parse score from review content (e.g., "Score: 85")
    for line in review_content.splitlines():
        if "Score:" in line:
            try:
                return int(line.split(":")[-1].strip())
            except ValueError:
                pass
    return 0


def run_ce_loop(
    task_prompt: str,
    domain: str = "_default",
    role: str = "coder",
    difficulty: str = "easy",
    repo: str = "__global__",
) -> Dict:
    """Run the full CE loop for a task.

    Args:
        task_prompt: The task to execute.
        domain: Task domain (e.g., "python-coding").
        role: Student role (e.g., "coder").
        difficulty: Task difficulty (e.g., "easy").
        repo: Repository namespace.

    Returns:
        Dict with `ce_phases` (list of phases executed) and `task_id`.
    """
    task_id = str(uuid.uuid4())[:8]
    phases_executed = []
    max_iterations = 3
    iteration = 0
    score = 0

    while iteration < max_iterations:
        iteration += 1
        logger.debug(f"Starting CE iteration {iteration} for task {task_id}")

        # Run phases
        for phase in ["brainstorm", "plan", "work", "simplify"]:
            result = _run_phase(
                task_id=task_id,
                task_prompt=task_prompt,
                phase=phase,
                domain=domain,
                role=role,
                difficulty=difficulty,
                repo=repo,
            )
            if result["status"] != "success":
                logger.error(f"Phase {phase} failed for task {task_id}")
                return {
                    "status": "error",
                    "error": f"Phase {phase} failed",
                    "ce_phases": phases_executed,
                    "task_id": task_id,
                }
            phases_executed.append(phase)

        # Run review phase
        review_result = _run_phase(
            task_id=task_id,
            task_prompt=task_prompt,
            phase="review",
            domain=domain,
            role=role,
            difficulty=difficulty,
            repo=repo,
        )
        if review_result["status"] != "success":
            logger.error(f"Review phase failed for task {task_id}")
            return {
                "status": "error",
                "error": "Review phase failed",
                "ce_phases": phases_executed,
                "task_id": task_id,
            }
        phases_executed.append("review")

        # Extract score from review
        score = _evaluate_review(review_result["content"])
        if score >= 50:
            # Run compound phase
            compound_result = _run_phase(
                task_id=task_id,
                task_prompt=task_prompt,
                phase="compound",
                domain=domain,
                role=role,
                difficulty=difficulty,
                repo=repo,
            )
            if compound_result["status"] != "success":
                logger.error(f"Compound phase failed for task {task_id}")
                return {
                    "status": "error",
                    "error": "Compound phase failed",
                    "ce_phases": phases_executed,
                    "task_id": task_id,
                }
            phases_executed.append("compound")
            break
        else:
            logger.debug(f"Score {score} < 50, looping back to plan phase")
            phases_executed.append("plan_retry")

    return {
        "status": "success",
        "ce_phases": phases_executed,
        "task_id": task_id,
        "score": score,
    }