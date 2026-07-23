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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scoring import ScoreStore
from director import evaluate_and_update
from bookbag import read_bookbag, list_bookbags, wait_for_verdicts
from leaf import run_leaf, StudentLeaf
from teacher import TeacherWorktree
from orca_executor import OrcaUnavailableError, OrcaExecutionManager
from github_fetcher import fetch_single_issue

# Map domain -> role for dispatch
DOMAIN_ROLE = {
    "code-search": "searcher",
    "terminal": "executor",
    "code-review": "reviewer",
    "web-automation": "browser",
    "python-coding": "coder",
    "python-testing": "tester",
    "debugging": "debugger",
    "_default": "student",
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

    # Delegate to the single-task pipeline (handles --async + two-judge review).
    args.task = issue["prompt"]
    args.domain = domain
    args.difficulty = difficulty
    _run_single_task(args, store)


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
    parser.add_argument("--resume", action="store_true",
                        help="Resume after crash: scan bookbags for partial verdicts, rediscover teachers, complete handoffs")
    args = parser.parse_args()

    # ── Standalone utilities ─────────────────────────────────────────────

    if args.list_bookbags:
        bags = list_bookbags()
        print(f"Bookbags on disk ({len(bags)}):")
        for b in bags:
            bag = read_bookbag(b)
            if bag:
                accepted = "\u2705" if bag.get("accepted") else "\u274c"
                student = bag.get("student", "?")
                domain = bag.get("domain", "?")
                print(f"  {accepted} {b:40s} {student:12s} {domain:20s}")
            else:
                print(f"  ?  {b}")
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
        store = ScoreStore()
        _run_issue(args, store)
        return

    # ── Core pipeline ────────────────────────────────────────────────────

    store = ScoreStore()

    if args.resume:
        _resume_loop(args, store)
        return

    if args.loop:
        if args.async_mode:
            _run_async_loop(args, store)
        else:
            _run_sync_loop(args, store)

    else:
        _run_single_task(args, store)


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

        result = run_leaf(
            task_prompt=task, role=role, domain=domain,
            difficulty=args.difficulty, store=store,
        )

        _score_and_print_round(result, store)
        print()

    _print_leaderboard(store)


def _run_async_loop(args, store):
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
    print("\U0001f3eb Booting teacher worktrees...")
    teachers = _boot_teachers()
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
            result = leaf.run_via_hermes(task)  # Hermes in Orca terminal

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
                cto_v, coo_v = wait_for_verdicts(bead, timeout=args.handoff_timeout)
                bag = read_bookbag(bead)
                if bag:
                    result["review"] = {
                        "cto_verdict": cto_v,
                        "coo_verdict": coo_v,
                        "cto_score": bag.get("cto_score", 0),
                        "coo_score": bag.get("coo_score", 0),
                        "findings": bag.get("findings", []),
                        "accepted": bag.get("accepted", False),
                    }
                    result["task_score"] = _compute_task_score(bag)

                completed += 1
                accepted = result["review"].get("accepted", False)
                mark = "\u2705" if accepted else "\u274c"
                print(f"  {idx+1}/{len(dispatched)} {label} "
                      f"CTO={cto_v} COO={coo_v} {mark}")

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

    result = run_leaf(
        task_prompt=args.task, role=role, domain=args.domain,
        difficulty=args.difficulty, store=store,
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
    all_beads = list_bookbags()
    if not all_beads:
        print("  No bookbags found — nothing to resume.")
        return

    partial: list[dict] = []   # one verdict filled, other empty
    reviewed: list[dict] = []  # both verdicts filled, not yet scored
    empty: list[dict] = []     # neither verdict filled

    for bead in all_beads:
        bag = read_bookbag(bead)
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


def _boot_teachers() -> dict[str, TeacherWorktree]:
    """Create persistent CTO and COO teacher worktrees.

    Creates `teacher-cto` and `teacher-coo` Orca child worktrees and
    starts the teacher review loop inside each via Orca terminals.
    Teachers poll `~/.hermes/bookbag/` for un-reviewed bookbags and
    fill verdicts asynchronously.

    Returns:
        Dict mapping role name ("cto", "coo") to TeacherWorktree instance,
        or empty dict if teachers couldn't be booted.
    """
    teachers = {}

    for role in ("cto", "coo"):
        try:
            teacher = TeacherWorktree(role)
            teacher.boot()
            teachers[role] = teacher

            # Start the review loop in the teacher's worktree terminal.
            # The teacher process will poll bookbags and fill verdicts.
            # Use a fresh OrcaExecutionManager for terminal control (the
            # teacher already booted and owns the worktree lifecycle).
            mgr = OrcaExecutionManager()
            handle = mgr.create_terminal(title=f"teacher-{role}")
            cmd = (
                f"cd {teacher.worktree_path} && "
                f"PYTHONPATH={Path(__file__).parent} "
                f"python3 -c \""
                f"from teacher import TeacherWorktree; "
                f"t = TeacherWorktree('{role}'); t.boot(); t.run_loop()"
                f"\""
            )
            mgr._run_orca([
                "terminal", "send",
                "--terminal", handle,
                "--text", cmd,
                "--enter",
            ], timeout=10)

            print(f"  \U0001f4e1 teacher-{role}: terminal started")

        except Exception as e:
            print(f"  \u274c teacher-{role}: boot failed — {e}")
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
