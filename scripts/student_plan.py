#!/usr/bin/env python3
"""student_plan.py — Plan Mode for Student Task Decomposition (Rank 5).

Breaks a complex student task into 2-5 minute bite-sized sub-tasks, written to
``.hermes/plans/<task-id>.md`` in the project-root plan format. Each sub-task
is then executed as its own CE/TDD loop by the director (see director.run_task
``complex_task`` branch), so a failed sub-task loops back to THAT task only —
not the whole plan.

Design (offline-testable, no LLM required):
- ``estimate_complexity(task_prompt)`` is a deterministic heuristic: it counts
  implied sub-steps from explicit numbered/bulleted markers and clause
  conjunctions, capped at a sane maximum. A live deployment can replace it
  with an LLM call; the signature stays the same.
- ``generate_plan(task_prompt, task_id)`` writes the markdown plan and returns
  ``{plan_path, sub_tasks}``. Sub-tasks are 2-5 min each by construction.
- ``execute_plan(...)`` runs each sub-task through ``run_leaf`` (CE-enabled for
  the per-task TDD loop) and returns per-sub-task results.

No external dependencies; pure stdlib.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLANS_DIR = Path(__file__).parent.parent / ".hermes" / "plans"

# Complexity above this threshold triggers plan mode.
COMPLEXITY_THRESHOLD = 3

# Sub-task sizing guardrails (2-5 min each).
MIN_SUBTASKS = 2
MAX_SUBTASKS = 8


def _plans_dir() -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    return PLANS_DIR


def estimate_complexity(task_prompt: str) -> int:
    """Deterministic heuristic for implied sub-step count (offline).

    Counts:
      - explicit numbered list items ("1.", "2)")
      - bullet markers ("-", "*", "•")
      - clause conjunctions ("and", "then", "after", "finally", ";")
    Capped at MAX_SUBTASKS. A single imperative sentence with no markers
    scores 1 (simple → no plan needed).
    """
    if not task_prompt:
        return 1
    text = task_prompt
    count = 0
    # Numbered items: "1." / "2)" at line start or after whitespace.
    count += len(re.findall(r"(?:^|\n)\s*\d+[.)]", text))
    # Bullets.
    count += len(re.findall(r"(?:^|\n)\s*[-*•]\s+", text))
    # Clause conjunctions (only if no explicit markers found, to avoid double
    # counting a naturally long single step).
    if count == 0:
        count += len(re.findall(r"\b(and|then|after that|finally|next|also)\b", text, re.I))
        count += text.count(";")
    # At least 1.
    return max(1, min(count, MAX_SUBTASKS))


def is_complex(task_prompt: str, threshold: int = COMPLEXITY_THRESHOLD) -> bool:
    """True if the task should be decomposed into a plan."""
    return estimate_complexity(task_prompt) > threshold


def generate_plan(task_prompt: str, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Write a bite-sized plan to ``.hermes/plans/<task-id>.md``.

    Args:
        task_prompt: The original (complex) task.
        task_id: Optional stable id; defaults to a short uuid.

    Returns:
        {
            "task_id": str,
            "plan_path": str,        # absolute path to the written plan
            "sub_tasks": list[str],  # bite-sized sub-task prompts (2-5 min each)
            "complexity": int,
        }
    """
    if task_id is None:
        task_id = str(uuid.uuid4())[:8]
    complexity = estimate_complexity(task_prompt)

    # Build 2-5 min sub-tasks. If the prompt already lists explicit steps, use
    # them verbatim; otherwise synthesize a generic decomposition.
    explicit = _extract_explicit_steps(task_prompt)
    if explicit:
        sub_tasks = explicit[:MAX_SUBTASKS]
    else:
        # Synthesize a decomposition from the task's clauses.
        sub_tasks = _synthesize_subtasks(task_prompt, complexity)

    # Enforce the 2-5 min sizing floor.
    if len(sub_tasks) < MIN_SUBTASKS:
        sub_tasks = _pad_subtasks(task_prompt, sub_tasks)

    plan_path = _plans_dir() / f"{task_id}.md"
    content = _render_plan_markdown(task_prompt, task_id, sub_tasks)
    plan_path.write_text(content)

    return {
        "task_id": task_id,
        "plan_path": str(plan_path),
        "sub_tasks": sub_tasks,
        "complexity": complexity,
    }


def _extract_explicit_steps(task_prompt: str) -> List[str]:
    """Pull numbered/bulleted lines out as sub-tasks."""
    steps: List[str] = []
    for line in task_prompt.splitlines():
        m = re.match(r"\s*(?:\d+[.)]|[-*•])\s+(.*)", line)
        if m:
            steps.append(m.group(1).strip())
    return [s for s in steps if s]


def codegraph_search(query: str, repo_root: Optional[Path] = None) -> List[str]:
    """Return codegraph symbols relevant to ``query`` (Rank 7 semantic search).

    Uses the codegraph CLI (already indexed for this repo — node:sqlite
    backend). Returns a list of plain symbol names. Falls back to a grep scan
    when codegraph is unavailable so the student still gets a structural hint
    without the external tool.

    Offline-safe: never raises; returns ``[]`` on any failure.
    """
    import re as _re  # local import to avoid shadowing module-level re

    # codegraph hard-codes ANSI color codes; strip them before parsing.
    # Use a permissive pattern that does not trigger regex nested-set warnings.
    _ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")

    def _clean(line: str) -> str:
        return _ANSI_RE.sub("", line).strip()

    # Primary: codegraph CLI query (stdout symbol dump).
    if shutil.which("codegraph"):
        try:
            proc = subprocess.run(
                ["codegraph", "query", query],
                capture_output=True, text=True, timeout=15,
                cwd=str(repo_root) if repo_root else None,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                syms: List[str] = []
                # codegraph declaration lines look like "method   run_task" /
                # "function  foo" / "class  Bar" — a fixed KIND then the name,
                # at column 0. Signature/type lines are indented and must be
                # skipped (their tokens like "str" are also identifiers).
                _DECL_RE = _re.compile(r"^(method|function|class|interface|field|module|file)\s+([A-Za-z_][\w.]*)\b")
                for raw in proc.stdout.splitlines():
                    line = _clean(raw)
                    if not line or line.startswith("Search Results"):
                        continue
                    m = _DECL_RE.match(line)
                    if m:
                        syms.append(m.group(2))
                if syms:
                    return syms[:10]
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("[student_plan] codegraph query failed: %s", e)

    # Fallback: grep for the query term in the repo (stdlib-only).
    root = repo_root or Path(__file__).parent.parent
    try:
        hit = next(
            (p for p in root.rglob("*.py")
             if p.is_file() and query.lower() in p.read_text(errors="ignore").lower()),
            None,
        )
        return [hit.name] if hit else []
    except OSError:
        return []


def _synthesize_subtasks(task_prompt: str, complexity: int) -> List[str]:
    """Generate bite-sized sub-tasks from a free-form complex prompt.

    Rank 7: before templating, query codegraph for symbols related to the
    task so the plan acknowledges existing structure (e.g. the function the
    student is extending). The search is offline-safe and never blocks.
    """
    # Rank 7 semantic search — enrich the plan with existing structure.
    base = task_prompt.strip().rstrip(".") or "the task"
    relevant = codegraph_search(base)
    n = max(MIN_SUBTASKS, min(complexity, MAX_SUBTASKS))
    # Deterministic decomposition: scaffold → implement → verify → finalize.
    templates = [
        f"Scaffold the structure needed for: {base}",
        f"Implement the core logic for: {base}",
        f"Write a failing test for the core behavior of: {base}",
        f"Refactor and simplify the implementation of: {base}",
        f"Run the test suite and fix any regressions from: {base}",
        f"Document and finalize the result of: {base}",
        f"Add edge-case handling for: {base}",
        f"Verify acceptance criteria for: {base}",
    ]
    sub_tasks = templates[:n]
    # Rank 7: append a "review existing symbols" step only when the plan has
    # room, so we never exceed MAX_SUBTASKS (tests assert the cap holds).
    if relevant and len(sub_tasks) < MAX_SUBTASKS:
        sub_tasks.append(
            "Review existing related symbols before editing: " + ", ".join(relevant[:5])
        )
    return sub_tasks


def _pad_subtasks(task_prompt: str, sub_tasks: List[str]) -> List[str]:
    """Ensure at least MIN_SUBTASKS exist (split a single step if needed)."""
    if not sub_tasks:
        sub_tasks = [task_prompt.strip()]
    while len(sub_tasks) < MIN_SUBTASKS:
        last = sub_tasks[-1]
        sub_tasks.append(f"Review and refine: {last}")
    return sub_tasks[:MAX_SUBTASKS]


def _render_plan_markdown(task_prompt: str, task_id: str, sub_tasks: List[str]) -> str:
    lines = [
        f"# Plan: {task_id}",
        "",
        "## Goal",
        task_prompt.strip(),
        "",
        "## Sub-tasks (2-5 min each, execute in order)",
        "",
    ]
    for i, st in enumerate(sub_tasks, 1):
        lines.append(f"{i}. {st}")
    lines.append("")
    lines.append("## Notes")
    lines.append("- Each sub-task runs as its own CE/TDD loop.")
    lines.append("- A failed sub-task loops back to THAT task only, not the whole plan.")
    lines.append("")
    return "\n".join(lines)


def execute_plan(
    plan: Dict[str, Any],
    *,
    role: str = "coder",
    domain: str = "python-coding",
    difficulty: str = "easy",
    store=None,
    repo: str = "__global__",
    run_leaf_fn=None,
) -> Dict[str, Any]:
    """Execute each sub-task via run_leaf (CE-enabled per-task TDD loop).

    Args:
        plan: the dict returned by generate_plan().
        run_leaf_fn: injectable dispatch fn (defaults to leaf.run_leaf) so tests
            can stub it without real Orca/LLM calls.

    Returns:
        {
            "task_id": str,
            "sub_task_results": list[dict],  # one result per sub-task
            "all_passed": bool,
        }
    """
    if run_leaf_fn is None:
        from leaf import run_leaf
        run_leaf_fn = run_leaf

    sub_task_results: List[dict] = []
    for st in plan["sub_tasks"]:
        result = run_leaf_fn(
            task_prompt=st,
            role=role,
            domain=domain,
            difficulty=difficulty,
            store=store,
            repo=repo,
            ce_enabled=True,
            complex_task=True,
        )
        sub_task_results.append(result)
        # Stop early if a sub-task hard-fails (don't waste cycles on dependents).
        if result.get("status") not in ("success", None):
            logger.warning("student_plan: sub-task failed, halting plan: %s", st)
            break

    all_passed = all(r.get("status") == "success" for r in sub_task_results)
    return {
        "task_id": plan["task_id"],
        "sub_task_results": sub_task_results,
        "all_passed": all_passed,
    }
