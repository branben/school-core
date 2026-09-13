#!/usr/bin/env python3
"""prime.py — emit a ticket-scoped context block for the inner-loop "Prime" step.

The outer loop decides *what* to build (PRD → SPEC → ticket). Prime is the
first step of the inner loop: given ONE ticket, it gathers only the context
that ticket needs and emits a handoff-shaped block:

    Metadata            who/what/when, ticket identity, repo identity
    Task                the one testable concern (issue body, normalized)
    Code Paths          repo tree + files relevant to the ticket keywords
    Pre-Dispatch Evidence  last_run status, prior solution docs, live Orca
                        worktrees overlapping candidate files
    Guardrails          allowlist / privacy / no-write defaults
    Verification        concrete commands that prove the task done

Ticket sources are tried in order so the tool works on the Mac (bd reachable)
and in this sandbox (GitHub issues live, beads export absent):

    1. `bd show <ticket>`               if the `bd` CLI is on PATH
    2. `.beads/issues.jsonl`            passive export, if present
    3. GitHub issue (`#N`, `owner/repo#N`, URL) via `gh`
    4. local file path (plan/Spec/PRD markdown)

Context layers follow the vault's documented order and degrade gracefully:
codegraph → serena → cocoindex are unreachable from the sandbox; Prime uses
repo_reader (always) + Obsidian live vault (if bridge up) + Orca worktree
radar (if paired) and prints a clear note when a layer is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Repo-relative paths (this file lives in scripts/).
ROOT = Path(__file__).resolve().parent.parent
# Make sibling modules importable regardless of how prime.py is invoked
# (python3 scripts/prime.py puts scripts/ on sys.path, not the repo root).
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Ticket loading
# ---------------------------------------------------------------------------


def _parse_issue_ref(ref: str) -> Optional[tuple[str, str, int]]:
    """Parse a GitHub issue reference into (owner, repo, number).

    Accepts ``owner/repo#123``, ``#123``, or a full GitHub issue URL.
    Returns None if the ref is not a GitHub issue reference.
    """
    m = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$", ref)
    if m:
        owner, repo = m.group(1).split("/", 1)
        return owner, repo, int(m.group(2))
    m = re.match(r"^#(\d+)$", ref)
    if m:
        return None  # resolved against default repo later
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", ref)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def _default_repo() -> str:
    """Resolve the checkout origin slug (owner/name), best-effort."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        url = out.stdout.strip()
    except Exception:
        url = ""
    if not url:
        return "branben/school-core"
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else "branben/school-core"


def _gh_issue(ref: str) -> Optional[dict]:
    """Read a GitHub issue via the `gh` CLI. Returns normalized dict or None."""
    try:
        out = subprocess.run(
            ["gh", "issue", "view", ref, "--json",
             "number,title,body,labels,state,milestone,url,author,createdAt,updatedAt",
             "--repo", _default_repo()],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except Exception:
        return None
    labels = [l.get("name", "") for l in (data.get("labels") or []) if isinstance(l, dict)]
    return {
        "source": "github",
        "id": f"{_default_repo()}#{data.get('number')}",
        "title": data.get("title", ""),
        "body": data.get("body") or "",
        "labels": labels,
        "state": data.get("state", ""),
        "milestone": (data.get("milestone") or {}).get("title") if data.get("milestone") else None,
        "url": data.get("url", ""),
        "author": (data.get("author") or {}).get("login", ""),
        "created_at": data.get("createdAt", ""),
        "updated_at": data.get("updatedAt", ""),
    }


def _beads_jsonl(ticket: str) -> Optional[dict]:
    """Read a ticket from a passive `.beads/issues.jsonl` export, if present."""
    path = ROOT / ".beads" / "issues.jsonl"
    if not path.exists():
        return None
    ident = ticket.lower()
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = str(rec.get("id", "")).lower()
            if rid == ident or rid.endswith(ident):
                title = rec.get("title", "")
                body = rec.get("body") or rec.get("description") or ""
                return {
                    "source": "beads",
                    "id": rec.get("id", ticket),
                    "title": title,
                    "body": body,
                    "labels": rec.get("labels") or [],
                    "state": rec.get("status") or rec.get("state") or "",
                    "milestone": None,
                    "url": "",
                    "author": rec.get("author") or rec.get("creator") or "",
                    "created_at": rec.get("created_at", ""),
                    "updated_at": rec.get("updated_at", ""),
                }
    except Exception:
        return None
    return None


def _bd_show(ticket: str) -> Optional[dict]:
    """Read a beads issue via the `bd` CLI if it is on PATH."""
    import shutil
    if not shutil.which("bd"):
        return None
    try:
        out = subprocess.run(
            ["bd", "show", ticket, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
    except Exception:
        return None
    # bd emits the issue under key 'issue' (or the top level) — normalize both.
    rec = data.get("issue") if isinstance(data, dict) else None
    if rec is None and isinstance(data, dict) and "id" in data:
        rec = data
    if not rec:
        return None
    body = rec.get("body") or rec.get("description") or rec.get("comments_text") or ""
    return {
        "source": "beads",
        "id": rec.get("id", ticket),
        "title": rec.get("title", ""),
        "body": body,
        "labels": rec.get("labels") or [],
        "state": rec.get("status") or rec.get("state") or "",
        "milestone": None,
        "url": rec.get("url", ""),
        "author": rec.get("author") or rec.get("creator") or "",
        "created_at": rec.get("created_at", ""),
        "updated_at": rec.get("updated_at", ""),
    }


def _local_doc(ticket: str) -> Optional[dict]:
    """Read a plan/spec/PRD markdown path as a ticket body (best-effort local)."""
    p = Path(ticket).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        return None
    text = p.read_text(errors="replace")
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return {
        "source": "file",
        "id": str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p),
        "title": title_match.group(1).strip() if title_match else p.name,
        "body": text,
        "labels": [],
        "state": "",
        "milestone": None,
        "url": "",
        "author": "",
        "created_at": "",
        "updated_at": "",
    }


def load_ticket(ticket: str) -> dict:
    """Load a ticket from the first working source. Raises ValueError if none."""
    loaders = [
        ("bd", lambda: _bd_show(ticket)),
        ("beads-jsonl", lambda: _beads_jsonl(ticket)),
        ("github", lambda: _gh_issue(ticket)),
        ("file", lambda: _local_doc(ticket)),
    ]
    seen = []
    for name, fn in loaders:
        try:
            rec = fn()
        except Exception as exc:  # pragma: no cover - defensive
            seen.append(f"{name}: {exc}")
            continue
        if rec:
            return rec
        seen.append(name)
    raise ValueError(
        f"Cannot read ticket {ticket!r}. Tried: {', '.join(seen)}. "
        "Supply a GitHub issue ref (e.g. #123 / owner/repo#123), a beads id, "
        "or a path to a plan/spec markdown file."
    )


# ---------------------------------------------------------------------------
# Context layers
# ---------------------------------------------------------------------------


def _repo_tree() -> str:
    """Return a compact repo file tree (top 3 levels, git-tracked)."""
    try:
        from repo_reader import get_file_tree
        return get_file_tree(ROOT, max_depth=3)
    except Exception:
        return ""


def _relevant_files(ticket: dict, max_files: int = 8) -> list:
    """Return tracked repo paths relevant to the ticket's title+body keywords.

    Deterministic scorer: filename hits are weighted above content hits so the
    result doesn't depend on git-grep exit semantics. Falls back to an empty
    list only when nothing matches at all.
    """
    try:
        from repo_reader import extract_keywords
    except Exception:
        return []
    text = f"{ticket.get('title', '')} {ticket.get('body', '')}"
    keywords = extract_keywords(text, max_keywords=12)
    if os.environ.get("PRIME_DEBUG"):
        print(f"[prime-debug] title={ticket.get('title','')[:40]!r} body_len={len(ticket.get('body',''))}", file=sys.stderr)
        print(f"[prime-debug] keywords={keywords}", file=sys.stderr)
    if not keywords:
        return []

    # All tracked source-ish files, cached per-process for the loop.
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    source_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".rs", ".java",
        ".sh", ".yaml", ".yml", ".json", ".md",
    }
    files = [f for f in files
             if len(f) < 200 and (Path(f).suffix in source_exts or Path(f).name == "Dockerfile")
             and not f.startswith("data/")]  # runtime state, never a coding surface

    scored = []
    for f in files:
        fname = Path(f).stem.lower()
        score = 0
        for kw in keywords:
            kwl = kw.lower()
            if kwl in fname:
                score += 3
            elif kwl in f.lower():  # any path segment
                score += 2
        if score > 0:
            scored.append((score, f))

    # Content-grep tiebreaker for the top filename candidates (bounded, safe).
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[: max_files * 2]
    if scored and len(top) < max_files:
        first_kw = keywords[0]
        try:
            cg = subprocess.run(
                ["git", "-C", str(ROOT), "grep", "-l", "-i", "--", first_kw],
                capture_output=True, text=True, timeout=30,
            ).stdout
        except Exception:
            cg = ""
        for f in cg.splitlines():
            if not f.strip() or f in {s for _, s in scored} or f.startswith("data/"):
                continue
            if len(f) < 200 and Path(f).suffix in source_exts:
                scored.append((1, f))
        scored.sort(key=lambda x: (-x[0], x[1]))

    if os.environ.get("PRIME_DEBUG"):
        print(f"[prime-debug] relevant={scored[:max_files]}", file=sys.stderr)
    return [p for _, p in scored[:max_files]]


def _obsidian_context(query: str, top_k: int = 3) -> Optional[str]:
    """Live vault search via the tailnet bridge; None if bridge/key missing."""
    try:
        from scripts.obsidian_client import ObsidianClient
        client = ObsidianClient()
        results = client.simple_search(query, top_k=top_k)
    except Exception:
        return None
    if not results:
        return None
    parts = []
    for res in results:
        path = res.get("filename") or res.get("path") or "note"
        snippet = res.get("snippet", "")
        parts.append(f"- {path}\n  {snippet[:200]}")
    return "[Live Vault]\n" + "\n".join(parts)


def _last_run_for(ticket: dict) -> Optional[dict]:
    """Look up the ticket in data/last_run.json (pipeline truth per issue)."""
    path = ROOT / "data" / "last_run.json"
    if not path.exists():
        return None
    td = str(ticket.get("id", ""))
    # GitHub issues are stored by number in last_run.json
    m = re.search(r"#(\d+)$", td)
    issue_num = int(m.group(1)) if m else None
    try:
        runs = json.loads(path.read_text(errors="replace"))
    except Exception:
        return None
    if not isinstance(runs, list):
        return None
    if issue_num is None:
        return None
    hits = [r for r in runs if isinstance(r, dict) and r.get("issue") == issue_num]
    if not hits:
        return None
    hits.sort(key=lambda r: r.get("timestamp", ""))
    return hits[-1]


def _orca_worktrees() -> Optional[list]:
    """List live Orca worktrees for the 'mac' environment if the CLI is paired.

    Returns None if Orca isn't reachable/paired (degrade gracefully).
    """
    cli = ROOT / "scripts" / "orca_cli.sh"
    if not cli.exists():
        return None
    try:
        out = subprocess.run(
            [str(cli), "worktree", "list", "--environment", "mac", "--json"],
            capture_output=True, text=True, timeout=40,
        )
        data = json.loads(out.stdout)
    except Exception:
        return None
    if not data.get("ok"):
        return None
    return data.get("result", {}).get("worktrees", []) or None


def _overlapping_orca(candidate_files: list, worktrees: Optional[list]) -> list:
    """Return Orca worktrees that touch this repo and could collide with edits.

    Conservative by design: any worktree that is a checkout of this repo
    (path hits `school-core` as a directory or workspace root) is treated as an
    overlap risk when the ticket maps to candidate files. Unrelated checkouts
    (e.g. KnowledgeCore) are excluded so the radar stays focused.
    """
    if not worktrees or not candidate_files:
        return []
    repo_wts = []
    for wt in worktrees:
        path = str(wt.get("path", ""))
        branch = wt.get("branch") or wt.get("ref") or ""
        if "school-core" not in path:
            continue
        repo_wts.append({"path": path, "branch": branch})
    return repo_wts


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _guardrails(repo_path: Path, ticket: dict) -> list:
    """Assemble guardrail rules that apply to this ticket's execution."""
    rules = [
        "Read-only by default: no writes outside the worktree/dispatch slice unless the task says otherwise.",
        "Respect the vault privacy allowlist (config/vault_allowlist.yaml): never read Or surface 00-Inbox, 05-Daily, 06-Archive, 01-Projects/Brandon Career.",
        "Track work in beads (`bd update <id>`), not parallel markdown TODO lists.",
    ]
    # Vault allowlist exists → reinforce.
    allow = repo_path / "config" / "vault_allowlist.yaml"
    if allow.exists():
        rules.append("Vault allowlist is enforced by scripts/check_vault_allowlist.py — do not bypass it.")
    # If ticket came from GitHub, don't create external PRs without approval.
    if ticket.get("source") == "github":
        rules.append("This is a live GitHub ticket: do not push / open PRs unless explicitly asked.")
    return rules


def _verification(ticket: dict, relevant: list) -> list:
    """Suggest concrete check commands for this ticket (best-effort)."""
    checks = []
    if ticket.get("source") == "github":
        checks.append("CI green: `gh run list --workflow ci.yml --branch main --limit 3`")
    if relevant:
        paths = " ".join(f"'{p}'" for p in relevant[:5])
        checks.append(f"Focused change leaves no lint/type regressions in: {paths}")
    checks.append("`python3 -m pytest -q -m 'not live'` (project test gate)")
    return checks


def _emit_markdown(ticket: dict, args) -> str:
    """Build the handoff-shaped context block."""
    repo_path = Path(args.repo).resolve()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- Code Paths ---
    tree = _repo_tree()
    relevant = _relevant_files(ticket)
    code_paths = []
    if relevant:
        code_paths.append("Candidate files (keyword-scored against the ticket):")
        for f in relevant:
            code_paths.append(f"- `{f}`")
    else:
        code_paths.append("No candidate files matched by keyword — use the tree below.")
    if tree:
        code_paths.append("")
        code_paths.append("Repo tree (depth 3):")
        code_paths.append("```")
        code_paths.append(tree)
        code_paths.append("```")

    # --- Pre-Dispatch Evidence ---
    evidence = []
    lr = _last_run_for(ticket)
    if lr:
        evidence.append(
            f"- Last pipeline run for this issue: **{lr.get('status')}** "
            f"(agent={lr.get('agent')}, score={lr.get('score')}, "
            f"at {lr.get('timestamp', 'unknown')})"
        )
    else:
        evidence.append("- No prior pipeline run found in data/last_run.json.")

    # Orca worktree radar
    worktrees = _orca_worktrees()
    if worktrees:
        overlap = _overlapping_orca(relevant, worktrees)
        ev = [f"- {wt['path']} ({wt['branch']})" for wt in worktrees]
        evidence.append(f"- **Live Orca worktrees on the Mac** ({len(worktrees)}):")
        evidence.extend(ev)
        if overlap:
            evidence.append("  ⚠️ Overlap candidate — check these worktrees before dispatching:")
            for o in overlap:
                evidence.append(f"  - {o['path']} ({o['branch']})")
    else:
        evidence.append("- Orca worktree radar: not reachable (bridge down or not paired).")

    # Obsidian live vault
    obsidian = _obsidian_context(
        f"{ticket.get('title','')} " + " ".join(relevant[:3]),
        top_k=args.top_k,
    )
    if obsidian:
        evidence.append(f"- **Obsidian (live vault)** context:\n{obsidian}")
    else:
        evidence.append("- Obsidian context: bridge/key not configured — skipped.")

    # Prior solution docs matching the ticket
    sols = _solution_docs(ticket)
    if sols:
        evidence.append("- Prior solution slices referencing this ticket:")
        evidence.extend(f"  - `{s}`" for s in sols)

    evidence_str = "\n".join(evidence)

    guardrails = _guardrails(repo_path, ticket)
    verification = _verification(ticket, relevant)

    def lines(items):
        return "\n".join(f"- {i}" for i in items)

    return f"""# PRIME — {ticket.get('id') or ticket.get('title') or 'UNTITLED'}

<!-- Auto-generated by scripts/prime.py at {now}. Regenerate with:
     python3 scripts/prime.py --ticket '{args.ticket}' {f"--top-k {args.top_k}" if args.top_k != 3 else ""} -->

## Metadata

- **Ticket:** {ticket.get('id', '')}
- **Title:** {ticket.get('title', '')}
- **Source:** {ticket.get('source', '')}
- **State:** {ticket.get('state', '') or 'n/a'}
- **Labels:** {", ".join(ticket.get('labels') or []) or 'n/a'}
- **Author:** {ticket.get('author', '') or 'n/a'}
- **URL:** {ticket.get('url', '') or 'n/a'}
- **Milestone:** {ticket.get('milestone', '') or 'n/a'}
- **Created:** {ticket.get('created_at', '') or 'n/a'}
- **Updated:** {ticket.get('updated_at', '') or 'n/a'}
- **Repo:** {repo_path}

## Task

{ticket.get('body', '').strip() or ticket.get('title', 'NO BODY — title only')}

## Code Paths

{chr(10).join(code_paths)}

## Pre-Dispatch Evidence

{evidence_str}

## Guardrails

{lines(guardrails)}

## Verification

{lines(verification)}
"""


def _solution_docs(ticket: dict) -> list:
    """Find docs/solutions/* slices whose markdown mentions the ticket id."""
    ident = str(ticket.get("id", "")).lower()
    m = re.search(r"#(\d+)$", ident)
    needle = f"#{m.group(1)}" if m else ident
    sol_dir = ROOT / "docs" / "solutions"
    if not sol_dir.is_dir():
        return []
    hits = []
    for f in sorted(sol_dir.glob("*/0*.md")):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if needle in text.lower():
            hits.append(str(f.relative_to(ROOT)))
            if len(hits) >= 5:
                break
    return hits


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="prime",
        description="Emit a ticket-scoped context block for the inner-loop Prime step.",
    )
    ap.add_argument("--ticket", required=True, help=(
        "Ticket reference: GitHub issue (#123, owner/repo#123, URL), a beads id, "
        "or a plan/spec markdown path."))
    ap.add_argument("--repo", default=str(ROOT), help="Repo path (default: this checkout).")
    ap.add_argument("--out", default="", help="Write the block to this file instead of stdout.")
    ap.add_argument("--top-k", type=int, default=3, help="Obsidian search depth (default 3).")
    ap.add_argument("--no-obsidian", action="store_true", help="Skip the live-vault probe.")
    ap.add_argument("--no-orca", action="store_true", help="Skip the Orca worktree radar.")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="Emit the raw result as JSON (for tooling).")
    args = ap.parse_args(argv)

    if args.no_obsidian:
        original = _obsidian_context
        globals()["_obsidian_context"] = lambda *a, **k: None
    if args.no_orca:
        globals()["_orca_worktrees"] = lambda: None

    try:
        ticket = load_ticket(args.ticket)
    except ValueError as exc:
        print(f"[prime] ERROR: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        payload = {
            "ticket": ticket,
            "relevant_files": _relevant_files(ticket),
            "worktrees": _orca_worktrees() or [],
            "last_run": _last_run_for(ticket),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        print(json.dumps(payload, indent=2))
        return 0

    block = _emit_markdown(ticket, args)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(block)
        print(f"[prime] wrote {out}", file=sys.stderr)
    else:
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())