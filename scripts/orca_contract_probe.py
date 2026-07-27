#!/usr/bin/env python3
"""
orca_contract_probe.py — Capture the REAL Orca CLI JSON shapes.

school-core's `orca_executor.py` parses a *specific* JSON contract
(`status.runtime.state`, `worktree.id` = "uuid::path",
`terminal.handle`, `terminals[].title`, etc.). These were asserted,
never verified against a live Orca. This probe runs the actual commands
against a running Orca desktop and prints the raw JSON so the contract
can be diffed.

Prereqs:
    - Orca desktop running (orca open)
    - CLI enabled (Settings -> Experimental -> CLI)
    - Run from the school-core repo root (or pass --repo-path)

Side effects (all self-cleaned):
    - Creates a temporary worktree "probe-<ts>" and removes it.
    - Creates a temporary terminal and closes it.

Usage:
    python3 scripts/orca_contract_probe.py
    python3 scripts/orca_contract_probe.py --repo-path /abs/path/to/repo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def run(args: list[str], timeout: int = 30) -> dict:
    """Run an orca command; return dict with cmd/rc/stdout/stderr/parsed."""
    cmd = ["orca", *args, "--json"]
    print(f"\n=== $ {' '.join(cmd)} ===")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        print("  [FATAL] `orca` CLI not found on PATH. Is Orca installed?")
        return {"cmd": cmd, "rc": None, "stdout": "", "stderr": "orca not on PATH", "parsed": None}
    except subprocess.TimeoutExpired as e:
        print(f"  [TIMEOUT after {timeout}s]")
        return {"cmd": cmd, "rc": "timeout", "stdout": e.stdout or "", "stderr": e.stderr or "", "parsed": None}

    print(f"  returncode: {proc.returncode}")
    print(f"  stdout:\n{proc.stdout}".rstrip())
    if proc.stderr.strip():
        print(f"  stderr:\n{proc.stderr}".rstrip())
    parsed = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            print(f"  [parse error] {e}")
    return {"cmd": cmd, "rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "parsed": parsed}


def keypath_exists(d: object, dotted: str) -> bool:
    cur = d
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the live Orca CLI contract.")
    parser.add_argument(
        "--repo-path",
        default=str(Path(__file__).resolve().parent.parent),
        help="Absolute path to the repo to test worktree creation against.",
    )
    args = parser.parse_args()
    import shutil

    repo_path = Path(args.repo_path).resolve()

    print(f"Repo path under test: {repo_path}")
    print(f"orca on PATH: ", end="")
    which = shutil.which("orca") or "(NOT FOUND)"
    print(which)

    # ── 1. status (verifies framework assumption: result.runtime.state == "ready") ──
    status = run(["status"])
    status_parsed = status["parsed"]
    status_ok = keypath_exists(status_parsed, "runtime.state") if isinstance(status_parsed, dict) else False
    if isinstance(status_parsed, dict):
        top_keys = list(status_parsed.keys())
        rt_keys = list(status_parsed.get("runtime", {}).keys()) if isinstance(status_parsed.get("runtime"), dict) else None
    else:
        top_keys, rt_keys = None, None

    # ── 2. repo list (verifies --repo id:<id> vs abs-path selector) ──
    repo_list = run(["repo", "list"])
    repo_id = None
    if isinstance(repo_list["parsed"], dict):
        repos = repo_list["parsed"].get("repos", repo_list["parsed"].get("repositories", []))
        for r in repos if isinstance(repos, list) else []:
            if isinstance(r, dict) and repo_path.as_posix() in (r.get("path", "") or ""):
                repo_id = r.get("id")
                break

    # Register the repo if not present (mirrors what a correct bootstrap would do)
    if not repo_id:
        add = run(["repo", "add", "--path", str(repo_path)], timeout=30)
        if isinstance(add["parsed"], dict):
            repo_id = (
                add["parsed"].get("id")
                or add["parsed"].get("repo", {}).get("id")
            )
        # re-list to confirm
        if not repo_id:
            repo_list2 = run(["repo", "list"])
            if isinstance(repo_list2["parsed"], dict):
                repos = repo_list2["parsed"].get("repos", repo_list2["parsed"].get("repositories", []))
                for r in repos if isinstance(repos, list) else []:
                    if isinstance(r, dict) and repo_path.as_posix() in (r.get("path", "") or ""):
                        repo_id = r.get("id")
                        break

    # ── 3. worktree create with ABS-PATH selector (framework's CURRENT call) ──
    probe_name = f"probe-{int(time.time())}"
    create_path = run(
        ["worktree", "create", "--name", probe_name, "--repo", str(repo_path)],
        timeout=30,
    )
    # ── 3b. worktree create with id:<repoId> selector (doc-canonical form) ──
    create_id = None
    if repo_id:
        create_id = run(
            ["worktree", "create", "--name", f"{probe_name}-id", "--repo", f"id:{repo_id}"],
            timeout=30,
        )

    # Extract worktree id + path from whichever create succeeded
    created_wt: dict | None = None
    created_selector: str | None = None  # "path" or "id"
    for res, sel in ((create_path, "path"), (create_id, "id")):
        p = res["parsed"]
        if isinstance(p, dict):
            wt = p.get("worktree", p)
            if isinstance(wt, dict) and wt.get("id"):
                created_wt = wt
                created_selector = sel
                break

    # ── 4. worktree list (verify worktrees[].displayName/name/path shape) ──
    wt_list = run(["worktree", "list"])

    # ── 5. terminal create WITHOUT --worktree (framework's current call) ──
    term_create = run(["terminal", "create", "--title", f"{probe_name}-term"], timeout=15)
    handle = None
    if isinstance(term_create["parsed"], dict):
        term = term_create["parsed"].get("terminal", term_create["parsed"])
        handle = term.get("handle") if isinstance(term, dict) else None

    # ── 6. terminal list (verify terminals[].title/handle) ──
    term_list = run(["terminal", "list"])

    # ── 7. terminal read (verify tail/nextCursor/latestCursor) ──
    term_read = None
    if handle:
        term_read = run(["terminal", "read", "--terminal", handle, "--limit", "50"], timeout=15)

    # ── Cleanup ──
    cleanup_notes = []
    if created_wt and created_wt.get("id"):
        rm = run(["worktree", "rm", "--worktree", f"id:{created_wt['id']}", "--force"], timeout=20)
        cleanup_notes.append(f"worktree rm id:{created_wt['id']} -> rc={rm['rc']}")
    if handle:
        cl = run(["terminal", "close", "--terminal", handle], timeout=10)
        cleanup_notes.append(f"terminal close {handle} -> rc={cl['rc']}")

    # ── SUMMARY: diff against orca_executor.py assumptions ──
    print("\n" + "=" * 70)
    print("CONTRACT DIFF (framework assumption  ->  live reality)")
    print("=" * 70)

    def line(label, assumption, reality):
        print(f"\n• {label}")
        print(f"    framework: {assumption}")
        print(f"    live     : {reality}")

    line(
        "status shape",
        'status -> result.runtime.state == "ready"',
        f"top-level keys={top_keys}; runtime keys={rt_keys}; "
        f"has runtime.state={status_ok}; raw state="
        f"{status_parsed.get('runtime', {}).get('state') if isinstance(status_parsed, dict) else 'N/A'}",
    )
    line(
        "--repo selector",
        f"--repo {repo_path}  (absolute path)",
        f"path-create rc={create_path['rc']} | "
        f"id:{repo_id} create rc={create_id['rc'] if create_id else 'skipped (no repo id)'} | "
        f"repo_id found={repo_id}",
    )
    line(
        "worktree create response",
        '{"worktree": {"id": "uuid::path", ...}}',
        f"created via '{created_selector}' selector; wt={created_wt}",
    )
    line(
        "worktree list items",
        "result.worktrees[] with displayName/name/path",
        f"keys in first item="
        f"{list((wt_list['parsed'].get('worktrees') or [{}])[0].keys()) if isinstance(wt_list['parsed'], dict) else 'N/A'}",
    )
    line(
        "terminal create response",
        '{"terminal": {"handle": "..."}}',
        f"handle={handle} (from {term_create['parsed']})",
    )
    line(
        "terminal list items",
        "result.terminals[] with title/handle",
        f"keys in first item="
        f"{list((term_list['parsed'].get('terminals') or [{}])[0].keys()) if isinstance(term_list['parsed'], dict) else 'N/A'}",
    )
    line(
        "terminal read shape",
        '{"tail":[...], "nextCursor", "latestCursor"}',
        f"keys={list(term_read['parsed'].keys()) if isinstance(term_read, dict) and term_read.get('parsed') else (term_read['parsed'] if term_read else 'skipped')}",
    )

    print("\n" + "=" * 70)
    print("CLEANUP")
    for n in cleanup_notes:
        print(f"  - {n}")
    print("=" * 70)

    # Exit non-zero if status failed (can't trust the rest)
    if status["rc"] not in (0,):
        print("\n[WARN] `orca status` failed — Orca may not be running or CLI not enabled.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
