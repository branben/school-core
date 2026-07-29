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
"""

from __future__ import annotations

import argparse
import sys
import re
import shlex
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))

from scoring import ScoreStore
from director import evaluate_and_update
from bookbag import read_bookbag, list_bookbags, list_bookbags_full, wait_for_verdicts, locked_update_bookbag, REPO_GLOBAL
from school_mail import notify_verdict
from leaf import run_leaf, StudentLeaf
from teacher import TeacherWorktree
from principal_doubt import run_doubt_cycle
from scripts.ce_router import route_decision
from scripts.spec_gate import check_dod, _load_spec
from scripts.student_plan import generate_plan, execute_plan, is_complex, COMPLEXITY_THRESHOLD
from orca_executor import OrcaUnavailableError, OrcaExecutionManager
from github_fetcher import fetch_single_issue, load_config
from activity_log import ActivityLog

_log = ActivityLog()

# Map domain -> role for dispatch. Roles MUST exist in executor.COMBO_MAP
# (searcher, executor, reviewer, browser, coder, openhands, a2a-agent);
# "student"/"tester"/"debugger" are NOT valid dispatch roles, so unknown
# domains fall back to "coder" (the universal code role) rather than a
# nonexistent agent.
DOMAIN_ROLE = {
    "code-search": "searcher",
    "terminal": "executor",
    "code-review": "reviewer",
    "web-automation": "browser",
    "python-coding": "coder",
    "python-testing": "coder",
    "debugging": "coder",
    "code-implementation": "coder",
    "_default": "coder",
}


def load_principal_soul() -> str:
    """Load the Principal's SOUL.md (all 8 personas are now live).

    Mirrors the 3-line read pattern in teacher.py / leaf.py. Falls back to a
    minimal system prompt if the profile file is absent.
    """
    return load_soul("principal")


def load_soul(profile_name: str) -> str:
    """Resolve a persona's SOUL.md.

    Resolution order (single source of truth = repo config/profiles):
        1. ``<repo>/config/profiles/<name>/SOUL.md``  (committed, authoritative)
        2. ``~/.hermes/profiles/<name>/SOUL.md``       (machine-local override)
        3. empty string (caller supplies a generic fallback)

    Keeping the repo copy authoritative means `git clone` + run works without
    a manual copy step; HOME remains an optional local override that cannot
    silently shadow the committed persona without being intentional.
    """
    repo_soul = Path(__file__).parent / "config" / "profiles" / profile_name / "SOUL.md"
    if repo_soul.exists():
        return repo_soul.read_text().strip()
    home_soul = Path.home() / ".hermes" / "profiles" / profile_name / "SOUL.md"
    if home_soul.exists():
        return home_soul.read_text().strip()
    return ""


def _parse_issue_ref(ref: str) -> tuple[str, str, int]:
    """Parse a GitHub issue reference into (owner, repo, number).

    Accepts either ``owner/repo#123`` or a full GitHub issue URL
    (https://github.com/owner/repo/issues/123). Raises ValueError if
    the reference can't be parsed.
    """
    ref = ref.strip()
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", ref)
    if not m:
        m = re.search(r"([^/\s]+)/([^/#\s]+)#(\d+)", ref)
    if not m:
        raise ValueError(
            f"Could not parse issue ref {ref!r}. Expected 'owner/repo#123' "
            f"or a full GitHub issue URL."
        )
    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    return owner, repo, number


def _persist_acceptance(bead: str, cto_v: str, coo_v: str, repo: str = "__global__") -> bool:
    """Compute and persist the bookbag 'accepted' flag from both verdicts.

    Must match director.run_task's acceptance contract (director.py:313):
    accepted requires BOTH judges PASS at score >= 50, with NO critical
    finding (a CRITICAL finding is an automatic veto). The teacher review
    loops write their individual verdicts/scores/findings but never set
    'accepted', and evaluate_and_update (which conductor calls) does not
    re-derive it — so this flag, once persisted, is authoritative.

    Returns the computed acceptance so callers can report it, but only
    returns True if the write actually reached disk (locked_update_bookbag
    returns None on lock-acquisition failure — it does not raise).
    """
    bag = read_bookbag(bead, repo) or {}
    try:
        cto_score = float(bag.get("cto_score", 0) or 0)
        coo_score = float(bag.get("coo_score", 0) or 0)
    except (TypeError, ValueError):
        cto_score = coo_score = 0.0
    findings = (bag.get("cto_findings", []) or []) + (bag.get("coo_findings", []) or [])
    has_critical = any(
        str(f.get("severity", "")).upper() == "CRITICAL" for f in findings
    )
    accepted = (
        cto_v == "PASS"
        and coo_v == "PASS"
        and cto_score >= 50
        and coo_score >= 50
        and not has_critical
    )
    written = locked_update_bookbag(bead, repo, lock_timeout=10.0, accepted=accepted)
    if written is None:
        print(f"[principal] WARNING: accepted flag for {bead} NOT persisted "
              f"(lock timeout) — disk may show stale accepted=False")
        return False
    return accepted


def _run_issue(args, store):
    """Fetch a single GitHub issue and dispatch it through the Principal pipeline."""
    try:
        owner, repo, number = _parse_issue_ref(args.issue)
    except ValueError as e:
        print(f"\u274c {e}")
        return

    print(f"\U0001f4e6 PRINCIPAL \u2014 fetching issue {owner}/{repo}#{number}")
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
    role = args.agent or DOMAIN_ROLE.get(domain, "student")
    print(f"   Title : {issue['title']}")
    print(f"   Domain: {domain}  Difficulty: {difficulty}  Role: {role}")
    if issue.get("state") != "ready-for-agent":
        print(f"   \u26a0 Classified state={issue.get('state')!r} (not 'ready-for-agent') \u2014 dispatching anyway")

    # Delegate to the pipeline. --async boots persistent teacher worktrees
    # (CTO+COO) and polls bookbags; otherwise run sync inline two-judge review.
    args.task = issue["prompt"]
    args.domain = domain
    args.difficulty = difficulty
    if args.async_mode:
        _run_issue_async(args, store, role, target_repo=f"{owner}/{repo}")
    else:
        _run_single_task(args, store)


def _run_issue_async(args, store, role, target_repo: Optional[str] = None):
    """Single-issue async path: boot teachers, run one leaf, poll verdicts.

    Mirrors _run_async_loop's topology for one issue — the CTO/COO teacher
    worktrees are persistent (visible in Orca's sidebar) and review the
    student's bookbag via the signal protocol, instead of the principal
    running both reviews inline.
    """
    print(f"\U0001f504 PRINCIPAL — async dispatch {role} / {args.domain}")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")

    # Scope the score store to the target repo so a role's learned capacity
    # (EMA score across tasks) stays per-repo and never leaks across repos.
    repo_slug = target_repo or "__global__"
    store = ScoreStore(repo=repo_slug)

    teachers = _boot_teachers()
    if len(teachers) < 2:
        print("  \u26a0\ufe0f Could not boot both teachers — falling back to sync review")
        if teachers:
            _shutdown_teachers(teachers)
        _run_single_task(args, store)
        return

    cto = teachers.get("cto")
    coo = teachers.get("coo")
    print(f"  \u2705 CTO worktree: {cto.worktree_name}")
    print(f"  \u2705 COO worktree: {coo.worktree_name}\n")

    leaf = None
    target_path = None
    if target_repo:
        # Cross-repo dispatch: clone a FRESH copy of the target repo so the
        # student never starts from a contaminated/stale base tree.
        try:
            from repo_reader import clone_repo
            target_path = clone_repo(target_repo, force_fresh=True)
        except Exception as e:
            print(f"  ⚠ Could not clone target repo {target_repo}: {e} — falling back to school-core")
            target_path = None
    try:
        leaf = StudentLeaf(role=role, domain=args.domain,
                           difficulty=args.difficulty, store=store,
                           repo_path=target_path)
        leaf.boot()
        leaf.write_brief(args.task)
        result = leaf.run_via_hermes(args.task)
        _log.student_stage(leaf.bead, role, "bookbag_written",
                            repo=str(target_path) if target_path else "")
        _log.student_stage(leaf.bead, role, "teachers_reviewing",
                            repo=str(target_path) if target_path else "")
        if result.get("status") != "success":
            print(f"  \u274c leaf LLM failed: {result.get('error', result.get('status'))}")
            leaf.dispose()
            _shutdown_teachers(teachers)
            return
        leaf.signal_ready()
        print(f"  \u2705 bead={leaf.bead[:20]} ({len(result.get('response', ''))} chars) — teachers notified\n")

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
        mark = "\u2705 YES" if accepted else "\u274c NO"
        print("\U0001f50d TWO-JUDGE REVIEW (async)")
        print(f"  CTO: {cto_v}  COO: {coo_v}  Accepted: {mark}")
        # Notify the human operator via AgentMail (best-effort; never crashes).
        try:
            notify_verdict(
                leaf.bead, accepted, cto_v, coo_v,
                repo=target_repo or "__global__",
                summary=result["review"].get("findings", []) and f"{len(result['review']['findings'])} findings",
            )
        except Exception as e:  # noqa: BLE001
            print(f"  \u26a0 AgentMail notify failed: {e}")
        findings = result["review"].get("findings", [])
        print(f"  Findings: {len(findings)}")
        for f in findings[:3]:
            print(f"    - [{f.get('severity', '?')}] {f.get('description', '')[:100]}")

        updated = evaluate_and_update(result, result.get("task_score", 0), store=store)
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f389 {crossed}!" if crossed else ""
        print(f"\U0001f4ca Score: {old_s:.1f} \u2192 {new_s:.1f}{gate_msg}")
    except Exception as e:
        print(f"  \u274c async dispatch error: {e}")
    finally:
        if leaf is not None:
            try:
                leaf.dispose()
            except Exception:
                pass
        _shutdown_teachers(teachers)


def main():
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
    parser.add_argument("--handoff-timeout", type=int, default=120,
                        help="Seconds to wait for teacher verdicts in async mode")
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

    args = parser.parse_args()

    # ── Serve mode: native Orca spawn (Gap C/D) ────────────────────────
    # The principal is an Orca *automation* (--provider hermes) — Orca owns
    # the schedule, no while-True pane. Teachers are persistent worktrees
    # (rediscover-or-create, so re-serve never mints cto-2/cto-3). The
    # bookbag file stays the durable verdict record; the loop polls it
    # inside the Hermes agent prompt, not as a Python process in a pane.
    if args.stop_serve:
        _teardown_serve()
        return

    if args.serve:
        _launch_serve()
        return

    # ── Standalone utilities ─────────────────────────────────────────────

    if args.list_bookbags:
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
        try:
            mgr = OrcaExecutionManager()
            count = mgr.cleanup_worktrees_by_prefix("study-")
            print(f"\U0001f9f9 Removed {count} study-* worktrees")
        except OrcaUnavailableError as e:
            print(f"\u26a0 Orca not available: {e}")
        return

    if args.issue:
        # Namespace the score store by the issue's repo so a role's learned
        # capacity stays per-repo (matches the multi-repo serve topology).
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

    # Namespace the score store by --repo (if given) so standalone --task
    # dispatch keeps a role's learning per-repo instead of __global__.
    store = ScoreStore(repo=args.repo or "__global__")

    if args.resume:
        _resume_loop(args, store)
        return

    if args.loop:
        if args.async_mode:
            _run_async_loop(args, store, repo=args.repo or "__global__")
        else:
            _run_sync_loop(args, store)

    else:
        _run_single_task(args, store)


def _principal_dispatch(
    task: str,
    role: str,
    domain: str,
    difficulty: str,
    store,
    repo: str,
    doubt_enabled: bool = False,
    doubt_fn=None,
    override_reason: Optional[str] = None,
    task_shape: Optional[dict] = None,
) -> dict:
    """Rank 3 + Rank 4 — Principal dispatch with DDD doubt cycle and CE router.

    Runs the DDD doubt cycle on the routing decision (gate/role/model) BEFORE
    committing to dispatch. If doubt is enabled and finds an issue, the chosen
    gate is down-shifted (reconcile) and the dispatch uses the reconciled gate.
    The ``doubt_log`` is attached to the returned result dict for traceability.

    Rank 4: the CE router maps the task shape to a Layer B skill (rank) and
    logs ``chosen_skill`` to the bookbag for traceability. The router is
    deterministic and offline — it never blocks dispatch.

    When ``doubt_enabled`` is False (default) the doubt cycle is skipped (no
    ``doubt_log`` key). The router always runs (it is cheap) unless a
    ``task_shape`` is provided that maps to a skill, in which case its choice
    is recorded regardless.
    """
    gate = difficulty
    if doubt_enabled:
        claim = (
            f"Routing task to {role} ({domain}) via gate {gate} "
            f"through OmniRoute / Orca student leaf."
        )
        extract = {
            "task": task,
            "role": role,
            "domain": domain,
            "gate": gate,
            "model": "omni-route/default",
            "lens": "principal",
        }
        doubt_log = run_doubt_cycle(
            claim=claim,
            extract=extract,
            doubt_fn=doubt_fn,
            max_cycles=1,
            override_reason=override_reason,
        )
        # RECONCILE: if doubt down-shifted the gate, dispatch with the softer gate.
        reconciled_gate = doubt_log["extract"].get("gate", gate)
    else:
        doubt_log = None
        reconciled_gate = gate

    # Rank 4: choose the Layer B skill from the task shape. Default a fresh
    # student task to "new implementation" when the caller doesn't supply one.
    if task_shape is None:
        task_shape = {
            "has_failed_gate": False,
            "is_new_implementation": True,
            "requires_architectural_routing": False,
            "complexity": 1,
            "is_spec_gap": False,
        }
    routing = route_decision(task_shape, bead=None, repo=repo)

    result = run_leaf(
        task_prompt=task, role=role, domain=domain,
        difficulty=reconciled_gate, store=store, repo=repo,
        ce_enabled=True,
        complex_task=(routing["chosen_skill"] == "rank5_student_plan"),
    )

    result["chosen_skill"] = routing["chosen_skill"]
    if doubt_log is not None:
        result["doubt_log"] = doubt_log
    # Log the routing choice to the bookbag for traceability (best-effort;
    # never breaks dispatch). Need the bead from the dispatch result.
    bead = result.get("bead")
    if bead:
        try:
            locked_update_bookbag(
                bead, repo,
                chosen_skill=routing["chosen_skill"],
                chosen_skill_label=routing["label"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ce_router: bookbag log skipped: %s", e)
    return result


def _run_sync_loop(args, store):
    """Phase 1 synchronous loop: dispatch one at a time with inline review."""
    print(f"🔄 Principal loop mode — {args.rounds} rounds (synchronous)\n")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")

    domain_tasks = _default_tasks()

    for i in range(args.rounds):
        domain, task = domain_tasks[i % len(domain_tasks)]
        role = DOMAIN_ROLE.get(domain, "student")
        round_num = i + 1

        print(f"--- Round {round_num}/{args.rounds} [{role} / {domain}] ---")

        result = _principal_dispatch(
            task=task, role=role, domain=domain,
            difficulty=args.difficulty, store=store, repo=args.repo,
            doubt_enabled=args.doubt_enabled,
        )

        _score_and_print_round(result, store)
        print()

    _print_leaderboard(store)


def _run_async_loop(args, store, repo: str = REPO_GLOBAL):
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
    print(f"🔄 Principal async loop — {args.rounds} rounds (concurrent dispatch)\n")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")

    # ── Step 1: Boot teachers ─────────────────────────────────────────
    print("🏫 Booting teacher worktrees...")
    teachers = _boot_teachers(repo)
    if len(teachers) < 2:
        print("  \u26a0\ufe0f Could not boot both teachers (need CTO+COO) — falling back to sync mode")
        if teachers:
            _shutdown_teachers(teachers)
        _run_sync_loop(args, store)
        return

    cto = teachers.get("cto")
    coo = teachers.get("coo")
    print(f"  \u2705 CTO worktree: {cto.worktree_name}")
    print(f"  \u2705 COO worktree: {coo.worktree_name}")
    print()

    # ── Step 2a: Create all leaf worktrees (fast, no LLM) ─────────────
    domain_tasks = _default_tasks()
    leaves: list[tuple[StudentLeaf, str, str, str]] = []

    print(f"\U0001f333 Creating {args.rounds} leaf worktrees...")
    for i in range(args.rounds):
        domain, task = domain_tasks[i % len(domain_tasks)]
        role = DOMAIN_ROLE.get(domain, "student")

        leaf = None
        try:
            leaf = StudentLeaf(role=role, domain=domain, difficulty=args.difficulty, store=store)
            leaf.boot()
            leaf.write_brief(task)
            leaves.append((leaf, task, role, domain))
        except Exception as e:
            print(f"  \u274c Leaf {i+1}/{args.rounds} [{role}] boot failed: {e}")
            if leaf is not None:
                try:
                    leaf.dispose()
                except Exception:
                    pass

    print(f"  \u2705 {len(leaves)}/{args.rounds} worktrees created")
    print()

    if not leaves:
        print("  No leaves could be created — aborting.")
        _shutdown_teachers(teachers)
        return

    # ── Step 2b: Run LLM calls + signal teachers ───────────────────────
    dispatched: list[tuple[StudentLeaf, dict]] = []
    print(f"\U0001f4ac Running {len(leaves)} LLM calls (teachers review as bookbags arrive)...")
    print()

    for idx, (leaf, task, role, domain) in enumerate(leaves):
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
                dispatched.append((leaf, result))
                print(f"    \u2705 bead={leaf.bead[:20]} ({len(result.get('response', ''))} chars) — teachers notified")
            else:
                error = result.get("error", result.get("status", "unknown"))
                print(f"    \u274c {result.get('status')}: {error}")
                leaf.dispose()
        except Exception as e:
            print(f"    \u274c Hermes call failed: {e}")
            try:
                leaf.dispose()
            except Exception:
                pass

    print()
    print(f"\U0001f4e8 Dispatched {len(dispatched)}/{len(leaves)} tasks to teachers")
    print()

    # ── Step 3: Poll for teacher verdicts ──────────────────────────────
    if dispatched:
        print(f"\U000023f3 Polling for teacher verdicts (timeout={args.handoff_timeout}s each)...")
        print()

        completed = 0
        for idx, (leaf, result) in enumerate(dispatched):
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
                    notify_verdict(
                        bead, accepted, cto_v, coo_v,
                        repo=repo,
                        summary=(findings or []) and f"{len(findings)} findings",
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠ AgentMail notify failed: {e}")

            except Exception as e:
                print(f"  {idx+1}/{len(dispatched)} {label} \u274c Timeout: {e}")

            # Score and dispose
            task_score = result.get("task_score", 0)
            evaluate_and_update(result, task_score, store=store)
            leaf.dispose()

        print()
        print(f"\u2705 Completed: {completed}/{len(dispatched)} verdicts received")

    # ── Step 4: Shutdown teachers ──────────────────────────────────────
    print()
    _shutdown_teachers(teachers)

    _print_leaderboard(store)


def _run_single_task(args, store):
    """Run a single task (synchronous, Phase 1)."""
    role = args.agent or DOMAIN_ROLE.get(args.domain, "student")

    print(f"🎓 PRINCIPAL — dispatching {role} / {args.domain}")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")
    print(f"   Task: {args.task[:100]}")
    print()

    result = _principal_dispatch(
        task=args.task, role=role, domain=args.domain,
        difficulty=args.difficulty, store=store, repo=args.repo,
        doubt_enabled=args.doubt_enabled,
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
        print(f"\U0001f4ca Score: {old_s:.1f} \u2192 {new_s:.1f}{gate_msg}")
    else:
        error = result.get("error", result.get("status", "unknown"))
        print(f"\u274c {status}: {error}")


# ── Helpers ──────────────────────────────────────────────────────────────────


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
        print("  \u26a0\ufe0f Cannot resume — need both CTO+COO teachers")
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
                print(f"  {idx+1}/{len(pending)} {bead[:30]} [{student}/{domain}] \u274c Timeout")
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


def _score_reviewed_bookbags(bookbags: list[dict], store: ScoreStore) -> None:
    """Score all reviewed bookbags via evaluate_and_update."""
    print(f"\U0001f4ca Scoring {len(bookbags)} reviewed bookbags...")

    scored = 0
    for bag in bookbags:
        bead = bag.get("bead", "unknown")
        student = bag.get("student", "unknown")
        domain = bag.get("domain", "unknown")
        cto_v = bag.get("cto_verdict", "?")
        coo_v = bag.get("coo_verdict", "?")
        task_score = _compute_task_score(bag)

        result = {
            "status": "success",
            "agent": student,
            "domain": domain,
            "review": {
                "cto_verdict": cto_v,
                "coo_verdict": coo_v,
                "cto_score": bag.get("cto_score", 0),
                "coo_score": bag.get("coo_score", 0),
                "findings": bag.get("findings", []),
                "accepted": bag.get("accepted", False),
            },
            "task_score": task_score,
        }

        updated = evaluate_and_update(result, task_score, store=store)
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f393 {crossed}!" if crossed else ""
        scored += 1
        print(f"  {scored}/{len(bookbags)} {bead[:30]} [{student}/{domain}] "
              f"{old_s:.1f}\u2192{new_s:.1f}{gate_msg}")

    print(f"  \u2705 Scored: {scored}")
    print()


def _cleanup_orphaned_leaves() -> None:
    """Remove orphaned study-* leaf worktrees left by a crash."""
    try:
        mgr = OrcaExecutionManager()
        count = mgr.cleanup_worktrees_by_prefix("study-")
        if count > 0:
            print(f"\U0001f9f9 Cleaned up {count} orphaned leaf worktrees")
    except OrcaUnavailableError:
        print("\u26a0\ufe0f Orca not available — skipping leaf cleanup")
    except Exception as e:
        print(f"\u26a0\ufe0f Leaf cleanup error: {e}")


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


def _default_tasks() -> list[tuple[str, str]]:
    """Default task rotation for loop mode."""
    return [
        ("code-search", "What single grep command finds all Python files with TODO comments recursively? One command only."),
        ("terminal", "What does this command do: `find . -name '*.py' -mtime -1 | xargs wc -l`? One sentence."),
        ("code-review", "Is `except Exception: pass` good practice? One word answer + one sentence why."),
        ("web-automation", "What CSS selector targets all <button> elements with class 'primary' inside a <form>? One selector."),
        ("python-coding", "Write def chunks(lst, n): yield successive n-sized chunks from lst using yield. Just code, no explanation."),
    ]


def _find_or_create_terminal(mgr: "OrcaExecutionManager", title: str) -> str:
    """Return a terminal handle for `title`, reusing an existing one if present.

    Orca's ``create_terminal`` never dedupes by title, so repeated
    dispatches accumulated duplicate ``teacher-cto`` / ``teacher-coo``
    terminals. This scans the existing terminal list for a matching
    title and reuses it; otherwise it creates a fresh terminal.
    """
    try:
        result = mgr._run_orca(["terminal", "list"], timeout=15)
    except Exception:
        return mgr.create_terminal(title=title)
    terminals = result.get("terminals", result.get("result", {}).get("terminals", []))
    if isinstance(result.get("result"), dict):
        terminals = result["result"].get("terminals", terminals)
    for term in terminals:
        t_title = term.get("title") or term.get("name") or ""
        handle = term.get("handle") or term.get("id") or ""
        if t_title == title and handle:
            return handle
    return mgr.create_terminal(title=title)


def _principal_prompt(repo: str = "__global__") -> str:
    """Build the per-repo Principal automation prompt.

    In multi-repo mode the prompt scopes the principal to its repo's
    bookbag namespace so teachers + students for OTHER repos never get
    cross-repo verdicts.
    """
    bag_ns = (
        "~/.hermes/bookbag/"
        if repo == "__global__"
        else f"~/.hermes/bookbag/{repo.replace('/', '__')}/"
    )
    scope = "" if repo == "__global__" else f"Repo scope: {repo}. "
    return (
        "You are the Agent-School Principal (Hermes, -p principal). "
        f"{scope}"
        "Each tick: read `bd ready` for open beads; classify + EFC-route "
        "each to a student leaf; wait for both CTO and COO verdicts in "
        f"{bag_ns}<bead>.json; apply the acceptance rule "
        "(both PASS AND score>=50 AND no critical -> accepted); notify the "
        "human via AgentMail. On /fix from the human, re-dispatch a fresh "
        "student (never edit yourself). Do not watch terminals or read logs."
    )


def _launch_serve() -> None:
    """Boot the school via native Orca primitives (Gap C/D).

    Multi-repo aware (config.github.yaml `target_repos`):

    - Single-repo (target_repos empty) → ONE principal automation
      (``agent-school-principal``) + a CTO/COO teacher pair scoped to
      ``__global__``. This is exactly the legacy serve behavior.
    - Multi-repo (target_repos populated) → one principal automation PER
      repo (``agent-school-principal-<slug>``) + a CTO/COO teacher pair
      scoped to that repo's bookbag namespace. Teachers poll only their
      own repo; verdicts never collide across repos.

    - Principal  → ``orca automations create --provider hermes`` (Orca owns
      the schedule; no while-True pane in a terminal).
    - Teachers   → persistent worktrees (rediscover-or-create, so re-serve
      never mints cto-2/cto-3). The review loop still runs inside the
      teacher's own terminal (the leaf/teacher Hermes agent), which is the
      Orca-native path once ``--agent hermes`` boot lands.

    Idempotent: re-running --serve reuses the existing automations + worktrees.
    """
    mgr = OrcaExecutionManager()
    repo_root = Path(__file__).parent
    cfg = load_config()
    target_repos: list[dict] = cfg.get("target_repos") or []

    def _boot_principal(repo: str, slug: str) -> None:
        # Orca automation names cannot contain '/'; slugify owner/repo slugs.
        safe = slug.replace("/", "__")
        name = "agent-school-principal" if repo == "__global__" else f"agent-school-principal-{safe}"
        existing = [
            a for a in (orca_automations_list() or [])
            if a.get("name") == name
        ]
        if existing:
            print(f"  \U0001f4e1 principal automation already running (id={existing[0]['id']})")
            return
        res = mgr._run_orca([
            "automations", "create",
            "--name", name,
            "--trigger", "hourly",
            "--prompt", _principal_prompt(repo),
            "--provider", "hermes",
            "--workspace", f"path:{repo_root}",
            "--json",
        ], timeout=30)
        aid = (res.get("result", {}).get("automation", {}).get("id")
               or res.get("id") or "??")
        print(f"  \U0001f4e1 principal automation created (id={aid})")

    if not target_repos:
        # ── Single-repo mode (legacy) ──
        _boot_principal("__global__", "global")
        teachers = _boot_teachers("__global__")
        for role in teachers:
            print(f"  \U0001f9e9 teacher-{role}: persistent worktree up")
    else:
        # ── Multi-repo mode ──
        for entry in target_repos:
            slug = entry.get("slug") or entry.get("repo")
            if not slug:
                print(f"  \u26a0 skipping target_repos entry with no slug: {entry!r}")
                continue
            _boot_principal(slug, slug)
            teachers = _boot_teachers(slug)
            for role in teachers:
                print(f"  \U0001f9e9 teacher-{role}-{slug}: persistent worktree up")

    print("\n\U0001f3d7 School is serving (native Orca). "
          "Stop with: python3 conductor.py --stop-serve")


def _teardown_serve() -> None:
    """Tear down the --serve school (Gap C/D). Multi-repo aware."""
    mgr = OrcaExecutionManager()
    cfg = load_config()
    target_repos: list[dict] = cfg.get("target_repos") or []

    # Remove principal automation(s). Single-repo → one; multi-repo → one per repo.
    principal_names = {"agent-school-principal"}
    for entry in target_repos:
        slug = entry.get("slug") or entry.get("repo")
        if slug:
            principal_names.add(f"agent-school-principal-{slug.replace('/', '__')}")
    for a in (orca_automations_list() or []):
        if a.get("name") in principal_names:
            mgr._run_orca(["automations", "remove", "--id", a["id"]], timeout=15)
            print(f"  \U0001f5d1 removed principal automation {a['id']}")

    # Remove the teacher automations (Gap D) + close the persistent worktrees.
    repos_for_teardown = ["__global__"] if not target_repos else [
        (entry.get("slug") or entry.get("repo")) for entry in target_repos
    ]
    for repo in repos_for_teardown:
        if not repo:
            continue
        suffix = "" if repo == "__global__" else f"-{repo.replace('/', '__')}"
        for role in ("cto", "coo"):
            orca_automations_remove(f"agent-school-teacher-{role}{suffix}")
            try:
                t = TeacherWorktree(role, repo=repo)
                t.boot()  # rediscover-or-create (safe: reuses existing)
                t.close()
                print(f"  \U0001f9e9 teacher-{role}{suffix}: worktree closed")
            except Exception as e:
                print(f"  ⚠️ teacher-{role}{suffix}: close error — {e}")


def orca_automations_list() -> list[dict]:
    """List Orca automations as a list of dicts (best-effort).

    Handles the live response shape: {"ok":true,"result":{"automations":[…]}}.
    """
    try:
        mgr = OrcaExecutionManager()
        res = mgr._run_orca(["automations", "list", "--json"], timeout=15)
    except Exception:
        return []
    if isinstance(res, list):
        return res
    r = res.get("result", res)
    if isinstance(r, dict):
        return r.get("automations", r.get("items", []))
    if isinstance(r, list):
        return r
    return []


def orca_automations_create(
    *,
    name: str,
    prompt: str,
    trigger: str = "hourly",
    workspace: Optional[str] = None,
) -> Optional[str]:
    """Create (or reuse) a scheduling automation via Orca.

    Mirrors the principal migration: Orca owns the schedule, so the teacher
    review loop no longer lives in a while-True pane + per-boot terminal
    spray (run_teacher_loop.py). Returns the automation id, or ``None`` if
    creation failed.
    """
    mgr = OrcaExecutionManager()
    existing = [a for a in (orca_automations_list() or []) if a.get("name") == name]
    if existing:
        return existing[0].get("id")
    args = [
        "automations", "create",
        "--name", name,
        "--trigger", trigger,
        "--prompt", prompt,
        "--provider", "hermes",
        "--json",
    ]
    if workspace:
        args += ["--workspace", workspace]
    try:
        res = mgr._run_orca(args, timeout=30)
    except Exception as e:
        print(f"  ⚠️ automation create '{name}' failed — {e}")
        return None
    r = res.get("result", res)
    if isinstance(r, dict):
        return (r.get("automation") or r).get("id") or r.get("id")
    return None


def orca_automations_remove(name: str) -> None:
    """Remove all automations matching ``name`` (best-effort)."""
    mgr = OrcaExecutionManager()
    for a in (orca_automations_list() or []):
        if a.get("name") == name:
            try:
                mgr._run_orca(["automations", "remove", "--id", a["id"]], timeout=15)
                print(f"  🗑 removed automation {name} ({a['id']})")
            except Exception as e:
                print(f"  ⚠️ automation remove '{name}' failed — {e}")


def _validate_verdict(bead: str, repo: str = "__global__") -> None:
    """Enforce the Gap-B verdict-record contract at the principal reconcile point.

    The bookbag after the refactor holds ONLY the two-judge output. This is a
    guard, not a task blocker: a malformed record is logged (so the human sees
    it on the dashboard) but the dispatch still completes.

    Rank 1: when a teacher ran the diagnose loop (--diagnose), surface the
    recorded `{role}_diagnosis` in the bookbag so the principal note is
    traceable and visible on the dashboard.
    """
    try:
        from bookbag import validate_verdict_record, read_bookbag
    except Exception:
        return
    bag = read_bookbag(bead, repo)
    if bag:
        cto_dx = bag.get("cto_diagnosis")
        coo_dx = bag.get("coo_diagnosis")
        if cto_dx:
            print(f"  🔬 CTO diagnose: {cto_dx.get('root_cause', '')[:80]}")
        if coo_dx:
            print(f"  🔬 COO diagnose: {coo_dx.get('root_cause', '')[:80]}")
    ok, issues = validate_verdict_record(bead, repo)
    if not ok:
        print(f"  ⚠️ verdict-record contract violation for {bead}: "
              f"{'; '.join(issues)}")


def _boot_teachers(repo: str = REPO_GLOBAL) -> dict[str, TeacherWorktree]:
    """Create persistent CTO and COO teacher worktrees.

    Creates `teacher-cto` and `teacher-coo` Orca child worktrees (scoped to
    the target repo when multi-repo dispatch is enabled) and starts the
    teacher review loop inside each via Orca terminals. Teachers poll
    `~/.hermes/bookbag/<repo>/` for un-reviewed bookbags and fill verdicts
    asynchronously.

    Returns:
        Dict mapping role name ("cto", "coo") to TeacherWorktree instance,
        or empty dict if teachers couldn't be booted.
    """
    teachers = {}

    for role in ("cto", "coo"):
        try:
            teacher = TeacherWorktree(role, repo=repo)
            teacher.boot()  # rediscover-or-create the persistent worktree
            teachers[role] = teacher

            # Gap D: launch the teacher as a NATIVE Orca automation (Orca owns
            # the schedule). No while-True pane, no per-boot terminal spray
            # (the old run_teacher_loop.py anti-pattern that minted a
            # teacher-*-review terminal every boot). The automation targets
            # the teacher's persistent worktree and runs run_teacher_review_once
            # on a trigger; review_cycle() inside does the one-pass judge.
            # Orca automation names cannot contain '/'; slugify owner/repo slugs.
            safe = repo.replace("/", "__")
            name = f"agent-school-teacher-{role}" if repo == "__global__" else f"agent-school-teacher-{role}-{safe}"
            prompt = (
                f"Run the {role.upper()} review pass for Agent-School. Each tick: "
                f"execute `python3 scripts/run_teacher_review_once.py {role} {repo} --diagnose` from "
                f"the repo root. That runs exactly one pass over un-reviewed "
                f"bookbags for your lens (CTO = CORRECTNESS+SECURITY, "
                f"COO = COMPLETENESS), writes your verdict into "
                f"~/.hermes/bookbag/<bead>.json, then exits. Do not edit code or "
                f"open terminals — Orca schedules you; only run the script.\\n\\n"
                f"When a gate verdict is FAIL, the --diagnose flag makes the teacher "
                f"run the systematic-debugging + TDD loop: it reproduces the failure "
                f"as a regression test under diagnoses/{role}/<bead>.py and records a "
                f"`{role}_diagnosis` dict (root_cause, regression_test, fix_applied) "
                f"in the bookbag. Do not skip the diagnose step on FAIL."
            )
            aid = orca_automations_create(
                name=name,
                prompt=prompt,
                trigger="*/5 * * * *",  # every 5 min (Orca cron; "every 5m" is rejected)
                workspace=f"path:{teacher.worktree_path}",
            )
            if aid:
                print(f"  🧑‍🏫 teacher-{role}: automation up (id={aid})")
            else:
                print(f"  ⚠️ teacher-{role}: automation not created — review won't run")

        except Exception as e:
            print(f"  ❌ teacher-{role}: boot failed — {e}")
            # Clean up any partial boot
            if role in teachers:
                try:
                    teachers[role].close()
                except Exception:
                    pass
                del teachers[role]

    return teachers


def _shutdown_teachers(teachers: dict[str, TeacherWorktree]) -> None:
    """Shut down teacher worktrees.

    Sends SIGINT to terminal sessions, then closes the worktrees.
    """
    print("\U0001f9f9 Shutting down teachers...")
    for role, teacher in teachers.items():
        try:
            teacher.close()
            print(f"  \u2705 teacher-{role}: closed")
        except Exception as e:
            print(f"  \u26a0\ufe0f teacher-{role}: close error — {e}")
    print()


def _score_and_print_round(result: dict, store: ScoreStore) -> None:
    """Score a single round's result and print it (used by sync loop)."""
    task_score = result.get("task_score", 0)
    updated = evaluate_and_update(result, task_score, store=store)

    status = result.get("status", "error")
    if status == "success":
        review = result.get("review", {})
        cto = review.get("cto_verdict", "?")
        coo = review.get("coo_verdict", "?")
        accepted = review.get("accepted", False)
        findings = len(review.get("findings", []))
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f393 {crossed}!" if crossed else ""
        print(f"  \u2705 CTO={cto} COO={coo} | {'Accepted' if accepted else 'Rejected'} | "
              f"Score: {old_s:.1f}\u2192{new_s:.1f}{gate_msg} | {findings} findings")
    else:
        error = result.get("error", result.get("status", "unknown"))
        print(f"  \u274c {status}: {error}")


def _print_leaderboard(store: ScoreStore) -> None:
    """Print the final leaderboard."""
    print("=" * 60)
    print("\U0001f4ca Final Leaderboard:")
    for rank, (agent, score) in enumerate(store.leaderboard("_default"), 1):
        gate = store.gate_for_score(score)
        print(f"  {rank}. {agent:12s} {score:6.1f} ({gate})")
    print()
    print(f"\U0001f4a1 To clean up: python conductor.py --clean-worktrees")


if __name__ == "__main__":
    main()
