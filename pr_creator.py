#!/usr/bin/env python3
"""
pr_creator.py — Create a GitHub PR from a completed task result.

Flow:
  1. Derive branch name from issue number + title slug
  2. Create branch from default branch's HEAD (or reuse if exists)
  3. Commit the response to the branch via GitHub API (no local checkout)
  4. Open a PR using gh CLI

Usage:
    from pr_creator import create_pr_for_issue
    pr_url = create_pr_for_issue(issue, task_result, repo="owner/repo")
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

API_BASE = "https://api.github.com"


def _slugify(title: str, max_len: int = 40) -> str:
    """Convert a title into a URL-friendly branch slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len].rstrip("-")


def branch_name(issue_number: int, title: str) -> str:
    """Generate a consistent branch name for an issue."""
    slug = _slugify(title)
    return f"school/issue-{issue_number}-{slug}"


def _gh(args: list[str], timeout: int = 30) -> Optional[str]:
    """Run `gh` and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, timeout=timeout, check=False, text=True,
        )
    except FileNotFoundError:
        sys.stderr.write("[pr_creator] gh CLI not found in PATH\n")
        return None
    except subprocess.TimeoutExpired:
        sys.stderr.write("[pr_creator] gh command timed out\n")
        return None

    if result.returncode != 0:
        err = result.stderr.strip()[:300]
        sys.stderr.write(f"[pr_creator] gh error (rc={result.returncode}): {err}\n")
        return None
    return result.stdout


def _gh_api(method: str, path: str, body=None, accept: str = "application/vnd.github+json") -> Optional[dict]:
    """Make a GitHub API call via `gh api`. Returns parsed JSON or None."""
    cmd = ["api", path, f"--method={method}"]
    if body is not None:
        cmd += ["-f", f"json={json.dumps(body)}"]
    cmd += ["--header", f"Accept: {accept}"]
    out = _gh(cmd)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _resolve_base_branch(repo: str) -> str:
    """Return the default branch name for *repo* (falls back to 'main')."""
    out = _gh(["repo", "view", repo, "--json", "defaultBranchRef"])
    if out is None:
        return "main"
    try:
        return json.loads(out).get("defaultBranchRef", {}).get("name", "main")
    except (json.JSONDecodeError, KeyError):
        return "main"


def _resolve_base_sha(repo: str, base_branch: str) -> str:
    """Return the HEAD SHA of *base_branch* in *repo*."""
    out = _gh(
        ["api", f"repos/{repo}/git/refs/heads/{base_branch}", "--jq", ".object.sha"]
    )
    return out.strip() if out else "HEAD"


def _create_or_reuse_branch(repo: str, branch: str, base_sha: str) -> bool:
    """Create *branch* at *base_sha*, or reuse if it already exists."""
    result = _gh_api(
        "POST",
        f"repos/{repo}/git/refs",
        {
            "ref": f"refs/heads/{branch}",
            "sha": base_sha,
        },
    )
    if result is not None:
        return True  # freshly created

    sys.stderr.write(
        f"[pr_creator] branch '{branch}' already exists — reusing\n"
    )
    return True


# ── Tree/commit construction ─────────────────────────────────────────────────


def _parse_response(response_text: str):
    """If the response is a fenced code block with a ``# <path>`` comment,
    return (path, content). Otherwise return None.

    Recognizes::

        ```python
        # hello.txt
        Hello World
        ```
    →::

        ("hello.txt", "Hello World\n")
    """
    m = re.match(
        r"^```\s*(\w*)\s*\n(#\s*([^\s`].*?))\n(.*?)\n```\s*$",
        response_text,
        re.DOTALL,
    )
    if not m:
        return None
    filename = m.group(3).strip()
    if "/" in filename or ".." in filename or filename.startswith("-"):
        return None
    return (filename, m.group(4).strip() + "\n")


def _blobSha(repo: str, content: str, encoding: str = "utf-8") -> Optional[str]:
    """Create a blob in *repo* and return its SHA, or None on failure."""
    result = _gh_api(
        "POST",
        f"repos/{repo}/git/blobs",
        {
            "content": content,
            "encoding": encoding,
        },
    )
    return result.get("sha") if result else None


def _treeSha(repo: str, base_tree: str, entries: list[dict]) -> Optional[str]:
    """Create a tree in *repo* with *entries* appended to *base_tree*.

    Each entry: {"path": "...", "mode": "100644", "type": "blob", "sha": "..."}
    Returns the new tree SHA, or None.
    """
    result = _gh_api(
        "POST",
        f"repos/{repo}/git/trees",
        {
            "base_tree": base_tree,
            "tree": entries,
        },
    )
    return result.get("sha") if result else None


def _commitSha(
    repo: str, message: str, tree_sha: str, parent_sha: str
) -> Optional[str]:
    """Create a commit in *repo* and return its SHA, or None."""
    result = _gh_api(
        "POST",
        f"repos/{repo}/git/commits",
        {
            "message": message,
            "tree": tree_sha,
            "parents": [parent_sha],
        },
    )
    return result.get("sha") if result else None


def _updateRef(repo: str, branch: str, sha: str) -> bool:
    """Move ``refs/heads/{branch}`` to *sha*. Returns True on success."""
    result = _gh_api(
        "PATCH",
        f"repos/{repo}/git/refs/heads/{branch}",
        {"sha": sha, "force": True},
    )
    return result is not None


# ── PR creation ──────────────────────────────────────────────────────────────


def create_pr_for_issue(
    issue: dict,
    task_result: dict,
    repo: str,
    review_evidence: Optional[dict] = None,
    verify_result: Optional[dict] = None,
    entire_review: Optional[dict] = None,
    combined_score: float = 0.0,
    work_dir: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """Create a PR from a completed task result.

    Args:
        issue: Issue dict from github_fetcher (keys: issue_number, title,
            domain, etc.).
        task_result: Result dict from director.run_task (keys: response,
            agent, etc.).
        repo: Repository in owner/repo format.
        review_evidence: Adversarial review dict (cto_verdict, coo_verdict,
            combined_score, findings, etc.). When present, the PR body
            carries the full acceptance evidence chain.
        verify_result: Verify-gate result dict (verdict, ran, exit, etc.).
        entire_review: Entire CLI review dict (status, findings, etc.).
        combined_score: Combined score from the review (used when
            review_evidence is absent).
        work_dir: Ignored (kept for backward compat).
        dry_run: If True, log actions without executing.

    Returns:
        PR URL string if successful, None otherwise.
    """
    num = issue["issue_number"]
    title = issue["title"]
    branch = branch_name(num, title)
    response_text = task_result.get("response", "")
    agent = task_result.get("agent", "unknown")

    if not response_text:
        sys.stderr.write(f"[pr_creator] No response text for issue #{num}\n")
        return None

    if dry_run:
        print(f"[pr_creator] DRY RUN: Would create branch '{branch}' from repo '{repo}'")
        print(f"[pr_creator] DRY RUN: Would write {len(response_text)} chars of output")
        print(f"[pr_creator] DRY RUN: Would PR '{title}'")
        return f"https://github.com/{repo}/pull/0"

    # 1. Resolve base branch + SHA, create (or reuse) the branch.
    base_branch = _resolve_base_branch(repo)
    base_sha = _resolve_base_sha(repo, base_branch)
    if not _create_or_reuse_branch(repo, branch, base_sha):
        return None

    # 2. Build the commit: blob → tree → commit → update ref.
    #    Get the branch's current tree so we append rather than replace.
    branch_ref = _gh_api(
        "GET", f"repos/{repo}/git/refs/heads/{branch}"
    )
    if branch_ref is None:
        sys.stderr.write(
            f"[pr_creator] cannot read branch '{branch}' ref — aborting\n"
        )
        return None
    branch_sha = branch_ref.get("object", {}).get("sha")
    if not branch_sha:
        sys.stderr.write(
            f"[pr_creator] branch '{branch}' has no SHA — aborting\n"
        )
        return None

    # Fetch the tree for the current branch head.
    branch_tree = _gh_api(
        "GET", f"repos/{repo}/git/trees/{branch_sha}"
    )
    base_tree = branch_tree.get("sha") if branch_tree else base_sha

    # Decide what to commit.
    named = _parse_response(response_text)
    if named is not None:
        path, content = named
        entries = [{"path": path, "mode": "100644", "type": "blob", "sha": _blobSha(repo, content)}]
        message = f"school: add {path} for #{num}"
        sys.stderr.write(f"[pr_creator] committing {path} to branch '{branch}'\n")
    else:
        # Artifact-dir mode: write to school-output/<domain>/<num>/output.py
        domain = issue.get("domain", "_default")
        path = f"school-output/{domain}/{num}/output.py"
        content = response_text
        entries = [{"path": path, "mode": "100644", "type": "blob", "sha": _blobSha(repo, content)}]
        message = f"school: task output for #{num} ({domain})"
        sys.stderr.write(f"[pr_creator] committing {path} to branch '{branch}'\n")

    # Filter out any entries where blob creation failed.
    entries = [e for e in entries if e["sha"]]
    if not entries:
        sys.stderr.write("[pr_creator] blob creation failed — aborting\n")
        return None

    tree_sha = _treeSha(repo, base_tree, entries)
    if not tree_sha:
        sys.stderr.write("[pr_creator] tree creation failed — aborting\n")
        return None

    commit_sha = _commitSha(repo, message, tree_sha, branch_sha)
    if not commit_sha:
        sys.stderr.write("[pr_creator] commit creation failed — aborting\n")
        return None

    if not _updateRef(repo, branch, commit_sha):
        sys.stderr.write("[pr_creator] ref update failed — aborting\n")
        return None

    # 3. Build PR body — carry the full acceptance evidence chain so a human
    #    reviewing the PR can see WHY the school accepted this, not just that
    #    it did. Without this, the PR is an evidence-free artifact.
    cto_verdict = "n/a"
    coo_verdict = "n/a"
    score = combined_score
    if review_evidence:
        cto_verdict = review_evidence.get("cto_verdict", "n/a") or "n/a"
        coo_verdict = review_evidence.get("coo_verdict", "n/a") or "n/a"
        score = review_evidence.get("combined_score", combined_score)

    if verify_result:
        v_verdict = verify_result.get("verdict", "n/a") or "n/a"
        v_ran = verify_result.get("ran", 0)
        verify_md = f"{v_verdict} ({v_ran} command(s))"
    else:
        verify_md = "not run"

    if entire_review:
        e_status = entire_review.get("status", "n/a") or "n/a"
        e_findings = entire_review.get("findings") or []
        e_count = len(e_findings)
        if e_count:
            blocking = [f for f in e_findings if f.get("severity") in ("CRITICAL", "HIGH")]
            if blocking:
                blocking_md = "\n".join(
                    f"- **{f['severity']}**: `{f['file']}:{f['line']}` — {f.get('message', '')[:150]}"
                    for f in blocking
                )
                entire_md = (
                    f"{e_status} ({e_count} finding(s))\n"
                    f"### Blocking findings\n{blocking_md}"
                )
            else:
                entire_md = f"{e_status} ({e_count} finding(s), none blocking)"
        else:
            entire_md = f"{e_status} (no findings)"
    else:
        entire_md = "not run"

    pr_body = (
        f"## Automated PR for #{num}: {title}\n\n"
        f"_Created by Agent School — agent: {agent}_\n\n"
        f"### Domain\n"
        f"{issue.get('domain', '_default')} ({issue.get('difficulty', 'medium')})\n\n"
        f"### Task Output\n"
        f"```python\n{response_text[:800]}\n```\n"
        + ("..." if len(response_text) > 800 else "") + "\n\n"
        f"### Acceptance Evidence\n\n"
        f"- **Review:** CTO `{cto_verdict}` / COO `{coo_verdict}` — score {score:.0f}\n"
        f"- **Verify gate:** {verify_md}\n"
        f"- **Pre-merge check (Entire):** {entire_md}\n"
    )

    # 4. Open PR.
    title_full = f"[School] {title[:70]}"
    pr_result = _gh(
        [
            "pr", "create",
            "--repo", repo,
            "--base", base_branch,
            "--head", branch,
            "--title", title_full,
            "--body", pr_body,
            "--label", "school-automated",
        ]
    )

    if pr_result is None:
        sys.stderr.write(f"[pr_creator] Failed to create PR for #{num}\n")
        return None

    pr_url = pr_result.strip()
    print(f"[pr_creator] PR created: {pr_url}")
    return pr_url


def create_prs_for_results(
    results: list[dict],
    repo: str,
    work_dir: Optional[str] = None,
    dry_run: bool = False,
) -> list[dict]:
    """Create PRs for multiple successful task results."""
    prs = []
    for result in results:
        if result.get("status") != "success":
            continue
        issue = {
            "issue_number": result["issue_number"],
            "title": result.get("title", f"Issue #{result['issue_number']}"),
            "domain": result.get("domain", "_default"),
            "difficulty": result.get("difficulty", "medium"),
        }
        pr_url = create_pr_for_issue(issue, result, repo, dry_run=dry_run)
        if pr_url:
            prs.append({"issue_number": result["issue_number"], "pr_url": pr_url})
    return prs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Create PR from task result")
    parser.add_argument("--issue-number", type=int, required=True, help="Issue number")
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--domain", default="_default", help="Task domain")
    parser.add_argument("--difficulty", default="medium", help="Task difficulty")
    parser.add_argument("--response", help="Task response text (or pipe stdin)")
    parser.add_argument("--agent", default="unknown", help="Agent name")
    parser.add_argument("--repo", required=True, help="Repository (owner/repo)")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without executing")
    args = parser.parse_args()

    response = args.response
    if not response and not sys.stdin.isatty():
        response = sys.stdin.read().strip()
    if not response:
        parser.error("--response or piped input required")

    issue = {
        "issue_number": args.issue_number,
        "title": args.title,
        "domain": args.domain,
        "difficulty": args.difficulty,
    }
    task_result = {"response": response, "agent": args.agent}

    url = create_pr_for_issue(issue, task_result, args.repo, dry_run=args.dry_run)
    if url:
        print(json.dumps({"pr_url": url}))
    else:
        sys.exit(1)
