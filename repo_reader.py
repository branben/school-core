"""
repo_reader.py — Clone target repos and extract codebase context for agent prompts.

Provides:
    clone_repo(repo_slug) -> Path      # get or refresh a cached clone
    get_file_tree(repo_path) -> str     # formatted tree of tracked files
    find_relevant_files(repo_path, keywords, max_files=5) -> list[Path]
    build_codebase_context(repo_path, issue_text, max_total_chars=10000) -> str
    cleanup_stale_caches(max_age_hours=1)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from repo_default import default_repo

# Cache root is repo-agnostic: derived from the self-configured default repo slug
# (owner__name) so a clone of sound-royale-ny caches under ~/.cache/<slug>/repos.
# Override with AGENT_SCHOOL_CACHE_DIR if you need an explicit location.
_CACHE_SLUG = os.environ.get("AGENT_SCHOOL_CACHE_DIR") or default_repo().replace("/", "__")
CACHE_DIR = Path.home() / ".cache" / _CACHE_SLUG / "repos"

# Orca registers crew worktrees under ~/orca/workspaces/<repo_as_dir>/ (the dir
# name is the repo slug with "/" -> "__", matching CACHE_DIR's repo subdir).
# Each crew dir carries a `.git` file of the form
# `gitdir: <clone>/.git/worktrees/<name>` — that `.git` file is the durable
# link; the clone-side `worktrees/<name>` admin dir is a deletable derivative
# (this guard exists precisely because rmtree'ing the clone deletes it).
WORKSPACES_DIR = Path(
    os.environ.get("AGENT_SCHOOL_WORKSPACES_DIR") or Path.home() / "orca" / "workspaces"
)
MAX_FILE_CHARS = 2000
MAX_FILES = 5
MAX_TOTAL_CHARS = 10000
STICKY_DIRS_MAX_AGE_HOURS = 1

STOP_WORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
    "her", "was", "one", "our", "out", "has", "have", "been", "some",
    "them", "than", "its", "over", "such", "that", "this", "with",
    "from", "they", "were", "will", "each", "which", "their", "said",
    "what", "would", "make", "like", "just", "into", "could", "your",
    "bug", "fix", "issue", "error", "feature", "add", "implement",
    "support", "please", "help", "needs", "want", "should", "would",
    "when", "where", "how", "why", "who", "what", "doing", "does",
    "didn", "don", "isn", "aren", "won", "hasn", "haven", "can't",
    "cannot", "failed", "failing", "crash", "broken", "working",
    "problem", "wrong", "able", "also", "very", "much", "many",
    "still", "even", "about", "after", "before", "between", "through",
    "during", "without", "within", "along", "among", "around",
}


def _git(repo_path: Path, *args, timeout: int = 30) -> str:
    """Run a git command inside repo_path. Returns stdout or empty string."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, timeout=timeout, check=False, text=True,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _has_live_worktrees(repo_path: Path) -> bool:
    """Return True if any crew worktree is still attached to *repo_path*.

    Crews are dispatched as worktrees of this shared clone (Orca's
    ``worktree create --repo <clone>``). The durable link is NOT the clone's
    ``<repo>/.git/worktrees/<name>`` admin dir — that is a deletable derivative
    that ``shutil.rmtree(repo_path)`` destroys, so checking it there returns
    False the moment a prior re-clone/teardown touched it (and cannot detect an
    already-orphaned crew). The authoritative link is the worktree's OWN
    ``.git`` file, living at ``~/orca/workspaces/<repo_as_dir>/<crew>/.git``,
    whose content is ``gitdir: <clone>/.git/worktrees/<name>``.

    We therefore INVERT the lookup: scan the worktree root for this repo and
    resolve each child's ``gitdir:`` back to *repo_path*. This is repo-scoped
    (the workspace dir is named by repo), robust to the clone-side admin dir
    being deleted (we read the still-present worktree ``.git``), and survives
    even when the registry thinks nothing is active but the dir is still on
    disk. Deleting the clone while any such worktree exists orphans it — the
    crew loses its cwd and can no longer append its status file.
    """
    # repo_path.name is the repo slug with "/" -> "__" (e.g. owner__repo),
    # which is exactly how Orca names the workspace subdir.
    ws_root = WORKSPACES_DIR / repo_path.name
    if not ws_root.is_dir():
        return False
    clone_git = (repo_path / ".git").resolve()
    for child in ws_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        gitfile = child / ".git"
        if not gitfile.is_file():
            continue
        try:
            line = gitfile.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not line.startswith("gitdir:"):
            continue
        target = Path(line[len("gitdir:"):].strip())
        try:
            target.resolve().relative_to(clone_git)
            return True
        except (ValueError, OSError):
            # Points at a different clone (or a dangling/unresolvable link).
            continue
    return False


def clone_repo(repo_slug: str, force_fresh: bool = False) -> Optional[Path]:
    """Get or create a cached clone of a GitHub repo.

    Returns the path to the local clone. By default refreshes a cached clone
    with ``git pull --ff-only``. Pass ``force_fresh=True`` to discard any
    cached clone (which may carry uncommitted/diverged state forward) and do a
    clean depth-1 clone — required for dispatch so a student never starts from
    a contaminated base tree.

    SAFETY (B8 fix): when ``force_fresh=True`` would discard a clone that still
    has live crew worktrees attached, we MUST NOT delete it — that orphans the
    crews (see ``_has_live_worktrees``). In that case force_fresh is downgraded
    to a safe ``git pull --ff-only`` refresh so the worktrees keep a valid
    ``.git`` linkage. The dispatch that requested force_fresh is itself about to
    spawn a crew, so a pristine re-clone is impossible anyway; the refreshed
    base is the safe fallback.

    Args:
        repo_slug: ``owner/repo`` to clone.
        force_fresh: If True, remove the cache dir and re-clone from origin
            — UNLESS live worktrees are attached, in which case refresh instead.

    Returns:
        Path to the clone, or None on failure.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    repo_path = CACHE_DIR / repo_slug.replace("/", "__")

    if force_fresh and repo_path.exists():
        # The shared clone may have live crew worktrees attached (see
        # _has_live_worktrees). rmtree-ing it would orphan them — the crew
        # loses its cwd and can no longer append status. When crews are active
        # we MUST NOT delete the clone. Downgrade force_fresh to a safe refresh
        # (git pull --ff-only) so the worktrees keep a valid .git linkage, and
        # let dispatch proceed from the refreshed base.
        if _has_live_worktrees(repo_path):
            sys.stderr.write(
                f"[repo_reader] force_fresh requested for {repo_slug} but live "
                f"worktrees are attached to {repo_path} — skipping rmtree to "
                f"avoid orphaning active crews; refreshing instead\n"
            )
            _git(repo_path, "pull", "--ff-only")
            return repo_path
        # No live worktrees: discard any cached clone (may carry
        # uncommitted/diverged state) so the student starts from a clean base
        # tree. Clone lands in the stable cache path (not a throwaway temp dir)
        # so it can be reused/swept.
        shutil.rmtree(repo_path, ignore_errors=True)

    if repo_path.exists() and (repo_path / ".git").exists():
        if force_fresh:
            # Stale clone just removed above; fall through to re-clone below.
            pass
        else:
            # Refresh existing clone
            _git(repo_path, "pull", "--ff-only")
            return repo_path

    # Fresh (or force_fresh re-)clone into the stable cache path.
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", f"https://github.com/{repo_slug}.git", str(repo_path)],
            capture_output=True, timeout=120, check=False, text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(f"[repo_reader] Failed to clone {repo_slug}: {result.stderr.strip()[:200]}\n")
            shutil.rmtree(repo_path, ignore_errors=True)
            return None
        return repo_path
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        sys.stderr.write(f"[repo_reader] Clone error for {repo_slug}: {e}\n")
        shutil.rmtree(repo_path, ignore_errors=True)
        return None


def get_file_tree(repo_path: Path, max_depth: int = 3) -> str:
    """Return a formatted tree of tracked files up to max_depth levels."""
    output = _git(repo_path, "ls-tree", "-r", "--name-only", "HEAD")
    if not output:
        return ""

    lines = [line.strip() for line in output.split("\n") if line.strip()]

    # Build tree structure
    tree: dict = {}
    for line in lines:
        parts = line.split("/")
        current = tree
        # Limit depth
        for part in parts[:max_depth]:
            if part not in current:
                current[part] = {}
            current = current[part]

    def render(node: dict, prefix: str = "", is_last: bool = True) -> list[str]:
        items = list(node.keys())
        result = []
        for i, name in enumerate(items):
            is_final = i == len(items) - 1
            connector = "└── " if is_final else "├── "
            result.append(f"{prefix}{connector}{name}")
            if node[name]:  # has children
                extension = "    " if is_final else "│   "
                result.extend(render(node[name], prefix + extension, is_final))
        return result

    return "\n".join(render(tree))


def extract_keywords(text: str, max_keywords: int = 10) -> list[str]:
    """Extract meaningful keywords from issue text for file matching."""
    # Split on non-alphanumeric chars
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", text)
    # Filter: length > 3, not stop words, not common verbs
    keywords = []
    seen = set()
    for w in words:
        wl = w.lower()
        if len(wl) <= 3 or wl in STOP_WORDS or wl in seen:
            continue
        seen.add(wl)
        keywords.append(wl)
        if len(keywords) >= max_keywords:
            break
    return keywords


def find_relevant_files(repo_path: Path, keywords: list[str], max_files: int = MAX_FILES) -> list[Path]:
    """Find source files relevant to the given keywords using grep."""
    if not keywords:
        return []

    # Limit to common source extensions
    output = _git(repo_path, "ls-tree", "-r", "--name-only", "HEAD")
    if not output:
        return []

    all_files = [line.strip() for line in output.split("\n") if line.strip()]
    source_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".rs", ".java", ".sh", ".yaml", ".yml", ".json", ".md"}
    source_files = [
        f for f in all_files
        if Path(f).suffix in source_exts and len(f) < 200
    ]

    # Score files by keyword matches
    scored: list[tuple[int, str]] = []
    for f in source_files:
        score = 0
        # Check filename
        fname = Path(f).stem.lower()
        for kw in keywords:
            if kw in fname:
                score += 3
        # Check content with grep (limit to top 100 files for speed)
        if score == 0 and len(scored) < 100:
            content_output = _git(repo_path, "grep", "-l", "-i", "--", f"{keywords[0]}", f)
            if content_output.strip():
                score += 1
        if score > 0:
            scored.append((score, f))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [repo_path / f for _, f in scored[:max_files]]


def read_file_content(file_path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    """Read file content, truncated to max_chars."""
    try:
        content = file_path.read_text(errors="replace")
    except (OSError, PermissionError):
        return ""

    if len(content) <= max_chars:
        return content

    # Truncate at a function/class boundary if possible
    truncated = content[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars * 0.7:
        truncated = truncated[:last_newline]

    return truncated + f"\n... [truncated, {len(content)} total chars]"


def build_codebase_context(repo_path: Path, issue_text: str) -> str:
    """Build a codebase context block to prepend to an agent prompt.

    Includes file tree + relevant source files matched by issue keywords.
    Returns empty string if repo_path is None or invalid.
    """
    if not repo_path or not repo_path.exists():
        return ""

    tree = get_file_tree(repo_path)
    keywords = extract_keywords(issue_text)
    relevant = find_relevant_files(repo_path, keywords)

    parts = ["## Codebase Context\n"]

    if tree:
        parts.append(f"### File Tree\n```\n{tree}\n```\n")

    total_chars = sum(len(p) for p in parts)
    if relevant:
        parts.append("### Relevant Files\n")
        for f in relevant:
            if total_chars >= MAX_TOTAL_CHARS:
                break
            content = read_file_content(f)
            if content:
                rel_path = f.relative_to(repo_path)
                section = f"#### `{rel_path}`\n```\n{content}\n```\n"
                if total_chars + len(section) > MAX_TOTAL_CHARS:
                    # Truncate to fit
                    remaining = MAX_TOTAL_CHARS - total_chars - len(f"#### `{rel_path}`\n```\n\n```\n")
                    if remaining > 100:
                        section = f"#### `{rel_path}`\n```\n{content[:remaining]}\n```\n"
                    else:
                        break
                parts.append(section)
                total_chars += len(section)

    return "\n".join(parts)


def cleanup_stale_caches(max_age_hours: int = STICKY_DIRS_MAX_AGE_HOURS) -> None:
    """Remove cached repo clones older than max_age_hours."""
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - (max_age_hours * 3600)
    for entry in CACHE_DIR.iterdir():
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            sys.stderr.write(f"[repo_reader] Cleaned stale cache: {entry.name}\n")
