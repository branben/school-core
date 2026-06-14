#!/usr/bin/env python3
"""
pr_creator.py — Create a GitHub PR from a completed task result.

Flow:
  1. Derive branch name from issue number + title slug
  2. Create and switch to branch
  3. Write the task's response to a file
  4. Commit and push
  5. Open a PR using gh CLI

Usage:
    from pr_creator import create_pr_for_issue
    pr_url = create_pr_for_issue(issue, task_result, repo="owner/repo")
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


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


def _gh_command(args: list[str], timeout: int = 30) -> Optional[str]:
    """Run a `gh` CLI command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, timeout=timeout, check=False, text=True,
        )
    except FileNotFoundError:
        sys.stderr.write("[pr_creator] gh CLI not found in PATH\n")
        return None
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[pr_creator] gh command timed out\n")
        return None

    if result.returncode != 0:
        err = result.stderr.strip()[:300]
        sys.stderr.write(f"[pr_creator] gh error (rc={result.returncode}): {err}\n")
        return None

    return result.stdout


def create_pr_for_issue(
    issue: dict,
    task_result: dict,
    repo: str,
    work_dir: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """Create a PR from a completed task result.

    Args:
        issue: Issue dict from github_fetcher (keys: issue_number, title, domain, etc.)
        task_result: Result dict from director.run_task (keys: response, agent, etc.)
        repo: Repository in owner/repo format.
        work_dir: Git working directory. Defaults to CWD.
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

    cwd = work_dir or os.getcwd()

    # 1. Create branch from default
    default_branch_result = _gh_command([
        "repo", "view", repo, "--json", "defaultBranch",
    ])
    if default_branch_result is None:
        base_branch = "main"
    else:
        import json
        try:
            base_branch = json.loads(default_branch_result).get("defaultBranch", "main")
        except (json.JSONDecodeError, KeyError):
            base_branch = "main"

    branch_created = _gh_command([
        "api", f"repos/{repo}/git/refs",
        "--method", "POST",
        "--field", f"ref=refs/heads/{branch}",
        "--field", f"sha=@{base_branch}",
    ])
    if branch_created is None:
        sys.stderr.write(f"[pr_creator] Failed to create branch '{branch}' in {repo}\n")
        return None

    # 2. Determine which path to write to based on domain
    domain = issue.get("domain", "_default")
    output_dir = Path(cwd) / "school-output" / domain / str(num)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "output.py"
    output_file.write_text(response_text)

    # 3. Build PR body
    pr_body = (
        f"## Automated PR for #{num}: {title}\n\n"
        f"_Created by Agent School — agent: {agent}_\n\n"
        f"### Domain\n"
        f"{domain} ({issue.get('difficulty', 'medium')})\n\n"
        f"### Task Output\n"
        f"```python\n{response_text[:800]}\n```\n"
        + ("..." if len(response_text) > 800 else "")
    )

    # 4. Open PR
    title_full = f"[School] {title[:70]}"
    pr_result = _gh_command([
        "pr", "create",
        "--repo", repo,
        "--base", base_branch,
        "--head", branch,
        "--title", title_full,
        "--body", pr_body,
        "--label", "school-automated",
    ])

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
        # Reconstruct a minimal issue dict from the bridge result
        issue = {
            "issue_number": result["issue_number"],
            "title": result.get("title", f"Issue #{result['issue_number']}"),
            "domain": result.get("domain", "_default"),
            "difficulty": result.get("difficulty", "medium"),
        }
        # Note: task_result needs to be reconstructed or passed alongside
        # This is a placeholder for the full flow
        pr_url = create_pr_for_issue(issue, result, repo, work_dir, dry_run)
        if pr_url:
            prs.append({"issue_number": result["issue_number"], "pr_url": pr_url})
    return prs


if __name__ == "__main__":
    import argparse
    import json

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
    task_result = {
        "response": response,
        "agent": args.agent,
    }

    url = create_pr_for_issue(issue, task_result, args.repo, dry_run=args.dry_run)
    if url:
        print(json.dumps({"pr_url": url}))
    else:
        sys.exit(1)
