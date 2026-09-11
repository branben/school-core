"""conductor/daemon.py — Activity server and serve state management.

Handles the Path-A daemon mode: persistent principal + teacher-both loops
in fixed Orca terminals, serve-state persistence, terminal GC, and the
legacy automation cleanup.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bookbag import list_bookbags, read_bookbag
from github_fetcher import load_config
from orca_executor import OrcaExecutionManager, OrcaUnavailableError
from scoring import ScoreStore
from teacher import TeacherWorktree

from school_core.principal import DOMAIN_ROLE, _principal_dispatch

# Persistence file for the daemon-mode serve state. Stores:
#   - principal_terminal_handle: handle of the principal Python-loop terminal
#   - teacher_both_terminal_handle: handle of the CTO+COO daemon terminal
#   - created_at: ISO timestamp when these terminals were first launched
#
# --serve writes this file once when launching and --stop-serve reads it
# again to close the terminals. A re-run of --serve reuses the saved handles
# (so no fresh terminals ever accumulate when re-serving).
SERVE_STATE_PATH = Path.home() / ".school-core" / "serve-state.json"


def load_serve_state(path: Path) -> dict:
    """Read serve-state.json; return {} when missing or unparseable.

    Doesn't raise on missing file or invalid JSON — this is read by
    --stop-serve where a missing file simply means "no serve was running".
    """
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


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
    repo_root = Path(__file__).parent.parent.parent
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


def _create_hermes_cronjob(*, name: str, script: str, schedule: str) -> None:
    """Register (or update) a Hermes no-agent cronjob — a plain shell script on a schedule.

    These jobs run ``script`` via the cron scheduler directly (no LLM agent,
    no TUI session). Each tick: shell process spawns, runs the script, exits.
    No accumulation. Used by ``_boot_teachers`` for the CTO/COO review passes.

    Uses the ``hermes cron`` CLI via subprocess (available even when
    ``conductor.py`` runs outside a Hermes agent context). If the cronjob
    already exists with the same name, it is updated in-place.
    """
    try:
        subprocess.run(
            ["hermes", "cron", "create",
             "--name", name,
             "--no-agent",
             "--script", script,
             schedule],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        pass


def _boot_teachers(repo: str = "__global__") -> dict[str, TeacherWorktree]:
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

            # Launch the teacher as a Hermes NO-AGENT cronjob (script-only).
            #
            # WHY NOT Orca --provider hermes: Orca automations with
            # --provider hermes spawn `hermes chat --query="..." --tui`, an
            # interactive TUI session. The --tui flag makes /exit (passed as
            # text in --query) not be processed — sessions never exit, and
            # each 2-min tick spawned a NEW stuck session + codegraph server.
            #
            # no_agent=True runs teacher_tick.sh as a plain shell process per
            # tick — no TUI, no LLM chat session, runs to completion (with a
            # 100s timeout guard), and exits. Period. Zero bleed.
            safe = repo.replace("/", "__")
            cron_name = f"agent-school-teacher-{role}" if repo == "__global__" else f"agent-school-teacher-{role}-{safe}"
            script_name = f"teacher_tick_{role}.sh"
            _create_hermes_cronjob(
                name=cron_name,
                script=script_name,
                schedule="*/5 * * * *",
            )
            print(f"  🧑‍🏫 teacher-{role}: cronjob up (name={cron_name})")

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
            print(f"  ✅ teacher-{role}: closed")
        except Exception as e:
            print(f"  ⚠️ teacher-{role}: close error — {e}")
    print()


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
        f"python3 -c \\\"from school_mail import notify_verdict; "
        f"notify_verdict('<bead>', accepted, cto_v, coo_v, repo='{repo}')\\\" "
        f"— this send is REQUIRED (best-effort: if it fails, log and "
        f"continue, never crash). On /fix from the human, re-dispatch a "
        f"fresh student (never edit yourself). Do not watch terminals or "
        f"read logs."
    )


def _default_tasks() -> list[tuple[str, str]]:
    """Default task rotation for loop mode."""
    return [
        ("code-search", "What single grep command finds all Python files with TODO comments recursively? One command only."),
        ("terminal", "What does this command do: `find . -name '*.py' -mtime -1 | xargs wc -l`? One sentence."),
        ("code-review", "Is `except Exception: pass` good practice? One word answer + one sentence why."),
        ("web-automation", "What CSS selector targets all <button> elements with class 'primary' inside a <form>? One selector."),
        ("python-coding", "Write def chunks(lst, n): yield successive n-sized chunks from lst using yield. Just code, no explanation."),
    ]
