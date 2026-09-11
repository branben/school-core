#!/usr/bin/env python3
"""conductor.py — The Principal. Orchestrates the full Agent School pipeline.

    Principal selects task → dispatch via StudentLeaf → bookbag → CTO+COO review → score

Two modes:

**Synchronous (Phase 1, default):**
    Each round: create leaf → LLM call + review inline → score → dispose.
    ``python conductor.py --loop --rounds 5``

**Async (Phase 2, ``--async``):**
    Boot persistent teacher worktrees (CTO + COO), dispatch all leaves with
    LLM calls only (no review), wait for teachers to fill verdicts, then
    batch-score and dispose.
    ``python conductor.py --loop --rounds 5 --async``

Utilities:
    python conductor.py --list-bookbags        # list all bookbags on disk
    python conductor.py --clean-worktrees      # remove stale study-* worktrees

NOTE: This module is now a backward-compatible re-export shim. The actual
implementation lives in the school_core package:
    - school_core.cli:main               — CLI entrypoint
    - school_core.principal               — Principal orchestration
    - school_core.conductor.dispatch      — Student dispatch logic
    - school_core.conductor.review        — CTO/COO review orchestration
    - school_core.conductor.scoring       — Score aggregation
    - school_core.conductor.daemon        — Activity server / serve state
    - school_core.teacher_worktree        — TeacherWorktree class
"""

from __future__ import annotations

# NOTE: No sys.path.insert needed — school_core.__init__.py handles path setup
# when imported from root-level scripts. This eliminates the import hack.
from school_core.soul import load_soul
from school_core.principal import (
    DOMAIN_ROLE,
    AGENT_TO_ROLE_CACHE,
    _agent_to_role,
    _resolve_agent,
    load_principal_soul,
    _parse_issue_ref,
    _principal_dispatch,
)
from school_core.conductor.dispatch import (
    _issue_task_shape,
    _persist_issue_route,
    _record_issue_dispatch_failure,
    _prepare_issue_context,
    _enrich_issue_task,
    _run_issue,
    _run_issue_async,
    _run_single_task,
    _run_sync_loop,
    _run_async_loop,
    _resume_loop,
    _compute_task_score,
    _cleanup_orphaned_leaves,
)
from school_core.conductor.tasks import (
    _map_domain_from_issue_type,
    _build_task_from_issue,
    _fetch_ready_from_kanban,
    _fetch_dispatch_tasks,
    _complete_kanban_task,
    _default_tasks,
)
from school_core.conductor.review import (
    _persist_acceptance,
    _validate_verdict,
)
from school_core.conductor.scoring import (
    _score_reviewed_bookbags,
    _score_and_print_round,
    _print_leaderboard,
)
from school_core.conductor.daemon import (
    SERVE_STATE_PATH,
    load_serve_state,
    save_serve_state,
    _send_to_terminal,
    _find_unreviewed_beads_for,
    _cleanup_legacy_automations,
    _gc_terminals,
    _launch_serve,
    _teardown_serve,
    principal_dispatch_loop,
    teacher_both_loop,
    orca_automations_list,
    orca_automations_create,
    orca_automations_remove,
    _create_hermes_cronjob,
    _boot_teachers,
    _shutdown_teachers,
    _find_or_create_terminal,
    _principal_prompt,  # noqa: F401
)
from school_core.teacher_worktree import TeacherWorktree

# Re-export main() from cli
from school_core.cli import main

# Re-export ActivityLog for backward compat with tests that import _log
from activity_log import ActivityLog as _ActivityLog
_log = _ActivityLog()

# Re-export REPO_GLOBAL for backward compat
from bookbag import REPO_GLOBAL

# Re-export remaining symbols that tests may import
from scripts.ce_router import classify_task, route_decision
from orca_executor import OrcaUnavailableError, OrcaExecutionManager
from github_fetcher import fetch_single_issue, load_config
from scoring import ScoreStore
from director import evaluate_and_update
from bookbag import (
    BookbagSignal,
    read_bookbag,
    list_bookbags,
    list_bookbags_full,
    wait_for_verdicts,
    locked_update_bookbag,
)
from school_mail import notify_verdict, notify_issue_alert
from leaf import run_leaf, StudentLeaf
from principal_doubt import run_doubt_cycle
from src.entire_review import run_entire_review

if __name__ == "__main__":
    main()
