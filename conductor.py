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
import json
import subprocess
import sys
import re
import shlex
import time
import logging
from datetime import datetime, timezone
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
from src.qodo_pre_merge import run_qodo_improve
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


# Model aliases that map to roles in COMBO_MAP. When --agent passes a model
# name (e.g. "foundry-coder-7b"), resolve to the role (e.g. "coder") so the
# score-store gate check and role dispatch work correctly. Without this, the
# conductor treats the model name as a role and rejects it as "Unknown role".
# Populated lazily from executor.COMBO_MAP to avoid import cycles at module
# load time (executor imports conductor for callback hooks).
AGENT_TO_ROLE_CACHE: Optional[dict] = None


def _agent_to_role(agent: str) -> Optional[str]:
    """Resolve a --agent value to a valid role name.

    Accepts both role names (coder, searcher, etc.) and model aliases
    (foundry-coder-7b, north-coding, auto/best-free). Returns None if the
    agent is not recognized.
    """
    global AGENT_TO_ROLE_CACHE
    if AGENT_TO_ROLE_CACHE is None:
        AGENT_TO_ROLE_CACHE = {}
        try:
            from executor import COMBO_MAP
            for role, entry in COMBO_MAP.items():
                # COMBO_MAP values are dicts with "default" model or lists
                if isinstance(entry, dict):
                    model = entry.get("default") or entry.get("_default")
                    if model:
                        AGENT_TO_ROLE_CACHE[model] = role
                elif isinstance(entry, str):
                    AGENT_TO_ROLE_CACHE[entry] = role
                elif isinstance(entry, list):
                    for m in entry:
                        if isinstance(m, str):
                            AGENT_TO_ROLE_CACHE[m] = role
        except Exception:
            pass
    return AGENT_TO_ROLE_CACHE.get(agent)


def _resolve_agent(args) -> str:
    """Resolve args.agent to a dispatchable role.

    Priority: explicit role match → model alias → domain default.
    """
    if args.agent:
        # Direct role match (coder, searcher, executor, reviewer, browser)
        try:
            from executor import COMBO_MAP
            if args.agent in COMBO_MAP:
                return args.agent
        except Exception:
            pass
        # Model alias resolution (foundry-coder-7b → coder)
        resolved = _agent_to_role(args.agent)
        if resolved is not None:
            return resolved
        # If it looks like a model name but isn't in COMBO_MAP, treat as
        # the domain's default role and let the executor route the model.
        logger.warning(
            "Agent %r not found in COMBO_MAP — treating as model name "
            "for the %s role", args.agent, DOMAIN_ROLE.get(args.domain, "coder")
        )
        return DOMAIN_ROLE.get(args.domain, "coder")
    return DOMAIN_ROLE.get(args.domain, "student")


# Persistence file for the daemon-mode serve state. Stores:
#   - principal_terminal_handle: handle of the principal Python-loop terminal
#   - teacher_both_terminal_handle: handle of the CTO+COO daemon terminal
#   - created_at: ISO timestamp when these terminals were first launched
#
# --serve writes this file once when launching and --stop-serve reads it
# again to close the terminals. A re-run of --serve reuses the saved handles
# (so no fresh terminals ever accumulate when re-serving).
SERVE_STATE_PATH = Path.home() / ".school-core" / "serve-state.json"


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
    role = _resolve_agent(args)
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

    teachers = _boot_teachers(repo=repo_slug)
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
        print(f"  ✅ bead={leaf.bead[:20]} ({len(result.get('response', ''))} chars) — teachers notified")

        # ── Pre-merge Qodo check (computational sensor layer) ──────────────
        # Runs before the two-judge semantic review. Catches mechanical
        # issues (unused vars, type narrowing, complexity) that the CTO/COO
        # LLM judges don't surface. Degrades gracefully if QODO_API_KEY
        # is missing — does NOT block the pipeline.
        qodo_result = {}
        try:
            qodo_result = run_qodo_improve(
                worktree_path=leaf.worktree_path or "",
                base_branch="main",
            )
            qodo_status = qodo_result.get("status", "skipped")
            qodo_findings = qodo_result.get("findings", [])
            if qodo_status == "fail":
                print(f"  ⚠ Entire review: {len(qodo_findings)} real issue(s) found")
            elif qodo_status == "skipped":
                print(f"  ⊘ Entire review: skipped (entire CLI not found)")
            else:
                print(f"  ✅ Entire review: {qodo_status} — no issues")
        except Exception as e:
            print(f"  ⚠ Entire review failed: {e}")
            qodo_status = "error"
            qodo_findings = []
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
            notify_verdict(
                leaf.bead, accepted, cto_v, coo_v,
                repo=target_repo or "__global__",
                summary=(bag.get("findings", []) and f"{len(bag.get('findings', []))} findings"),
                qodo_findings=qodo_findings,
                qodo_status=qodo_status,
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
    parser.add_argument("--handoff-timeout", type=int, default=600,
                        help="Seconds to wait for teacher verdicts in async mode (default 600)")
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

    # ── Daemon modes (Path A) ────────────────────────────────────────────
    # --principal-daemon and --teacher-both-daemon are the runtime handlers
    # for the persistent Python loops that --serve launches into 2 fixed
    # terminals. They never return in production — only via Ctrl-C, max-ticks
    # (test mode), or --once (test mode). Each handles one tick and sleeps
    # --daemon-interval seconds between ticks.
    # ── Orphan terminal GC (Path A residue cleanup) ──────────────────────
    # Standalone flag for ad-hoc cleanup; also auto-runs as Step 3 of
    # --stop-serve so a re-serve is always clean.
    if args.gc_terminals:
        n = _gc_terminals(dry_run=args.gc_terminals_dry_run)
        suffix = " (dry-run, nothing closed)" if args.gc_terminals_dry_run else ""
        print(f"\n✅ gc-terminals: closed {n} orphaned terminal(s){suffix}")
        return

    if args.principal_daemon:
        principal_dispatch_loop(args)
        return

    if args.teacher_both_daemon:
        teacher_both_loop(args)
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
    skip_readiness: bool = False,
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
        # CE mode is for complex spec-gated tasks only — direct code-
        # implementation tasks need the student to write actual code,
        # not markdown artifacts in docs/solutions/.
        ce_enabled=routing["chosen_skill"] == "rank5_student_plan",
        complex_task=(routing["chosen_skill"] == "rank5_student_plan"),
        skip_readiness=skip_readiness,
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
            print(f"  \u274c Leaf {i+1}/{args.rounds} [{role}] boot failed: {e}")
            if leaf is not None:
                try:
                    leaf.dispose()
                except Exception:
                    pass
            _complete_kanban_task(bd_id)  # close the bd task so it doesn't re-dispatch

    print(f"  \u2705 {len(leaves)}/{args.rounds} worktrees created")
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
                print(f"    \u2705 bead={leaf.bead[:20]} ({len(result.get('response', ''))} chars) — teachers notified")
            else:
                error = result.get("error", result.get("status", "unknown"))
                print(f"    \u274c {result.get('status')}: {error}")
                leaf.dispose()
                _complete_kanban_task(bd_id)
        except Exception as e:
            print(f"    \u274c Hermes call failed: {e}")
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
                    notify_verdict(
                        bead, accepted, cto_v, coo_v,
                        repo=repo,
                        summary=(findings or []) and f"{len(findings)} findings",
                        qodo_findings=(bag or {}).get("qodo_findings", []),
                        qodo_status=(bag or {}).get("qodo_status", "unknown"),
                        cto_findings=(bag or {}).get("cto_findings", []),
                        coo_findings=(bag or {}).get("coo_findings", []),
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠ AgentMail notify failed: {e}")

            except Exception as e:
                print(f"  {idx+1}/{len(dispatched)} {label} \u274c Timeout: {e}")

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


def _run_single_task(args, store):
    """Run a single task (synchronous, Phase 1)."""
    role = _resolve_agent(args)

    print(f"🎓 PRINCIPAL — dispatching {role} / {args.domain}")
    print(f"   Persona: {load_principal_soul()[:80].splitlines()[0]}\n")
    print(f"   Task: {args.task[:100]}")
    print()

    result = _principal_dispatch(
        task=args.task, role=role, domain=args.domain,
        difficulty=args.difficulty, store=store, repo=args.repo,
        doubt_enabled=args.doubt_enabled,
        skip_readiness=bool(args.agent),
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


# ── Daemon-mode helpers (Path A) ──────────────────────────────────────────────


def _send_to_terminal(handle: str, text: str, enter: bool = True) -> None:
    """Send raw text + optional Enter to a terminal handle (best-effort).

    Uses ``orca terminal send --terminal <handle> --text <text>`` so a Python
    daemon can be launched into a freshly-opened permanent terminal. The
    runtime signature was confirmed at /Applications/Orca.app CLI help on
    this build. Best-effort: a transient Orca hiccup must NOT abort --serve —
    the launching handle already exists, so the failure is recoverable.
    """
    try:
        mgr = OrcaExecutionManager()
        send_args = ["terminal", "send", "--terminal", handle, "--text", text]
        if enter:
            send_args.append("--enter")
        mgr._run_orca(send_args, timeout=10)
    except Exception:
        # best-effort
        pass


def _find_unreviewed_beads_for(repo: str) -> list[str]:
    """Return beads still missing cto OR coo verdict (insertion order).

    Insertion order from list_bookbags() is typically mtime-ordered on macOS
    (the directory entries are returned in mtime order), giving FIFO review
    without needing an explicit sort. v2 can layer an explicit mtime sort
    via bookbag.bookbag_path() if needed.
    """
    from bookbag import list_bookbags, read_bookbag
    out: list[str] = []
    try:
        candidates = list_bookbags(repo=repo) or []
    except Exception:
        return out
    for bead in candidates:
        try:
            bag = read_bookbag(bead, repo) or {}
        except Exception:
            continue
        if not bag.get("cto_verdict") or not bag.get("coo_verdict"):
            out.append(bead)
    return out


def save_serve_state(state: dict, path: Path) -> None:
    """Persist the serve-state.json payload (atomic write, mkdir -p parent).

    Writes to a sibling ``.tmp`` first and renames over the target, so a
    concurrent reader never sees a half-written JSON file (avoids the case
    where --stop-serve reads mid-write and gets a JSONDecodeError).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def load_serve_state(path: Path) -> dict:
    """Read serve-state.json; return {} when missing or unparseable.

    Doesn't raise on missing file or invalid JSON — this is read by
    --stop-serve where a missing file simply means "no serve was running".
    """
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _cleanup_legacy_automations(mgr: "OrcaExecutionManager") -> int:
    """Remove agent-school-principal-* and agent-school-teacher-* automations.

    Path A (daemon mode) fully replaces the legacy cron-driven automations, so
    switching serve models should NOT leave the old ones alive — a live user
    observed 67 stray hermes-chat TUIs trace back to a stale principal
    automation never getting GC'd. Each removal failure is logged but does
    not abort the migration; best-effort guarantees --serve still succeeds
    even when one legacy automation is permanently wedged.
    """
    removed = 0
    for a in (orca_automations_list() or []):
        name = a.get("name", "") or ""
        if name.startswith("agent-school-principal") or name.startswith("agent-school-teacher-"):
            try:
                mgr._run_orca(["automations", "remove", "--id", a["id"]], timeout=15)
                print(f"  🗑️ legacy automation removed: {name!r}")
                removed += 1
            except Exception as exc:
                print(f"  ⚠️ legacy automation removal failed for {name!r}: {exc}")
    return removed


def principal_dispatch_loop(args) -> None:
    """Persistent Principal daemon loop (Path A).

    Each tick: dispatch one default task → score inline → sleep --daemon-
    interval. NEVER waits for verdicts — the teacher-both daemon (separate
    terminal) fills them asynchronously. This keeps tick cadence independent
    of teacher latency, which is essential for the principal to dispatch
    a steady stream of beads regardless of how long review takes.

    Trap KeyboardInterrupt cleanly so --stop-serve's terminal close can
    terminate this process without leaving a broken cleanup state.

    Args:
        args: argparse.Namespace with --daemon-interval, --max-ticks, --once,
              --repo, --difficulty, --doubt-enabled.
    """
    repo = args.repo or "__global__"
    print(f"🏫 PRINCIPAL DAEMON — interval={args.daemon_interval}s, repo={repo!r}, "
          f"max-ticks={args.max_ticks or '∞'}, once={args.once}")

    store = ScoreStore(repo=repo)
    tick = 0
    max_ticks = 1 if args.once else (args.max_ticks or 0)
    domain_tasks = _default_tasks()

    while True:
        tick += 1
        try:
            domain, task = domain_tasks[(tick - 1) % len(domain_tasks)]
            role = DOMAIN_ROLE.get(domain, "coder")
            print(f"[principal-daemon] tick {tick}: dispatch {role}/{domain}")
            result = _principal_dispatch(
                task=task, role=role, domain=domain,
                difficulty=args.difficulty, store=store, repo=repo,
                doubt_enabled=args.doubt_enabled,
            )
            status = result.get("status", "?")
            bead = result.get("bead", "?")
            print(f"[principal-daemon] tick {tick}: status={status}, "
                  f"bead={(bead[:20] if bead and bead != '?' else '?')}")
        except KeyboardInterrupt:
            print("[principal-daemon] Ctrl-C — exiting cleanly")
            return
        except Exception as exc:
            print(f"[principal-daemon] tick {tick} error: {exc}")

        if max_ticks and tick >= max_ticks:
            return
        try:
            time.sleep(args.daemon_interval)
        except KeyboardInterrupt:
            print("[principal-daemon] Ctrl-C during sleep — exiting cleanly")
            return


def teacher_both_loop(args) -> None:
    """Persistent Teacher daemon loop (Path A).

    Replaces the 2 separate agent-school-teacher-{cto,coo} cron automations
    with ONE Python process that fills both verdicts per tick (CTO first,
    then COO) on the OLDEST un-reviewed bead. The serial per-tick order
    keeps the bookbag write footprint deterministic, so the principal's
    verdict-wait sees a consistent state on each poll.

    The cto+coo worktrees are boot()ed ONCE for the daemon lifetime (in
    the outer try-block) and disposed in the finally clause ONLY. This
    is critical: closing+re-creating them per tick would re-register with
    Orca and trigger the ``-N`` suffix spray that the close() patch
    already fixes. The terminal itself is split from the worktree
    lifecycle — the terminal is closed by --stop-serve, but the worktree
    cleanup is owned by this daemon's finally clause.

    Args:
        args: argparse.Namespace with --daemon-interval, --max-ticks, --once,
              --repo, --difficulty.
    """
    repo = args.repo or "__global__"
    print(f"🧑‍🏫 TEACHER-BOTH DAEMON — interval={args.daemon_interval}s, repo={repo!r}, "
          f"max-ticks={args.max_ticks or '∞'}, once={args.once}")

    tick = 0
    max_ticks = 1 if args.once else (args.max_ticks or 0)
    cto = coo = None

    try:
        cto = TeacherWorktree("cto", repo=repo)
        coo = TeacherWorktree("coo", repo=repo)
        try:
            cto.boot()
        except Exception as exc:
            print(f"[teacher-both-daemon] CTO boot failed: {exc}")
            cto = None
        try:
            coo.boot()
        except Exception as exc:
            print(f"[teacher-both-daemon] COO boot failed: {exc}")
            coo = None

        while True:
            tick += 1
            try:
                beads = _find_unreviewed_beads_for(repo)
                if beads:
                    bead = beads[0]
                    for role, teacher in (("cto", cto), ("coo", coo)):
                        if teacher is None:
                            print(f"[teacher-both-daemon] tick {tick}: "
                                  f"{role} teacher unavailable — skipped")
                            continue
                        try:
                            reviewed = teacher.review_cycle()
                            print(f"[teacher-both-daemon] tick {tick}: "
                                  f"{role} reviewed={reviewed} (bead={bead[:20]})")
                        except Exception as exc:
                            print(f"[teacher-both-daemon] tick {tick}: "
                                  f"{role} review_cycle error: {exc}")
                    print(f"[teacher-both-daemon] tick {tick}: bead={bead[:20]} "
                          f"both verdicts written (cto + coo)")
                else:
                    print(f"[teacher-both-daemon] tick {tick}: no unreviewed beads")
            except KeyboardInterrupt:
                print("[teacher-both-daemon] Ctrl-C — exiting cleanly")
                return
            except Exception as exc:
                print(f"[teacher-both-daemon] tick {tick} error: {exc}")

            if max_ticks and tick >= max_ticks:
                return
            try:
                time.sleep(args.daemon_interval)
            except KeyboardInterrupt:
                print("[teacher-both-daemon] Ctrl-C during sleep — exiting cleanly")
                return

    except KeyboardInterrupt:
        print("[teacher-both-daemon] Ctrl-C — exiting cleanly")
        return
    finally:
        # Best-effort close — the 3-layer cleanup patches prevent post-mortem
        # -N suffix spray on the NEXT --serve. Never raise on shutdown.
        for teacher in (cto, coo):
            if teacher is not None:
                try:
                    teacher.close()
                except Exception:
                    pass


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
        f"You are the Agent-School Principal (Hermes, -p principal). "
        f"{scope}"
        f"Each tick: read `bd ready` for open beads; classify + EFC-route "
        f"each to a student leaf; wait for both CTO and COO verdicts in "
        f"{bag_ns}<bead>.json; apply the acceptance rule "
        f"(both PASS AND score>=50 AND no critical -> accepted); THEN email "
        f"the human the verdict by running from the repo root: "
        f"python3 -c \"from school_mail import notify_verdict; "
        f"notify_verdict('<bead>', accepted, cto_v, coo_v, repo='{repo}')\" "
        f"— this send is REQUIRED (best-effort: if it fails, log and "
        f"continue, never crash). On /fix from the human, re-dispatch a "
        f"fresh student (never edit yourself). Do not watch terminals or "
        f"read logs."
    )


def _gc_terminals(
    mgr: Optional["OrcaExecutionManager"] = None,
    *,
    dry_run: bool = False,
    print_prefix: str = "  ",
    state_path: Optional[Path] = None,
) -> int:
    """[Path A] Close orphaned Orca terminals that match a GC predicate.

    Default match criteria (both trigger a close):
      1. **Empty / missing title** — residue from buggy serves that opened a
         terminal but never named it. This is the dominant cause of the
         64+ empty terminal tabs observed live: orphan shell exits leave the
         PTY in Orca's sidebar with no title.
      2. **Title starts with ``agent-school-``** — stale tabs from older
         serves under either the legacy cron-automation model OR an
         interrupted Path A serve that didn't reach ``--stop-serve``.

    Conservative match (what is NOT closed): any terminal with a non-empty
    title that doesn't start with ``agent-school-``. So user-named tabs
    like ``Conductor serve command...`` and ``Main branch worktree`` are
    preserved. This is deliberate: a stray --gc-terminals pass should never
    touch deliberate operator workspaces.

    Best-effort: each close is wrapped in try/except so a single wedged
    terminal doesn't poison the whole GC pass. Each failure is logged
    with the terminal's handle so the operator can clean up by hand.

    Args:
        mgr: ``OrcaExecutionManager`` (default: instantiates one). Tests pass
            a ``MagicMock`` so this function never touches live Orca.
        dry_run: when True, list matches but don't close. Returns 0.
        print_prefix: prepended to every log line, e.g. ``"  "`` keeps
            output aligned when called inside --stop-serve's bigger block.

    Returns:
        count of terminals successfully closed (0 when ``dry_run`` or when
        no candidates matched).
    """
    if mgr is None:
        mgr = OrcaExecutionManager()
    try:
        result = mgr._run_orca(["terminal", "list"], timeout=15)
    except Exception as exc:
        print(f"{print_prefix}⚠️ terminal list failed: {exc}")
        return 0

    # Parse the Orca CLI response shape (bare list, or wrapped in
    # ``{"result": {"terminals": [...]}}``, or ``{"terminals": [...]}``).
    terminals: list[dict] = []
    if isinstance(result, list):
        terminals = result
    elif isinstance(result, dict):
        terminals = (
            result.get("terminals")
            or result.get("result", {}).get("terminals")
            or []
        )
    else:
        terminals = []

    # Reviewer #8: load the active serve-state so --gc-terminals NEVER closes
    # a handle that Path A's --serve just launched. Without this guard, the
    # very next cleanup pass would tear down its own daemons — exactly the
    # regression surface by the live dry-run.
    resolved_state_path = (
        Path(state_path) if state_path is not None else SERVE_STATE_PATH
    )
    _live = load_serve_state(resolved_state_path)
    # NOTE: if a Path A daemon dies but this file survives, the guard
    # protects a phantom handle — recovery is operator-level:
    # ``--stop-serve && --serve``.
    live_handles = {
        _live.get("principal_terminal_handle"),
        _live.get("teacher_both_terminal_handle"),
    } - {None, ""}

    # Operator-visible: in dry-run, surface the live daemons we're PROTECTING
    # so the user is not confused why their live sidebar tabs don't appear in
    # the ``would close`` list. Real closes stay quiet by design.
    if dry_run:
        for term in terminals:
            _h = term.get("handle") or term.get("id") or ""
            if _h in live_handles:
                _title = term.get("title") or term.get("name") or "(no-title)"
                print(
                    f"{print_prefix}🛡️ would skip live daemon: "
                    f"{_title!r} (handle={_h})"
                )

    def is_match(t: object) -> bool:
        if not isinstance(t, dict):
            return False
        _raw = t.get("title") or t.get("name") or ""
        title = _raw.strip() if isinstance(_raw, str) else ""
        handle = t.get("handle") or t.get("id") or ""
        # Skip live daemons — their handle is registered in serve-state.
        if handle in live_handles:
            return False
        return (not title) or title.startswith("agent-school-")

    candidates = [t for t in terminals if is_match(t)]

    if not candidates:
        print(
            f"{print_prefix}✅ no orphaned terminals match "
            f"(scanned {len(terminals)})"
        )
        return 0

    failed_handles: list[str] = []
    closed = 0
    for term in candidates:
        handle = term.get("handle") or term.get("id") or ""
        title = (term.get("title") or term.get("name") or "(no-title)")
        if not handle:
            print(f"{print_prefix}ⓘ skipped (no handle): {title!r}")
            continue
        if dry_run:
            print(f"{print_prefix}🔍 would close: {title!r} (handle={handle})")
            continue
        try:
            mgr.close_terminal(handle)
            print(f"{print_prefix}🗑️ closed terminal: {title!r} (handle={handle})")
            closed += 1
        except Exception as exc:
            print(
                f"{print_prefix}⚠️ close failed for {title!r} "
                f"(handle={handle}): {exc}"
            )
            failed_handles.append(handle)
    if failed_handles:
        # Surface handle list inline so 200-terminal runs remain grep-able.
        print(
            f"{print_prefix}⚠️ summary: failed to close "
            f"{len(failed_handles)} terminal(s): {failed_handles}; "
            f"inspect each via `orca terminal close <handle>`"
        )
    return closed


def _launch_serve(state_path: Optional[Path] = None) -> None:
    """Path A: Boot the school via 2 persistent Python daemons in 2 fixed terminals.

    Multi-repo aware (config.github.yaml `target_repos`):

    - Single-repo (target_repos empty) → 1 principal terminal + 1 teacher-
      both terminal. Exactly 2 NEW terminals are opened (or reused if serve-
      state already saved their handles). CTO+COO worktrees are boot()ed so
      the teacher daemon's first tick finds them ready.
    - Multi-repo (target_repos populated) → still 2 terminals (verdicts are
      namespaced per repo via the bookbag path, not via per-repo terminals).

    Principal  →  machine-persistent Python loop (--principal-daemon) in the
                  `agent-school-principal` terminal.
    Teachers   →  machine-persistent Python loop (--teacher-both-daemon) in
                  the `agent-school-teacher-both` terminal that fills BOTH
                  CTO+COO verdicts per tick.

    Legacy GC: any cron-driven `agent-school-principal-*` or
    `agent-school-teacher-*` automations from prior serves are silently
    removed (Path A fully replaces them — verified live; obsoleted 67
    stray hermeschat TUIs).

    Terminal handles are saved to ``state_path`` so a re-serve reuses the
    existing terminals (via `_find_or_create_terminal`'s title-dedup) and
    --stop-serve can read them back to close.

    Args:
        state_path: Override the persistence file (default: SERVE_STATE_PATH
            = ~/.school-core/serve-state.json). Tests pass a tmpdir path so
            they never touch the user's actual file.

    Idempotent: re-running --serve with an existing serve-state.json reuses
    the same 2 terminals and re-sends the daemon launcher commands to each.
    The persistent worktrees survive the re-serve (their canonical names
    `teacher-cto` / `teacher-coo` are preserved by the close() patch).
    """
    mgr = OrcaExecutionManager()
    repo_root = Path(__file__).parent
    resolved_state_path = (
        Path(state_path) if state_path is not None else SERVE_STATE_PATH
    )
    cfg = load_config()
    target_repos: list[dict] = cfg.get("target_repos") or []

    print("🏫 [Path A] launching school via 2 persistent Python daemons...")

    # ── Step 1: GC legacy cron-driven automations (silent migration) ──
    print("  ⓘ legacy-cleanup: removing any prior agent-school-* automations")
    legacy_count = _cleanup_legacy_automations(mgr)
    if legacy_count:
        print(f"  ✅ removed {legacy_count} legacy automations")

    # ── Step 2: Open (or reuse) the 2 fixed daemon terminals ──
    principal_handle = _find_or_create_terminal(mgr, "agent-school-principal")
    teacher_both_handle = _find_or_create_terminal(
        mgr, "agent-school-teacher-both"
    )

    # ── Step 3: Send daemon launcher commands ──
    cmd_principal = (
        f"cd {shlex.quote(str(repo_root))} && "
        f"python3 conductor.py --principal-daemon "
        f"--daemon-interval 1800 --repo __global__"
    )
    _send_to_terminal(principal_handle, cmd_principal)
    print(f"  🚀 principal daemon launched in terminal handle={principal_handle}")

    cmd_teacher_both = (
        f"cd {shlex.quote(str(repo_root))} && "
        f"python3 conductor.py --teacher-both-daemon "
        f"--daemon-interval 60 --repo __global__"
    )
    _send_to_terminal(teacher_both_handle, cmd_teacher_both)
    print(f"  🚀 teacher-both daemon launched in terminal handle={teacher_both_handle}")

    # ── Step 4: Persist handles to serve-state.json ──
    save_serve_state({
        "principal_terminal_handle": principal_handle,
        "teacher_both_terminal_handle": teacher_both_handle,
        "principal_launch_cmd": cmd_principal,
        "teacher_both_launch_cmd": cmd_teacher_both,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_repos": target_repos,
    }, resolved_state_path)
    print(f"  📝 serve-state written: {resolved_state_path}")

    # ── Step 5: Boot the cto+coo worktrees (so the daemon's first tick finds them) ──
    # We boot them HERE -- not in the daemon -- so they belong to the
    # conductor's process lifetime (which is short-lived; --serve returns
    # after Step 4). The daemon process inherits these worktrees via Orca's
    # worktree registry. This avoids -N suffix spray by ensuring the
    # worktree registration is identical between serve runs (only ONE
    # registered worktree per role, ever). Best-effort: the daemon will
    # retry boot on its first tick if this fails (it calls boot() itself).
    try:
        for role in ("cto", "coo"):
            t = TeacherWorktree(role, repo="__global__")
            t.boot()
        print("  ✅ cto + coo worker worktrees booted")
    except Exception as exc:
        print(f"  ⚠️ worker worktree boot failed "
              f"(non-fatal, daemon will retry on first tick): {exc}")

    if target_repos:
        slugs = [
            (e.get("slug") or e.get("repo"))
            for e in target_repos if (e.get("slug") or e.get("repo"))
        ]
        if slugs:
            print(f"  📚 multi-repo target_repos configured: {slugs} "
                  f"(verdicts namespaced per repo; daemons handle all repos)")

    print()
    print("🏫 School is serving (daemon mode — 2 persistent terminals).")
    print("    Stop with: python3 conductor.py --stop-serve")


def _teardown_serve(state_path: Optional[Path] = None) -> None:
    """Path A: Tear down the daemon-mode school.

    Steps:

    1. Read serve-state.json → close the 2 daemon terminals (Orca's
       ``terminal close --handle <handle>`` issues SIGTERM to the PTY and
       disposes it). The Python daemon inside receives the signal and the
       ``finally`` clause of ``teacher_both_loop`` runs its worker-worktree
       cleanup. For the principal, the terminal is the standard in/out pipe
       of the Python loop; closing it triggers Python's normal-exit path.
    2. Delete serve-state.json so the NEXT --serve starts fresh handles
       in case anything was wedged.
    3. Defensive GC of any legacy ``agent-school-*`` automations that may
       have reappeared (e.g. from a partially failed prior --stop-serve).
    4. Close the teacher-cto / teacher-coo worktrees via the patch-fixed
       three-layer ``TeacherWorktree.close()`` so a re-serve cannot mint
       ``teacher-cto-N`` from a stale admin entry.

    Args:
        state_path: Override the persistence file (default: SERVE_STATE_PATH
            = ~/.school-core/serve-state.json). Tests pass a tmpdir path.
    """
    mgr = OrcaExecutionManager()
    resolved_state_path = (
        Path(state_path) if state_path is not None else SERVE_STATE_PATH
    )

    print("🏫 [Path A] tearing down school...")

    # ── Step 1: Close the 2 daemon terminals ──
    state = load_serve_state(resolved_state_path)
    for key, label in (
        ("principal_terminal_handle", "principal"),
        ("teacher_both_terminal_handle", "teacher-both"),
    ):
        handle = state.get(key)
        if not handle:
            print(f"  ⓘ {label} terminal handle not found in serve-state — skipping")
            continue
        try:
            mgr.close_terminal(handle)
            print(f"  🛑 daemon terminal closed: {label} (handle={handle})")
        except Exception as exc:
            print(f"  ⚠️ terminal close for {label} failed: {exc}")

    # ── Step 2: Clear serve-state.json ──
    try:
        if resolved_state_path.exists():
            resolved_state_path.unlink()
            print(f"  🗑️ serve-state cleared: {resolved_state_path}")
    except Exception as exc:
        print(f"  ⚠️ serve-state delete failed: {exc}")

    # ── Step 3: GC orphan terminals (auto-runs as part of --stop-serve) ──
    # Picks up empty-title tabs from prior buggy serves + any leftover
    # agent-school-* terminals that --serve didn't get to clean up because
    # the user crashed/closed the app mid-flight. Skips named user tabs.
    print("  ⓘ gc-terminals: scanning for empty-title + stale agent-school-*")
    gc_count = _gc_terminals(mgr=mgr)
    if gc_count:
        print(f"  ✅ gc-terminals: closed {gc_count} orphaned terminal(s)")

    # ── Step 4: Defensive GC of legacy automations ──
    legacy_count = _cleanup_legacy_automations(mgr)
    if legacy_count:
        print(f"  🗑️ removed {legacy_count} legacy automations (defensive)")

    # ── Step 5: Close the teacher worktrees (3-layer cleanup) ──
    for role in ("cto", "coo"):
        try:
            t = TeacherWorktree(role, repo="__global__")
            t.boot()  # rediscover-or-create (safe: reuses existing)
            t.close()
            print(f"  ⛏ teacher-{role}: worktree closed")
        except Exception as exc:
            print(f"  ⚠️ teacher-{role}: close error — {exc}")

    print()
    print("✅ School torn down.")


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
    reuse_session: bool = True,
) -> Optional[str]:
    """Create (or reuse) a scheduling automation via Orca.

    Mirrors the principal migration: Orca owns the schedule, so the teacher
    review loop no longer lives in a while-True pane + per-boot terminal
    spray (run_teacher_loop.py). Returns the automation id, or ``None`` if
    creation failed.

    CRITICAL (fixes the session spray): default ``reuse_session=True`` and
    ``--workspace-mode existing``. Without these, each scheduled tick launches
    a NEW ``hermes chat --tui`` session against a new-per-run worktree that
    NEVER EXITS, so the 2 teacher automations pile up dozens of interactive
    TUIs + their codegraph servers every few minutes. ``existing`` reuses the
    persistent teacher worktree we boot; ``reuse-session`` reuses one live
    session per automation instead of spawning a fresh one per tick. The
    prompt itself runs `run_teacher_review_once.py` (a one-shot script), so a
    single reused session is exactly the intended "persistent teacher" model.
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
        "--workspace-mode", "existing",
        "--json",
    ]
    if reuse_session:
        args += ["--reuse-session"]
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
                trigger="* * * * *",  # every minute — fast enough for single-issue async handoff
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
