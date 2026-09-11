"""cli.py — argparse entrypoints for the Conductor (Principal).

Provides CLI flags:
    --task, --domain, --difficulty, --agent
    --loop, --rounds, --async
    --list-bookbags, --clean-worktrees
    --serve, --stop-serve
    --repo, --resume, --doubt, --issue
    --principal-daemon, --teacher-both-daemon
    --daemon-interval, --max-ticks, --once
    --serve-state-path, --gc-terminals
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def conductor_main(argv: list[str] | None = None) -> int:
    """Run the Conductor (Principal) — orchestrates the Agent School pipeline."""
    main(argv)
    return 0


def director_main(argv: list[str] | None = None) -> int:
    """Run the Director — task selection and review."""
    # TODO: Import from refactored modules once split is complete
    from director import main as _legacy_main
    return _legacy_main(argv)


def bridge_main(argv: list[str] | None = None) -> int:
    """Run the Issue Bridge — GitHub issue polling and conversion."""
    # TODO: Import from refactored modules once split is complete
    from issue_bridge import main as _legacy_main
    return _legacy_main(argv)


def main(argv: list[str] | None = None) -> None:
    """Main entrypoint for the Conductor CLI."""
    parser = argparse.ArgumentParser(description="Agent School Conductor (Principal)")
    parser.add_argument("--task", default="Write a Python function is_palindrome(s: str) -> bool. Just the code, no explanation.",
                        help="Task prompt")
    parser.add_argument("--domain", default="python-coding", help="Task domain")
    parser.add_argument("--difficulty", default="easy", help="Task difficulty")
    parser.add_argument("--agent", default=None, help="Force a specific agent/role")
    parser.add_argument("--issue", default=None,
                        help="GitHub issue to process, e.g. 'owner/repo#123' or a full "
                             "issue URL. Fetched via gh, classified, and dispatched through "
                             "the Principal pipeline (run_leaf -> two-judge review).")
    parser.add_argument("--loop", action="store_true", help="Autonomous loop mode")
    parser.add_argument("--rounds", type=int, default=5, help="Number of rounds in loop mode")
    parser.add_argument("--async", action="store_true", dest="async_mode",
                        help="Async dispatch: boot teachers, dispatch all leaves, poll for verdicts")
    parser.add_argument("--handoff-timeout", type=int, default=900,
                        help="Seconds to wait for teacher verdicts in async mode (default 900)")
    parser.add_argument("--list-bookbags", action="store_true", help="List all bookbags on disk")
    parser.add_argument("--clean-worktrees", action="store_true",
                        help="Remove all study-* worktrees created by previous runs")
    parser.add_argument("--serve", action="store_true", dest="serve",
                        help="Launch the school via NATIVE Orca primitives: a "
                             "persistent principal automation (Orca owns the "
                             "schedule) plus persistent teacher worktrees. "
                             "Retires the old while-True pane loop.")
    parser.add_argument("--stop-serve", action="store_true", dest="stop_serve",
                        help="Tear down the --serve school: remove the principal "
                             "automation and dispose teacher worktrees.")
    parser.add_argument("--repo", default=None,
                        help="Target repo slug (owner/repo) for this dispatch. "
                             "Namespaces the bookbag + score store per repo so a "
                             "standalone --task/--issue lands in the right repo's "
                             "files instead of __global__. Defaults to __global__.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume after crash: scan bookbags for partial verdicts, rediscover teachers, complete handoffs")
    parser.add_argument("--doubt", action="store_true", dest="doubt_enabled",
                        help="Rank 3: run a Doubt-Driven Development cycle on each "
                             "routing decision before dispatch (adversarial review "
                             "of gate/role/model selection). Off by default for "
                             "backward compat.")
    parser.add_argument("--principal-daemon", action="store_true", dest="principal_daemon",
                        help="[Path A] Run the Principal as a persistent Python loop. "
                             "Dispatches one task per tick from the default rotation, "
                             "scores inline, sleeps --daemon-interval seconds. NEVER "
                             "exits on its own; Ctrl-C to stop. Intended to run inside "
                             "the agent-school-principal terminal launched by --serve.")
    parser.add_argument("--teacher-both-daemon", action="store_true", dest="teacher_both_daemon",
                        help="[Path A] Run a single teacher daemon that fills BOTH "
                             "CTO and COO verdicts per tick. Replaces the 2 separate "
                             "agent-school-teacher-{cto,coo} cron automations with "
                             "ONE persistent Python process. Intended to run inside "
                             "the agent-school-teacher-both terminal launched by --serve.")
    parser.add_argument("--daemon-interval", type=int, default=300,
                        help="Tick interval (seconds) for --principal-daemon and "
                             "--teacher-both-daemon. Default 300 (5 min). The principal "
                             "in --serve is launched at 1800 (30 min); the teacher-both "
                             "daemon in --serve is launched at 60 (1 min) — beads are "
                             "filtered out by _find_unreviewed_beads_for once a verdict "
                             "is written, so re-reviewing the same bead is impossible.")
    parser.add_argument("--max-ticks", type=int, default=0,
                        help="[TEST ONLY] Stop the daemon after N ticks (0 = unlimited). "
                             "Production daemons must NOT pass --max-ticks — they should "
                             "run until --stop-serve closes their terminal.")
    parser.add_argument("--once", action="store_true",
                        help="[TEST ONLY] Run ONE tick of the daemon and exit. Equivalent "
                             "to --max-ticks 1. Production daemons must NOT pass --once.")
    parser.add_argument("--serve-state-path", default=None,
                        help="[TEST ONLY] Override the daemon-serve persistence file "
                             "(default: ~/.school-core/serve-state.json). Tests pass a "
                             "tmpdir path so they never touch the user's actual file.")
    parser.add_argument("--gc-terminals", action="store_true", dest="gc_terminals",
                        help="[Path A] Scan ``orca terminal list --json`` and close "
                             "any terminal whose title is EMPTY or starts with "
                             "``agent-school-`` (stale residue from prior buggy "
                             "serves). Best-effort per-close, never raises. "
                             "Also runs automatically as the orphan-terminal cleanup phase of --stop-serve "
                             "so a re-serve always lands on a clean slate. "
                             "Use --gc-terminals-dry-run to preview without closing.")
    parser.add_argument("--gc-terminals-dry-run", action="store_true",
                        dest="gc_terminals_dry_run",
                        help="[--gc-terminals] List matches without closing. "
                             "Useful as a sanity check before pulling the trigger "
                             "on a real cleanup pass.")

    args = parser.parse_args(argv)

    # ── Serve mode: native Orca spawn (Gap C/D) ────────────────────────
    if args.stop_serve:
        from school_core.conductor.daemon import _teardown_serve
        _teardown_serve(Path(args.serve_state_path) if args.serve_state_path else None)
        return

    if args.serve:
        from school_core.conductor.daemon import _launch_serve
        _launch_serve(Path(args.serve_state_path) if args.serve_state_path else None)
        return

    # ── Daemon modes (Path A) ────────────────────────────────────────────
    if args.gc_terminals:
        from school_core.conductor.daemon import _gc_terminals
        n = _gc_terminals(dry_run=args.gc_terminals_dry_run)
        suffix = " (dry-run, nothing closed)" if args.gc_terminals_dry_run else ""
        print(f"\n✅ gc-terminals: closed {n} orphaned terminal(s){suffix}")
        return

    if args.principal_daemon:
        from school_core.conductor.daemon import principal_dispatch_loop
        principal_dispatch_loop(args)
        return

    if args.teacher_both_daemon:
        from school_core.conductor.daemon import teacher_both_loop
        teacher_both_loop(args)
        return

    # ── Standalone utilities ─────────────────────────────────────────────

    if args.list_bookbags:
        from bookbag import list_bookbags_full, read_bookbag
        pairs = list_bookbags_full()
        print(f"Bookbags on disk ({len(pairs)}):")
        for repo, b in pairs:
            bag = read_bookbag(b, repo)
            if bag:
                accepted = "\u2705" if bag.get("accepted") else "\u274c"
                student = bag.get("student", "?")
                domain = bag.get("domain", "?")
                print(f"  {accepted} [{repo}] {b:36s} {student:12s} {domain:20s}")
            else:
                print(f"  ?  [{repo}] {b}")
        return

    if args.clean_worktrees:
        from orca_executor import OrcaExecutionManager, OrcaUnavailableError
        try:
            mgr = OrcaExecutionManager()
            count = mgr.cleanup_worktrees_by_prefix("study-")
            print(f"\U0001f9f9 Removed {count} study-* worktrees")
        except OrcaUnavailableError as e:
            print(f"\u26a0 Orca not available: {e}")
        return

    if args.issue:
        from scoring import ScoreStore
        from school_core.conductor.dispatch import _run_issue
        from school_core.principal import _parse_issue_ref
        repo_slug = None
        try:
            o, r, _ = _parse_issue_ref(args.issue)
            repo_slug = f"{o}/{r}"
        except Exception:
            repo_slug = None
        store = ScoreStore(repo=repo_slug or "__global__")
        _run_issue(args, store)
        return

    # ── Core pipeline ────────────────────────────────────────────────────

    from scoring import ScoreStore
    from school_core.conductor.dispatch import _run_issue, _run_single_task
    from school_core.principal import _parse_issue_ref, _principal_dispatch, _resolve_agent

    # Namespace the score store by --repo (if given) so standalone --task
    # dispatch keeps a role's learning per-repo instead of __global__.
    store = ScoreStore(repo=args.repo or "__global__")

    if args.resume:
        from school_core.conductor.dispatch import _resume_loop
        _resume_loop(args, store)
        return

    if args.loop:
        from school_core.conductor.dispatch import _run_async_loop, _run_sync_loop
        if args.async_mode:
            _run_async_loop(args, store, repo=args.repo or "__global__")
        else:
            _run_sync_loop(args, store)

    else:
        _run_single_task(args, store)


if __name__ == "__main__":
    main()
