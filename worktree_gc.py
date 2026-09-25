"""Worktree GC — scheduled janitor for stale git worktree rot.

WHY THIS EXISTS
---------------
2026-09-23 audit: 41 registrations across three MAIN repos, only 12 real.
13 gaia-gate-* + 11 sr-bead-pool/* entries in /tmp died on reboot but their
git registrations persisted as `prunable`. 6 orphaned orca-tcc-login shells
(oldest 3d22h). 5 husk repo dirs containing only `.orca-worktree-trash`
tombstones. The bloat guard (worktree_bloat_guard.py, N9) is a DETECTOR —
it fails loudly when spray crosses a threshold but never cleans. This module
is the JANITOR half: runs on a schedule (launchd), prunes what provably rotted,
keeps everything else.

SAFETY RULES (the whole point — read before editing)
----------------------------------------------------
1. DRY-RUN BY DEFAULT. ``--apply`` is required for any mutation.
2. A worktree is removed ONLY if ALL of these hold:
     - not the repo's main checkout, not bare, not locked
     - working tree is CLEAN (``git status --porcelain`` empty; git error → KEEP)
     - HEAD is fully merged into the repo's main branch (no main ref → KEEP)
     - older than MIN_AGE_HOURS (default 48) — grace for freshly merged work
     - no live process has its cwd inside the worktree (lsof; can't tell → KEEP)
   Every unverifiable condition fails CLOSED (keep). A janitor that guesses
   wrong destroys work; one that skips does nothing worse than defer.
3. NEVER ``--force``. If ``git worktree remove`` refuses (dirty race since
   classification), the entry is logged and kept. Force is a human decision.
4. Prunable entries (dir gone, registration stale) are pruned — that is the
   exact rot from the gaia-gate/sr-bead-pool incident and carries zero risk:
   git refuses to prune a registration whose directory still exists.
5. Append-only JSONL run log so every action is auditable after the fact
   (same discipline as data/throughput_runs.jsonl).

USAGE
-----
    python3 worktree_gc.py                    # dry-run, all default repos
    python3 worktree_gc.py --apply            # actually clean
    python3 worktree_gc.py --repos ~/OmniRoute --min-age-hours 24
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DEFAULT_REPOS = [
    str(Path.home() / "school-core"),
    str(Path.home() / "sound-royale-ny"),
    str(Path.home() / "OmniRoute"),
]
DEFAULT_LOG = Path.home() / "school-core" / "logs" / "worktree_gc.jsonl"
MIN_AGE_HOURS = 48
MAIN_BRANCH = "main"


# ── Parsing ──────────────────────────────────────────────────────────────────

@dataclass
class WorktreeEntry:
    path: str
    head: Optional[str] = None
    branch: Optional[str] = None
    locked: bool = False
    prunable: bool = False
    bare: bool = False


def parse_worktree_list(text: str) -> list[WorktreeEntry]:
    """Parse `git worktree list --porcelain` (v1) into entries."""
    entries: list[WorktreeEntry] = []
    cur: Optional[WorktreeEntry] = None
    for line in text.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = None
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = WorktreeEntry(path=val)
        elif cur is None:
            continue
        elif key == "HEAD":
            cur.head = val
        elif key == "branch":
            cur.branch = val.removeprefix("refs/heads/")
        elif key == "locked":
            cur.locked = True
        elif key == "prunable":
            cur.prunable = True
        elif key == "bare":
            cur.bare = True
    if cur:
        entries.append(cur)
    return entries


# ── Pure decision logic (unit-testable without git) ──────────────────────────

ACTION_PRUNE = "PRUNE"
ACTION_REMOVE = "REMOVE"
ACTION_KEEP = "KEEP"


def decide(
    entry: WorktreeEntry,
    *,
    is_main: bool,
    dirty: Optional[bool],
    merged: Optional[bool],
    age_hours: Optional[float],
    active: Optional[bool],
    git_ok: bool = True,
    min_age_hours: float = MIN_AGE_HOURS,
) -> tuple[str, str]:
    """Return (action, reason). None-able facts fail CLOSED to KEEP."""
    if entry.prunable:
        return ACTION_PRUNE, "registration points at missing directory"
    if entry.bare or is_main or entry.locked:
        return ACTION_KEEP, "bare/main/locked — never touched"
    if not git_ok:
        return ACTION_KEEP, "git inspection failed — fail closed"
    if any(v is None for v in (dirty, merged, age_hours, active)):
        return ACTION_KEEP, "unverifiable fact (None) — fail closed"
    if dirty:
        return ACTION_KEEP, "uncommitted changes present"
    if not merged:
        return ACTION_KEEP, "HEAD not fully merged into main"
    if age_hours is None or age_hours < min_age_hours:
        return ACTION_KEEP, f"younger than {min_age_hours:g}h grace"
    if active:
        return ACTION_KEEP, "a live process has cwd inside it"
    return ACTION_REMOVE, "clean + merged + aged + unattended"


# ── Fact gathering (git + os) ────────────────────────────────────────────────

def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command; an unlaunchable binary degrades to a failed result so
    callers hit their fail-closed paths instead of the sweep crashing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, -1, "", f"unlaunchable: {e}")


def gather_facts(
    repo: Path,
    entry: WorktreeEntry,
    *,
    now: float,
    live_paths: set[str],
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> dict:
    """Collect the facts `decide` needs. Any failure degrades to KEEP."""
    facts = {"is_main": False, "dirty": None, "merged": None,
             "age_hours": None, "active": None, "git_ok": True}
    wt = Path(entry.path)
    try:
        repo_real = repo.resolve()
        wt_real = wt.resolve()
        facts["is_main"] = wt_real == repo_real
    except OSError:
        facts["is_main"] = False

    if not wt.exists():
        # dir vanished but git didn't mark prunable yet — prune handles it
        facts["git_ok"] = False
        return facts

    # dirty?
    r = run(["git", "-C", str(wt), "status", "--porcelain"])
    if r.returncode != 0:
        facts["git_ok"] = False
        return facts
    facts["dirty"] = bool(r.stdout.strip())

    # merged into main?
    r = run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
             "HEAD", MAIN_BRANCH])
    facts["merged"] = (r.returncode == 0) if r.returncode in (0, 1) else None
    if facts["merged"] is None:
        facts["git_ok"] = False
        return facts

    # age: max(mtime of .git pointer file, of the dir itself)
    try:
        gitfile = wt / ".git"
        base = gitfile.stat().st_mtime if gitfile.is_file() else wt.stat().st_mtime
        dir_m = max(p.stat().st_mtime for p in [wt])
        facts["age_hours"] = (now - max(base, dir_m)) / 3600.0
    except OSError:
        facts["age_hours"] = None  # fail closed

    # live cwd inside? None = inspection unavailable → decide() fails closed
    if live_paths is None:
        facts["active"] = None
    else:
        wt_s = str(wt_real)
        facts["active"] = any(
            p == wt_s or p.startswith(wt_s + os.sep) for p in live_paths
        )
    return facts


def live_cwds(run: Callable[..., subprocess.CompletedProcess] = _run) -> Optional[set[str]]:
    """All cwd paths of running processes (user-visible).

    None means inspection FAILED (lsof missing/denied) — callers must treat
    that as 'unknown' and fail closed. An empty set on success is a real
    (if unusual) answer: no process cwds were visible.
    """
    r = run(["lsof", "-w", "-d", "cwd", "-Fn"])
    if r.returncode != 0:
        return None
    return {
        line[1:] for line in r.stdout.splitlines()
        if line.startswith("n/") and os.path.isdir(line[1:])
    }


# ── Sweep ────────────────────────────────────────────────────────────────────

@dataclass
class SweepResult:
    repo: str
    decisions: list[tuple[str, str, str]] = field(default_factory=list)  # path, action, reason
    errors: list[str] = field(default_factory=list)

    @property
    def removed(self) -> int:
        return sum(1 for _, a, _ in self.decisions if a == ACTION_REMOVE)

    @property
    def pruned(self) -> int:
        return sum(1 for _, a, _ in self.decisions if a == ACTION_PRUNE)


def sweep(
    repo: Path,
    *,
    apply: bool,
    min_age_hours: float = MIN_AGE_HOURS,
    run: Callable[..., subprocess.CompletedProcess] = _run,
    log: Optional[Callable[[str], None]] = None,
) -> SweepResult:
    result = SweepResult(repo=str(repo))
    if not (repo / ".git").exists():
        result.errors.append(f"{repo}: not a git repo, skipping")
        return result

    r = run(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if r.returncode != 0:
        result.errors.append(f"{repo}: worktree list failed: {r.stderr.strip()}")
        return result

    entries = parse_worktree_list(r.stdout)
    paths = live_cwds(run=run)
    now = time.time()

    for entry in entries:
        if entry.prunable:
            action, reason = ACTION_PRUNE, "stale registration (dir missing)"
        elif entry.bare:
            action, reason = ACTION_KEEP, "bare"
        else:
            facts = gather_facts(repo, entry, now=now, live_paths=paths, run=run)
            action, reason = decide(
                entry,
                is_main=facts["is_main"],
                dirty=facts["dirty"],
                merged=facts["merged"],
                age_hours=facts["age_hours"],
                active=facts["active"],
                git_ok=facts["git_ok"],
                min_age_hours=min_age_hours,
            )
        result.decisions.append((entry.path, action, reason))

    if apply:
        for path, action, _ in result.decisions:
            if action == ACTION_PRUNE:
                r = run(["git", "-C", str(repo), "worktree", "prune"])
                if r.returncode != 0:
                    result.errors.append(f"prune failed: {r.stderr.strip()}")
                break  # one prune clears all stale entries
        for path, action, _ in result.decisions:
            if action != ACTION_REMOVE:
                continue
            r = run(["git", "-C", str(repo), "worktree", "remove", path])
            if r.returncode != 0:
                # race (went dirty since classification) or git refusal — keep, never force
                result.errors.append(
                    f"remove refused {path}: {r.stderr.strip()} — KEPT (no force)"
                )
            elif log:
                log(f"removed {path}")
    return result


# ── Run log (append-only JSONL) ──────────────────────────────────────────────

def append_log(log_path: Path, record: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"warn: could not write run log: {e}", file=sys.stderr)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually prune/remove (default: dry-run)")
    ap.add_argument("--repos", nargs="*", default=None,
                    help="repos to sweep (default: school-core, sound-royale-ny, OmniRoute)")
    ap.add_argument("--min-age-hours", type=float, default=MIN_AGE_HOURS)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    repos = [Path(p).expanduser() for p in (args.repos or DEFAULT_REPOS)]
    mode = "apply" if args.apply else "dry-run"
    results: list[SweepResult] = []

    for repo in repos:
        res = sweep(repo, apply=args.apply, min_age_hours=args.min_age_hours)
        results.append(res)
        if not args.quiet:
            print(f"== {repo} [{mode}]")
            for path, action, reason in res.decisions:
                print(f"  {action:7} {path}  — {reason}")
            for err in res.errors:
                print(f"  ERROR: {err}", file=sys.stderr)

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": mode,
        "min_age_hours": args.min_age_hours,
        "repos": [str(r) for r in repos],
        "removed": sum(x.removed for x in results),
        "pruned": sum(x.pruned for x in results),
        "kept": sum(
            1 for x in results for _, a, _ in x.decisions if a == ACTION_KEEP
        ),
        "errors": [e for x in results for e in x.errors],
        "details": [
            {"repo": x.repo,
             "decisions": [{"path": p, "action": a, "reason": rn}
                           for p, a, rn in x.decisions]}
            for x in results
        ],
    }
    append_log(args.log, record)
    if not args.quiet:
        print(f"\n[{mode}] removed={record['removed']} pruned={record['pruned']} "
              f"kept={record['kept']} errors={len(record['errors'])} "
              f"log={args.log}")
    return 1 if record["errors"] and args.apply else 0


if __name__ == "__main__":
    sys.exit(main())
