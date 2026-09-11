"""conductor/loops.py — Sync, async, and resume dispatch loops.

Handles the Phase 1 synchronous loop (dispatch one at a time with inline review),
Phase 2 async loop (boot teachers, dispatch all, poll for verdicts), and the
resume-after-crash recovery loop.
"""

from __future__ import annotations

from typing import Optional

from bookbag import list_bookbags_full, read_bookbag, wait_for_verdicts
from director import evaluate_and_update
from leaf import StudentLeaf
from orca_executor import OrcaExecutionManager, OrcaUnavailableError
from principal_doubt import run_doubt_cycle

from school_core.conductor.daemon import _boot_teachers, _shutdown_teachers
from school_core.conductor.review import _persist_acceptance, _validate_verdict
from school_core.conductor.scoring import _print_leaderboard, _score_and_print_round, _score_reviewed_bookbags
from school_core.conductor.tasks import _complete_kanban_task, _fetch_dispatch_tasks
from school_core.principal import DOMAIN_ROLE, _principal_dispatch


def _run_sync_loop(args, store):
    """Phase 1 synchronous loop: dispatch one at a time with inline review."""
    from school_core.principal import load_principal_soul

    print(f"🔄 Principal loop mode — {args.rounds} rounds (synchronous)\n")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")

    domain_tasks = _fetch_dispatch_tasks()

    for i in range(args.rounds):
        task_tuple = domain_tasks[i % len(domain_tasks)]
        # 4-tuple: (domain, task, role, bd_id) — from kanban bridge
        # 2-tuple: (domain, task) — from _default_tasks() fallback
        if len(task_tuple) == 4:
            domain, task, role, bd_id = task_tuple
        else:
            domain, task = task_tuple
            role = DOMAIN_ROLE.get(domain, "student")
            bd_id = None
        round_num = i + 1

        print(f"--- Round {round_num}/{args.rounds} [{role} / {domain}] ---")

        try:
            result = _principal_dispatch(
                task=task, role=role, domain=domain,
                difficulty=args.difficulty, store=store, repo=args.repo,
                doubt_enabled=args.doubt_enabled,
            )
        except Exception as e:
            print(f"  ❌ Dispatch failed: {e}")
            _complete_kanban_task(bd_id)
            continue

        _complete_kanban_task(bd_id)
        _score_and_print_round(result, store)
        print()

    _print_leaderboard(store)


def _run_async_loop(args, store, repo: str = "__global__"):
    """Phase 2 async loop: boot teachers, dispatch all, poll for verdicts.

    Pipeline:
        1. Boot CTO + COO teacher worktrees, start review loops
        2a. Create all leaf worktrees + write briefs (fast, no LLM calls)
        2b. Run LLM calls for each leaf + signal teachers ready
            (teachers can review earlier bookbags while later LLMs run)
        3. Poll bookbags for completed verdicts (any order)
        4. Score completed bookbags + dispose leaves
        5. Shutdown teachers
    """
    from school_core.principal import load_principal_soul

    print(f"🔄 Principal async loop — {args.rounds} rounds (concurrent dispatch)\n")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")

    # ── Step 1: Boot teachers ─────────────────────────────────────────
    print("🏫 Booting teacher worktrees...")
    teachers = _boot_teachers(repo)
    if len(teachers) < 2:
        print("  ⚠️ Could not boot both teachers (need CTO+COO) — falling back to sync mode")
        if teachers:
            _shutdown_teachers(teachers)
        _run_sync_loop(args, store)
        return

    cto = teachers.get("cto")
    coo = teachers.get("coo")
    print(f"  ✅ CTO worktree: {cto.worktree_name}")
    print(f"  ✅ COO worktree: {coo.worktree_name}")
    print()

    # ── Step 2a: Create all leaf worktrees (fast, no LLM) ─────────────
    domain_tasks = _fetch_dispatch_tasks()
    leaves: list[tuple[StudentLeaf, str, str, str, Optional[str]]] = []

    print(f"\U0001f333 Creating {args.rounds} leaf worktrees...")
    for i in range(args.rounds):
        task_tuple = domain_tasks[i % len(domain_tasks)]
        # 4-tuple: (domain, task, role, bd_id) — from kanban bridge
        # 2-tuple: (domain, task) — from _default_tasks() fallback
        if len(task_tuple) == 4:
            domain, task, role, bd_id = task_tuple
        else:
            domain, task = task_tuple
            role = DOMAIN_ROLE.get(domain, "student")
            bd_id = None

        leaf = None
        try:
            leaf = StudentLeaf(role=role, domain=domain, difficulty=args.difficulty, store=store)
            leaf.boot()
            leaf.write_brief(task)
            leaves.append((leaf, task, role, domain, bd_id))
        except Exception as e:
            print(f"  ❌ Leaf {i+1}/{args.rounds} [{role}] boot failed: {e}")
            if leaf is not None:
                try:
                    leaf.dispose()
                except Exception:
                    pass
            _complete_kanban_task(bd_id)  # close the bd task so it doesn't re-dispatch

    print(f"  ✅ {len(leaves)}/{args.rounds} worktrees created")
    print()

    if not leaves:
        print("  No leaves could be created — aborting.")
        _shutdown_teachers(teachers)
        return

    # ── Step 2b: Run LLM calls + signal teachers ───────────────────────
    dispatched: list[tuple[StudentLeaf, dict, Optional[str]]] = []
    print(f"\U0001f4ac Running {len(leaves)} LLM calls (teachers review as bookbags arrive)...")
    print()

    for idx, (leaf, task, role, domain, bd_id) in enumerate(leaves):
        print(f"  {idx+1}/{len(leaves)} [{role}/{domain}] LLM call...")

        try:
            # Rank 3: Doubt-Driven Development on the routing decision. In async
            # mode the leaf is already booted with its gate, so we record the
            # doubt_log for traceability and apply re-routing on the synchronous
            # (run_leaf) paths where re-booting with a reconciled gate is safe.
            if getattr(args, "doubt_enabled", False):
                claim = (
                    f"Routing task to {role} ({domain}) via gate {args.difficulty} "
                    f"through OmniRoute / Orca student leaf (async)."
                )
                extract = {
                    "task": task, "role": role, "domain": domain,
                    "gate": args.difficulty, "model": "omni-route/default",
                    "lens": "principal",
                }
                doubt_log = run_doubt_cycle(claim=claim, extract=extract, max_cycles=1)
            else:
                doubt_log = None

            result = leaf.run_via_hermes(task)  # Hermes in Orca terminal
            if doubt_log is not None:
                result["doubt_log"] = doubt_log

            if result.get("status") == "success":
                leaf.signal_ready()
                dispatched.append((leaf, result, bd_id))
                print(f"    ✅ bead={leaf.bead[:20]} ({len(result.get('response', ''))} chars) — teachers notified")
            else:
                error = result.get("error", result.get("status", "unknown"))
                print(f"    ❌ {result.get('status')}: {error}")
                leaf.dispose()
                _complete_kanban_task(bd_id)
        except Exception as e:
            print(f"    ❌ Hermes call failed: {e}")
            try:
                leaf.dispose()
            except Exception:
                pass
            _complete_kanban_task(bd_id)

    print()
    print(f"\U0001f4e8 Dispatched {len(dispatched)}/{len(leaves)} tasks to teachers")
    print()

    # ── Step 3: Poll for teacher verdicts ──────────────────────────────
    if dispatched:
        print(f"\U000023f3 Polling for teacher verdicts (timeout={args.handoff_timeout}s each)...")
        print()

        completed = 0
        for idx, (leaf, result, bd_id) in enumerate(dispatched):
            bead = leaf.bead
            role = result.get("agent", "?")
            domain = result.get("domain", "?")
            label = f"[{role}/{domain}]"

            try:
                cto_v, coo_v = wait_for_verdicts(
                    bead, repo=repo, timeout=args.handoff_timeout
                )
                bag = read_bookbag(bead, repo)
                if bag:
                    accepted = _persist_acceptance(bead, cto_v, coo_v, repo=repo)
                    _validate_verdict(bead, repo=repo)
                    # Teachers write cto_findings/coo_findings (not a combined
                    # "findings" key) in async mode — combine for display.
                    findings = (bag.get("cto_findings", []) or []) + (
                        bag.get("coo_findings", []) or []
                    )
                    result["review"] = {
                        "cto_verdict": cto_v,
                        "coo_verdict": coo_v,
                        "cto_score": bag.get("cto_score", 0),
                        "coo_score": bag.get("coo_score", 0),
                        "findings": findings,
                        "accepted": accepted,
                    }
                    result["task_score"] = _compute_task_score(bag)

                completed += 1
                accepted = result["review"].get("accepted", False)
                mark = "\u2705" if accepted else "\u274c"
                print(f"  {idx+1}/{len(dispatched)} {label} "
                      f"CTO={cto_v} COO={coo_v} {mark}")
                # Notify the human operator via AgentMail (best-effort; never crashes).
                try:
                    from school_mail import notify_verdict
                    notify_verdict(
                        bead, accepted, cto_v, coo_v,
                        repo=repo,
                        summary=(findings or []) and f"{len(findings)} findings",
                        entire_findings=(bag or {}).get("entire_findings", []),
                        entire_status=(bag or {}).get("entire_status", "unknown"),
                        cto_findings=(bag or {}).get("cto_findings", []),
                        coo_findings=(bag or {}).get("coo_findings", []),
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠ AgentMail notify failed: {e}")

            except Exception as e:
                print(f"  {idx+1}/{len(dispatched)} {label} ❌ Timeout: {e}")

            # Score and dispose
            task_score = result.get("task_score", 0)
            evaluate_and_update(result, task_score, store=store)
            leaf.dispose()
            _complete_kanban_task(bd_id)

        print()
        print(f"\u2705 Completed: {completed}/{len(dispatched)} verdicts received")

    # ── Step 4: Shutdown teachers ──────────────────────────────────────
    print()
    _shutdown_teachers(teachers)

    _print_leaderboard(store)


def _resume_loop(args, store):
    """Resume after a crash: scan bookbags, boot teachers, finish handoffs.

    When the principal crashes mid-handoff in async mode, bookbags are left
    with partial verdicts (e.g., CTO filled but COO didn't). This function:

        1. Scans ~/.hermes/bookbag/ for bookbags with partial verdicts
        2. Also finds fully-reviewed bookbags that were never scored
        3. Boots teachers (which rediscover existing worktrees if present)
        4. Waits for remaining verdicts on partial bookbags
        5. Scores completed + timed-out bookbags
        6. Cleans up orphaned study-* leaf worktrees
    """
    print("\U0001f504 Resume mode — recovering from interruption\n")

    # ── Step 1: Scan bookbags ──────────────────────────────────────────
    all_beads = list_bookbags_full()
    if not all_beads:
        print("  No bookbags found — nothing to resume.")
        return

    partial: list[dict] = []   # one verdict filled, other empty
    reviewed: list[dict] = []  # both verdicts filled, not yet scored
    empty: list[dict] = []     # neither verdict filled

    for repo, bead in all_beads:
        bag = read_bookbag(bead, repo)
        if not bag:
            continue

        cto = bag.get("cto_verdict", "")
        coo = bag.get("coo_verdict", "")

        if cto and coo:
            reviewed.append(bag)
        elif cto or coo:
            partial.append(bag)
        else:
            empty.append(bag)

    print(f"  \U0001f4cb Bookbags: {len(reviewed)} reviewed, {len(partial)} partial, {len(empty)} empty")

    if not partial and not empty:
        # All bookbags are already reviewed — just score them
        print("  All bookbags have both verdicts — scoring and cleaning up.\n")
        _score_reviewed_bookbags(reviewed, store)
        _cleanup_orphaned_leaves()
        _print_leaderboard(store)
        return

    # ── Step 2: Boot teachers for remaining verdicts ────────────────────
    print()
    print("\U0001f3eb Booting teachers to finish reviews...")
    # NOTE: _boot_teachers() always creates new terminals and starts new
    # run_loop() processes, even when TeacherWorktree.boot() rediscovers
    # existing worktrees from a pre-crash session. The old terminals become
    # orphaned (no real harm — polls are idempotent), but the user may see
    # duplicate teacher terminals in Orca's UI.
    teachers = _boot_teachers()
    if len(teachers) < 2:
        print("  ⚠️ Cannot resume — need both CTO+COO teachers")
        if teachers:
            _shutdown_teachers(teachers)
        return
    print()

    # ── Step 3: Wait for remaining verdicts ─────────────────────────────
    pending = partial + empty  # All bookbags that need at least one verdict
    if pending:
        print(f"\U000023f3 Waiting for {len(pending)} bookbags (timeout={args.handoff_timeout}s each)...")
        print()

        completed = 0
        for idx, bag in enumerate(pending):
            bead = bag["bead"]
            student = bag.get("student", "?")
            domain = bag.get("domain", "?")

            try:
                cto_v, coo_v = wait_for_verdicts(bead, timeout=args.handoff_timeout)
                bag_refreshed = read_bookbag(bead) or bag
                bag_refreshed["cto_verdict"] = cto_v
                bag_refreshed["coo_verdict"] = coo_v
                bag_refreshed["accepted"] = _persist_acceptance(bead, cto_v, coo_v)
                reviewed.append(bag_refreshed)
                completed += 1
                print(f"  {idx+1}/{len(pending)} {bead[:30]} [{student}/{domain}] "
                      f"CTO={cto_v} COO={coo_v}")
            except Exception as e:
                print(f"  {idx+1}/{len(pending)} {bead[:30]} [{student}/{domain}] ❌ Timeout")
                # Mark as reviewed anyway (will be scored as failure)
                bag["cto_verdict"] = bag.get("cto_verdict", "FAIL")
                bag["coo_verdict"] = bag.get("coo_verdict", "FAIL")
                reviewed.append(bag)

        print()
        print(f"\u2705 Verdicts received: {completed}/{len(pending)}")

    # ── Step 4: Score all reviewed bookbags ─────────────────────────────
    _shutdown_teachers(teachers)
    print()
    _score_reviewed_bookbags(reviewed, store)

    # ── Step 5: Clean up orphaned leaf worktrees ────────────────────────
    _cleanup_orphaned_leaves()

    _print_leaderboard(store)


def _cleanup_orphaned_leaves() -> None:
    """Remove orphaned study-* leaf worktrees left by a crash."""
    try:
        mgr = OrcaExecutionManager()
        count = mgr.cleanup_worktrees_by_prefix("study-")
        if count > 0:
            print(f"\U0001f9f9 Cleaned up {count} orphaned leaf worktrees")
    except OrcaUnavailableError:
        print("\u26a0️ Orca not available — skipping leaf cleanup")
    except Exception as e:
        print(f"\u26a0️ Leaf cleanup error: {e}")


def _compute_task_score(bag: dict) -> float:
    """Compute a task score from bookbag verdict + scores.

    Uses the same formula as the async loop: accepted → max(60, combined),
    rejected → min(40, combined). Shared by _run_async_loop() and
    _score_reviewed_bookbags().
    """
    cto_score = bag.get("cto_score", 0)
    coo_score = bag.get("coo_score", 0)
    combined = (cto_score + coo_score) / 2.0
    accepted = bag.get("accepted", bag.get("cto_verdict") == "PASS" and bag.get("coo_verdict") == "PASS")
    return max(60, combined) if accepted else min(40, combined)
