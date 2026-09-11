

"""conductor/dispatch.py — Student dispatch logic (run_leaf, StudentLeaf integration).

Handles GitHub issue fetching, task shaping, route persistence, context
preparation, and the sync/async issue dispatch paths.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bookbag import (
    BookbagSignal,
    list_bookbags_full,
    locked_update_bookbag,
    read_bookbag,
    wait_for_verdicts,
)
from director import evaluate_and_update
from github_fetcher import fetch_single_issue
from leaf import StudentLeaf
from orca_executor import OrcaExecutionManager, OrcaUnavailableError
from scoring import ScoreStore
from scripts.ce_router import classify_task, route_decision
from src.entire_review import run_entire_review

from school_core.conductor.review import _persist_acceptance, _validate_verdict
from school_core.conductor.tasks import _complete_kanban_task, _default_tasks, _fetch_dispatch_tasks
from school_core.conductor.loops import _run_sync_loop, _run_async_loop, _resume_loop, _cleanup_orphaned_leaves, _compute_task_score
from school_core.principal import DOMAIN_ROLE, _parse_issue_ref, _principal_dispatch, _resolve_agent

_log = None  # lazy import to avoid circular deps


def _get_log():
    global _log
    if _log is None:
        from activity_log import ActivityLog
        _log = ActivityLog()
    return _log


def _issue_task_shape(issue: dict) -> dict:
    """Build the conservative CE shape for a fetched GitHub issue.

    The single-issue async path historically bypassed the Principal router.
    Until GitHub triage exposes richer policy flags, every fetched issue is
    treated as a new implementation; this preserves the existing dispatch
    behavior while making the route contract explicit and durable.
    """
    return classify_task(
        is_new_implementation=True,
        complexity=1,
    )


def _persist_issue_route(
    bead: str,
    repo: str,
    task_shape: dict,
    strict: bool = True,
) -> dict:
    """Persist and return the CE route for a real issue bookbag.

    A ready signal without a durable route is an invalid handoff: teachers
    could review a bookbag that cannot explain how it was dispatched. Make the
    write result part of the handoff contract instead of treating it as a
    best-effort telemetry update.
    """
    routing = route_decision(
        task_shape,
        bead=bead,
        repo=repo,
        bookbag_writer=locked_update_bookbag,
    )
    if not routing.get("logged") and strict:
        raise RuntimeError(
            f"CE route could not be persisted for bead {bead!r}; "
            "ready signal withheld"
        )
    return routing


def _record_issue_dispatch_failure(
    bead: Optional[str],
    repo: str,
    error: str,
    issue_number: Optional[int] = None,
    issue_title: str = "",
) -> None:
    """Persist a terminal pre-review failure and alert direct issue callers.

    A direct async issue dispatch has no bridge cycle around it to record the
    failure. Store the state in the bead's bookbag so teacher/resume tooling
    can distinguish a failed dispatch from an unstarted pending handoff.
    Alerting is only enabled when GitHub issue identity is supplied; generic
    async loop callers retain their existing best-effort behavior.
    """
    from school_mail import notify_issue_alert

    failed_at = datetime.now(timezone.utc).isoformat()
    fields = {
        "dispatch_status": "school-failed",
        "dispatch_error": str(error)[:1000],
        "dispatch_failed_at": failed_at,
    }
    if bead:
        try:
            written = locked_update_bookbag(bead, repo, **fields)
            if written is None:
                print(f"  ⚠️ dispatch failure status for {bead} was not persisted")
        except Exception as persist_error:  # noqa: BLE001
            print(f"  ⚠️ could not persist dispatch failure for {bead}: {persist_error}")
    elif issue_number is not None:
        # Clone/teacher-boot failures happen before a student bead exists.
        # Keep a durable cycle record so a fresh checkout still explains why
        # the issue did not enter the teacher handoff protocol. Reuse the
        # bridge's atomic append helper so concurrent cycles cannot clobber it.
        path = Path(__file__).parent.parent.parent / "data" / "last_run.json"
        try:
            from issue_bridge import record_run
            record_run(
                path,
                {
                    "issue": issue_number,
                    "repo": repo,
                    "status": "school-failed",
                    "error": str(error)[:1000],
                },
            )
        except Exception as persist_error:  # noqa: BLE001
            print(f"  ⚠️ could not persist pre-dispatch failure: {persist_error}")

    if issue_number is not None:
        try:
            notify_issue_alert(
                issue_number,
                issue_title or f"Issue #{issue_number}",
                "school-failed",
                error=str(error),
                repo=repo,
                attempt=1,
            )
        except Exception as notify_error:  # noqa: BLE001
            print(f"  ⚠️ issue failure alert failed: {notify_error}")


def _prepare_issue_context(target_repo: str, task: str) -> tuple[Optional[Path], str]:
    """Clone a fresh issue target and build grounded context when possible.

    Clone failure is a hard boundary: running the issue in school-core would
    silently modify/review the wrong repository. Context extraction is softer:
    once the correct clone exists, a reader error may degrade to an empty
    context without changing the target path.
    """
    from repo_reader import clone_repo, build_codebase_context

    try:
        target_path = clone_repo(target_repo, force_fresh=True)
    except Exception as e:
        raise RuntimeError(f"could not clone target repo {target_repo}: {e}") from e
    if not target_path:
        raise RuntimeError(f"could not clone target repo {target_repo}")

    try:
        context = build_codebase_context(target_path, task)
    except Exception as e:
        print(f"  ⚠ codebase context unavailable for {target_repo}: {e}")
        context = ""
    return target_path, context


def _enrich_issue_task(task: str, codebase_context: str) -> str:
    """Prefix a task with grounded target-repo context when available."""
    if not codebase_context:
        return task
    return f"{codebase_context}\n\n## Issue\n{task}"


def _run_issue(args, store):
    """Fetch a single GitHub issue and dispatch it through the Principal pipeline."""
    try:
        owner, repo, number = _parse_issue_ref(args.issue)
    except ValueError as e:
        print(f"\u274c {e}")
        return

    print(f"\U0001f4e6 PRINCIPAL — fetching issue {owner}/{repo}#{number}")
    try:
        issue = fetch_single_issue(owner, repo, number)
    except Exception as e:
        print(f"\u274c Failed to fetch issue: {e}")
        return

    if issue is None:
        print(f"\u274c gh could not fetch {owner}/{repo}#{number} (auth? exists?)")
        return

    domain = issue["domain"]
    difficulty = issue["difficulty"]
    role = _resolve_agent(args)
    print(f"   Title : {issue['title']}")
    print(f"   Domain: {domain}  Difficulty: {difficulty}  Role: {role}")
    if issue.get("state") != "ready-for-agent":
        print(f"   ⚠ Classified state={issue.get('state')!r} (not 'ready-for-agent') — dispatching anyway")

    # Delegate to the pipeline. --async boots persistent teacher worktrees
    # (CTO+COO) and polls bookbags; otherwise run sync inline two-judge review.
    args.task = issue["prompt"]
    args.domain = domain
    args.difficulty = difficulty
    # Both issue modes must share the target-repo namespace. Without this,
    # the synchronous path writes its route to __global__ while its ScoreStore
    # and student artifact belong to owner/repo.
    args.repo = f"{owner}/{repo}"
    args.task_shape = _issue_task_shape(issue)
    args.issue_number = number
    args.issue_title = issue["title"]
    if args.async_mode:
        _run_issue_async(
            args,
            store,
            role,
            target_repo=f"{owner}/{repo}",
            task_shape=args.task_shape,
            issue_number=number,
            issue_title=issue["title"],
        )
    else:
        # Sync issue dispatch also receives the fresh target context. This
        # keeps `--issue` behavior consistent across sync and async modes.
        try:
            target_path, codebase_context = _prepare_issue_context(args.repo, args.task)
        except RuntimeError as e:
            print(f"  ❌ Issue dispatch aborted: {e}")
            _record_issue_dispatch_failure(
                None,
                args.repo,
                e,
                issue_number=number,
                issue_title=issue["title"],
            )
            return
        args.task = _enrich_issue_task(args.task, codebase_context)
        args.repo_path = target_path
        args.codebase_context_chars = len(codebase_context)
        try:
            result = _run_single_task(args, store)
            if isinstance(result, dict) and result.get("status") != "success":
                error = result.get("error", result.get("status", "issue dispatch failed"))
                _record_issue_dispatch_failure(
                    None,
                    args.repo,
                    error,
                    issue_number=number,
                    issue_title=issue["title"],
                )
        except Exception as e:
            print(f"  ❌ Issue dispatch failed: {e}")
            _record_issue_dispatch_failure(
                None,
                args.repo,
                e,
                issue_number=number,
                issue_title=issue["title"],
            )


def _run_issue_async(
    args,
    store,
    role,
    target_repo: Optional[str] = None,
    task_shape: Optional[dict] = None,
    issue_number: Optional[int] = None,
    issue_title: str = "",
):
    """Single-issue async path: boot teachers, run one leaf, poll verdicts.

    Mirrors _run_async_loop's topology for one issue — the CTO/COO teacher
    worktrees are persistent (visible in Orca's sidebar) and review the
    student's bookbag via the signal protocol, instead of the principal
    running both reviews inline.
    """
    from school_core.conductor.daemon import _boot_teachers, _shutdown_teachers
    from school_core.principal import load_principal_soul

    print(f"\U0001f504 PRINCIPAL — async dispatch {role} / {args.domain}")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")

    # Scope the score store to the target repo so a role's learned capacity
    # (EMA score across tasks) stays per-repo and never leaks across repos.
    repo_slug = target_repo or "__global__"
    store = ScoreStore(repo=repo_slug)

    try:
        teachers = _boot_teachers(repo=repo_slug)
    except Exception as e:
        if issue_number is not None:
            print(f"  ❌ Direct issue teacher boot failed: {e}")
            _record_issue_dispatch_failure(
                None,
                repo_slug,
                e,
                issue_number=issue_number,
                issue_title=issue_title,
            )
            return
        raise
    if len(teachers) < 2:
        print("  ⚠️ Could not boot both teachers — falling back to sync review")
        if teachers:
            _shutdown_teachers(teachers)
        if issue_number is not None:
            _record_issue_dispatch_failure(
                None,
                repo_slug,
                "could not boot both teachers",
                issue_number=issue_number,
                issue_title=issue_title,
            )
            return
        _run_single_task(args, store)
        return

    cto = teachers.get("cto")
    coo = teachers.get("coo")
    print(f"  ✅ CTO worktree: {cto.worktree_name}")
    print(f"  ✅ COO worktree: {coo.worktree_name}\n")

    leaf = None
    handoff_signaled = False
    target_path = None
    codebase_context = ""
    if target_repo:
        # Cross-repo dispatch: clone a FRESH copy of the target repo so the
        # student never starts from a contaminated/stale base tree.
        try:
            target_path, codebase_context = _prepare_issue_context(target_repo, args.task)
        except RuntimeError as e:
            print(f"  ❌ Async issue dispatch aborted: {e}")
            _record_issue_dispatch_failure(
                None,
                repo_slug,
                e,
                issue_number=issue_number,
                issue_title=issue_title,
            )
            _shutdown_teachers(teachers)
            return
    try:
        leaf = StudentLeaf(
            role=role,
            domain=args.domain,
            difficulty=args.difficulty,
            store=store,
            repo_path=target_path,
            repo=target_repo,
        )
        leaf.boot()

        # The real issue path owns the fresh clone, so give the student the
        # same grounded context that the bridge path provides. This also makes
        # the reported context size truthful instead of implying that a zero
        # length context was collected from the target repository.
        print(f"  codebase context: {len(codebase_context)} chars")
        enriched_task = _enrich_issue_task(args.task, codebase_context)

        leaf.write_brief(enriched_task)
        result = leaf.run_via_hermes(enriched_task)
        _get_log().student_stage(leaf.bead, role, "bookbag_written",
                                repo=str(target_path) if target_path else "")
        _get_log().student_stage(leaf.bead, role, "teachers_reviewing",
                                repo=str(target_path) if target_path else "")
        if result.get("status") != "success":
            print(f"  ❌ leaf LLM failed: {result.get('error', result.get('status'))}")
            error = result.get("error", result.get("status", "leaf failed"))
            _record_issue_dispatch_failure(
                leaf.bead,
                repo_slug,
                error,
                issue_number=issue_number,
                issue_title=issue_title,
            )
            return
        # Persist the CE route before signaling teachers. The route is now
        # visible in the bookbag before any reviewer can consume the ready
        # signal, while the student execution remains backward-compatible.
        routing = _persist_issue_route(
            leaf.bead,
            repo_slug,
            task_shape or classify_task(is_new_implementation=True),
        )
        try:
            leaf.signal_ready()
            handoff_signaled = True
        except Exception:
            # A signal write may succeed immediately before a wrapper reports
            # an error. Check the protocol file before deciding the handoff
            # failed, so reviewed work is never relabeled as dispatch failure.
            try:
                handoff_signaled = BookbagSignal(
                    leaf.bead, repo=repo_slug
                ).check()
            except Exception:
                handoff_signaled = False
            raise
        print(f"  ✅ bead={leaf.bead[:20]} ({len(result.get('response', ''))} chars) — teachers notified")
        result.update({
            "chosen_skill": routing["chosen_skill"],
            "primary_workflow": routing["primary_workflow"],
            "overlays": routing["overlays"],
            "discarded_overlays": routing["discarded_overlays"],
            "curiosity_required": routing["curiosity_required"],
            "human_gate_required": routing["human_gate_required"],
        })

        # ── Pre-merge Entire review (computational sensor layer) ───────────
        # Runs before the two-judge semantic review. Catches mechanical
        # issues (unused vars, type narrowing, complexity) that the CTO/COO
        # LLM judges don't surface. Degrades gracefully when the entire CLI
        # is missing — does NOT block the pipeline.
        entire_result = {}
        try:
            entire_result = run_entire_review(
                worktree_path=leaf.worktree_path or "",
                base_branch="main",
            )
            entire_status = entire_result.get("status", "skipped")
            entire_findings = entire_result.get("findings", [])
            if entire_status == "fail":
                print(f"  ⚠ Entire review: {len(entire_findings)} real issue(s) found")
            elif entire_status == "skipped":
                print(f"  ⊘ Entire review: skipped (entire CLI not found)")
            else:
                print(f"  ✅ Entire review: {entire_status} — no issues")
        except Exception as e:
            print(f"  ⚠ Entire review failed: {e}")
            entire_status = "error"
            entire_findings = []
        # ──────────────────────────────────────────────────────────────────────

        cto_v, coo_v = wait_for_verdicts(leaf.bead, repo=target_repo or "__global__", timeout=args.handoff_timeout)
        bag = read_bookbag(leaf.bead, target_repo or "__global__") or {}
        accepted = _persist_acceptance(leaf.bead, cto_v, coo_v, repo=target_repo or "__global__")
        _validate_verdict(leaf.bead, repo=target_repo or "__global__")
        result["review"] = {
            "cto_verdict": cto_v,
            "coo_verdict": coo_v,
            "cto_score": bag.get("cto_score", 0),
            "coo_score": bag.get("coo_score", 0),
            "findings": bag.get("findings", []),
            "accepted": accepted,
        }
        result["task_score"] = _compute_task_score(bag) if bag else 0

        accepted = result["review"].get("accepted", False)
        mark = "✅ YES" if accepted else "❌ NO"
        print("\n🔍 TWO-JUDGE REVIEW (async)")
        print(f"  CTO: {cto_v}  COO: {coo_v}  Accepted: {mark}")
        # Notify the human operator via AgentMail (best-effort; never crashes).
        try:
            from school_mail import notify_verdict
            notify_verdict(
                leaf.bead, accepted, cto_v, coo_v,
                repo=target_repo or "__global__",
                summary=(bag.get("findings", []) and f"{len(bag.get('findings', []))} findings"),
                entire_findings=entire_findings,
                entire_status=entire_status,
                cto_findings=bag.get("cto_findings", []),
                coo_findings=bag.get("coo_findings", []),
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ AgentMail notify failed: {e}")
        findings = result["review"].get("findings", [])
        print(f"  Findings: {len(findings)}")
        for f in findings[:3]:
            print(f"    - [{f.get('severity', '?')}] {f.get('description', '')[:100]}")

        updated = evaluate_and_update(result, result.get("task_score", 0), store=store)
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f389 {crossed}!" if crossed else ""
        print(f"\U0001f4ca Score: {old_s:.1f} → {new_s:.1f}{gate_msg}")
    except Exception as e:
        print(f"  ❌ async dispatch error: {e}")
        if not handoff_signaled and leaf is not None and getattr(leaf, "bead", None):
            _record_issue_dispatch_failure(
                leaf.bead,
                repo_slug,
                e,
                issue_number=issue_number,
                issue_title=issue_title,
            )
    finally:
        if leaf is not None:
            try:
                leaf.dispose()
            except Exception:
                pass
        _shutdown_teachers(teachers)


def _run_single_task(args, store):
    """Run a single task (synchronous, Phase 1)."""
    from school_core.conductor.scoring import _print_leaderboard
    from school_core.principal import load_principal_soul

    role = _resolve_agent(args)

    print(f"🎓 PRINCIPAL — dispatching {role} / {args.domain}")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")
    print(f"   Task: {args.task[:100]}")
    print()

    result = _principal_dispatch(
        task=args.task, role=role, domain=args.domain,
        difficulty=args.difficulty, store=store, repo=args.repo,
        doubt_enabled=args.doubt_enabled,
        task_shape=getattr(args, "task_shape", None),
        skip_readiness=bool(args.agent),
        repo_path=getattr(args, "repo_path", None),
        strict_route_persistence=bool(getattr(args, "issue_number", None)),
    )

    task_score = result.get("task_score", 0)
    updated = evaluate_and_update(result, task_score, store=store)

    status = result.get("status", "error")
    if status == "success":
        review = result.get("review", {})
        cto = review.get("cto_verdict", "?")
        coo = review.get("coo_verdict", "?")
        accepted = review.get("accepted", False)
        findings = review.get("findings", [])
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f393 {crossed}!" if crossed else ""

        print(f"\u2705 Student ({role}) produced {len(result.get('response', ''))} chars")
        print()
        print(f"\U0001f50d TWO-JUDGE REVIEW")
        print(f"  CTO: {cto} (score={review.get('cto_score', 0):.0f})")
        print(f"  COO: {coo} (score={review.get('coo_score', 0):.0f})")
        mark = "\u2705 YES" if accepted else "\u274c NO"
        print(f"  Accepted: {mark}")
        print(f"  Findings: {len(findings)}")
        for f in findings[:3]:
            print(f"    - [{f.get('severity', '?')}] {f.get('description', '')[:100]}")
        print(f"\U0001f4ca Score: {old_s:.1f} → {new_s:.1f}{gate_msg}")
    else:
        error = result.get("error", result.get("status", "unknown"))
        print(f"\u274c {status}: {error}")

    return result


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



    """Phase 1 synchronous loop: dispatch one at a time with inline review."""
    from school_core.conductor.scoring import _print_leaderboard, _score_and_print_round
    from school_core.principal import DOMAIN_ROLE, load_principal_soul

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
    from bookbag import read_bookbag, wait_for_verdicts
    from leaf import StudentLeaf
    from principal_doubt import run_doubt_cycle
    from school_core.conductor.daemon import _boot_teachers, _shutdown_teachers
    from school_core.conductor.scoring import _print_leaderboard
    from school_core.principal import DOMAIN_ROLE, load_principal_soul

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
    from bookbag import list_bookbags_full, read_bookbag
    from orca_executor import OrcaExecutionManager, OrcaUnavailableError
    from school_core.conductor.daemon import _boot_teachers, _shutdown_teachers
    from school_core.conductor.scoring import _print_leaderboard, _score_reviewed_bookbags

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



