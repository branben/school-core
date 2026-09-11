"""conductor/tasks.py — Task fetching and kanban integration.

Handles fetching ready tasks from the bd (beads) kanban tracker, mapping
issue types to domains, building task prompts, and the default task rotation.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from school_core.principal import DOMAIN_ROLE


def _map_domain_from_issue_type(issue_type: str, title: str, description: str) -> str:
    """Map a bd issue_type to a Director domain.

    bd issues don't carry GitHub labels, only an ``issue_type`` string
    (``bug``, ``enhancement``, ``chore``, etc.).  We map by category only —
    NOT via title keyword matching (which would misclassify e.g. a bug
    titled "test failures" as ``python-testing``).

    ``bug`` → ``debugging``, ``enhancement`` → ``code-implementation``,
    everything else (chore, task, unknown) → ``_default`` so the universal
    ``coder`` role handles it via DOMAIN_ROLE["_default"].
    """
    if issue_type == "bug":
        return "debugging"
    if issue_type == "enhancement" or issue_type == "feature":
        return "code-implementation"
    return "_default"


def _build_task_from_issue(issue: dict) -> str:
    """Build a task prompt string from a bd issue dict.

    The ``description`` field from ``bd ready --json`` contains a line like
    ``GitHub: https://github.com/owner/repo/issues/N`` followed by the issue
    body (if any).  We include the URL for traceability and the body for
    context.
    """
    title = issue.get("title", "")
    description = issue.get("description", "") or ""
    parts = [title] if title else []
    if description:
        parts.append(description)
    task = "\n\n".join(parts)
    return task


def _fetch_ready_from_kanban() -> list[tuple[str, str, str, str]]:
    """Fetch ready tasks from the bd (beads) kanban tracker.

    Runs ``bd ready --json`` and maps each returned issue to a
    ``(domain, task, role, bd_id)`` tuple.  The ``bd_id`` is the native
    issue id (e.g. ``school-core-7bk``) retained so the principal can
    call ``bd close`` after the task is processed.

    Returns an empty list when ``bd`` is unavailable, returns no ready
    work, or exits non-zero — callers fall back to ``_default_tasks()``.
    """
    try:
        result = subprocess.run(
            ["bd", "ready", "--json"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        issues = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []
    tasks: list[tuple[str, str, str, str]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        title = issue.get("title", "")
        description = issue.get("description", "")
        issue_type = issue.get("issue_type", "")
        domain = _map_domain_from_issue_type(issue_type, title, description)
        role = DOMAIN_ROLE.get(domain, DOMAIN_ROLE["_default"])
        task = _build_task_from_issue(issue)
        bd_id = issue.get("id", "")
        tasks.append((domain, task, role, bd_id))
    return tasks


def _fetch_dispatch_tasks() -> list[tuple]:
    """Fetch dispatchable tasks: bd ready first, _default_tasks() fallback.

    Returns list of tuples.  When ``bd ready`` yields tasks, each tuple is
    ``(domain, task, role, bd_id)`` (4-tuple).  When ``bd ready`` is empty or
    unavailable, falls back to ``_default_tasks()`` which returns
    ``(domain, task)`` (2-tuple).

    The loops in _run_sync_loop / _run_async_loop unpack with a length
    check so both shapes work transparently.
    """
    kanban_tasks = _fetch_ready_from_kanban()
    if kanban_tasks:
        return kanban_tasks
    return _default_tasks()


def _complete_kanban_task(bd_id: Optional[str]) -> bool:
    """Mark a kanban task as done via ``bd close``.

    Called after the Principal pipeline finishes (acceptance or rejection)
    so the kanban board reflects that the task was processed.  Best-effort:
    failures are logged but never crash the Principal.
    """
    if not bd_id:
        return False
    try:
        result = subprocess.run(
            ["bd", "close", bd_id],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"  ⚠️ bd close {bd_id} exited {result.returncode}: "
                  f"{result.stderr.strip()[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ bd close {bd_id} timed out")
        return False
    except (FileNotFoundError, OSError) as e:
        print(f"  ⚠️ bd close {bd_id} failed: {e}")
        return False
    return True


def _default_tasks() -> list[tuple[str, str]]:
    """Default task rotation for loop mode."""
    return [
        ("code-search", "What single grep command finds all Python files with TODO comments recursively? One command only."),
        ("terminal", "What does this command do: `find . -name '*.py' -mtime -1 | xargs wc -l`? One sentence."),
        ("code-review", "Is `except Exception: pass` good practice? One word answer + one sentence why."),
        ("web-automation", "What CSS selector targets all <button> elements with class 'primary' inside a <form>? One selector."),
        ("python-coding", "Write def chunks(lst, n): yield successive n-sized chunks from lst using yield. Just code, no explanation."),
    ]
