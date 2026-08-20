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


def build_pr_body(
    issue: dict,
    response_text: str,
    agent: str,
    review_evidence: Optional[dict] = None,
    verify_result: Optional[dict] = None,
    entire_review: Optional[dict] = None,
    combined_score: float = 0.0,
    artifact_path: Optional[str] = None,
    crew_used: bool = False,
    patch_path: Optional[str] = None,
) -> str:
    """Render the PR body carrying the full acceptance evidence chain (B3).

    Extracted from ``create_pr_for_issue`` so the rendering is directly
    testable: the caller short-circuits on ``dry_run`` before the body is ever
    built, which made the body untestable through the public entry point.

    Two evidence lines matter beyond the raw judge verdicts:

    ACCEPTANCE STATUS — "CTO PASS / COO PASS" is NOT the verdict. Live run
    32319064467 produced ``CTO=PASS (79), COO=PASS (82) -> REJECTED``: a CRITICAL
    finding vetoed it (director.py:618), and since ca400aa an unparseable judge
    also blocks acceptance while still reporting ``verdict=PASS``. A reader
    seeing two PASSes would conclude the opposite of what happened, so the
    verdict has to be stated rather than inferred.

    ARTIFACT PATH — the crew path's entire output is a ``report.md`` whose
    branch/commit/base identity must match the status detail
    (crew_dispatch.py:860-910). Surfacing the path is what lets a human check
    that handshake without leaving GitHub. Omitted entirely on the direct path
    rather than rendered empty, so the PR never advertises an artifact it has no.
    """
    num = issue["issue_number"]
    title = issue.get("title", "")

    cto_verdict = "n/a"
    coo_verdict = "n/a"
    score = combined_score
    accepted: Optional[bool] = None
    if review_evidence:
        cto_verdict = review_evidence.get("cto_verdict", "n/a") or "n/a"
        coo_verdict = review_evidence.get("coo_verdict", "n/a") or "n/a"
        score = review_evidence.get("combined_score", combined_score)
        if "accepted" in review_evidence:
            accepted = bool(review_evidence["accepted"])

    if verify_result:
        v_verdict = verify_result.get("verdict", "n/a") or "n/a"
        v_ran = verify_result.get("ran", 0)
        verify_md = f"{v_verdict} ({v_ran} command(s))"
    else:
        verify_md = "not run"

    if entire_review:
        e_status = entire_review.get("status", "n/a") or "n/a"
        raw_findings = entire_review.get("findings") or []
        # `findings` arrives either as a list of dicts or already counted.
        if isinstance(raw_findings, int):
            e_findings: list = []
            e_count = raw_findings
        else:
            e_findings = list(raw_findings)
            e_count = len(e_findings)
        if e_count:
            blocking = [
                f for f in e_findings
                if isinstance(f, dict) and f.get("severity") in ("CRITICAL", "HIGH")
            ]
            if blocking:
                blocking_md = "\n".join(
                    f"- **{f['severity']}**: `{f.get('file')}:{f.get('line')}` — "
                    f"{f.get('message', '')[:150]}"
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

    if accepted is True:
        acceptance_md = "**ACCEPTED** — both judges passed and no veto fired"
    elif accepted is False:
        acceptance_md = (
            "**REJECTED** — see findings below; note that a CRITICAL finding or "
            "an unparseable judge vetoes acceptance even when both verdicts read PASS"
        )
    else:
        acceptance_md = "not recorded"

    # An inconclusive review must be visible to whoever decides to merge. When
    # _run_adversarial_review crashes it fails closed (review_failed=True) and
    # omits its score, so the review component silently falls back to the
    # execution score — the number looks earned when the check never ran.
    if review_evidence and review_evidence.get("review_failed"):
        acceptance_md += (
            "\n  - ⚠️ **The adversarial review DID NOT RUN** "
            f"(`{str(review_evidence.get('error', 'unknown'))[:160]}`). "
            "Its score contribution fell back to the execution score. "
            "Treat this as UNREVIEWED, not approved."
        )

    body = (
        f"## Automated PR for #{num}: {title}\n\n"
        f"_Created by Agent School — agent: {agent}_\n\n"
        f"### Domain\n"
        f"{issue.get('domain', '_default')} ({issue.get('difficulty', 'medium')})\n\n"
        f"### Task Output\n"
        f"```python\n{response_text[:800]}\n```\n"
        + ("..." if len(response_text) > 800 else "") + "\n\n"
        f"### Acceptance Evidence\n\n"
        f"- **Acceptance:** {acceptance_md}\n"
        # Label the score as QUALITY, not a gate. ReviewResult.score is 100 minus
        # difficulty-weighted findings penalties (adversarial_reviewer.py:102-117)
        # while the verdict is FAIL iff a CRITICAL/HIGH finding exists — so a high
        # score can sit beside a FAIL verdict and be arithmetically correct.
        # Live #341 logged `cto=FAIL coo=FAIL combined=82.0`; unlabelled, a reader
        # draws the opposite conclusion from the verdict.
        f"- **Review:** CTO `{cto_verdict}` / COO `{coo_verdict}` — "
        f"quality {score:.0f}/100 _(quality only; the verdict is the gate)_\n"
        f"- **Verify gate:** {verify_md}\n"
        f"- **Pre-merge check (Entire):** {entire_md}\n"
        f"- **Path:** {'crew' if crew_used else 'direct'}\n"
    )
    # Only on the crew path: a direct-path PR must not advertise an artifact.
    if crew_used and artifact_path:
        body += f"- **Artifact:** `{artifact_path}`\n"
    # A cited commit that does not resolve is worse than no commit at all,
    # because a reader trusts the hash. crew_dispatch probes reachability BEFORE
    # teardown and records a tri-state; surface False loudly and None honestly.
    if crew_used:
        reachable = (review_evidence or {}).get("commit_reachable")
        if reachable is False:
            body += (
                "- ⚠️ **The crew's commit does NOT resolve.** Its branch lived in "
                "a disposable worktree that has been torn down, so the cited SHA "
                "is orphaned and the content of this PR was NOT taken from it. "
                "Do not treat the commit as evidence.\n"
            )
        elif reachable is None:
            body += (
                "- **Commit reachability:** not determined (the probe could not "
                "run) — treat the cited SHA as unverified.\n"
            )
        # The crew's actual diff, captured before teardown (bead school-core-3um).
        # The commit cannot survive its disposable clone, so this patch is the
        # only durable record of what the crew really changed — and it is NOT
        # what this PR's content was built from. Say both plainly.
        # Prefer the dedicated `patch_path` argument (B8 Phase 2: the bridge
        # forwards it from CrewResult); fall back to the legacy review_evidence
        # key so direct renderer callers keep working.
        patch_path = patch_path or (review_evidence or {}).get("patch_path")
        if patch_path:
            body += (
                f"- **Crew diff (captured):** `{patch_path}` — the real change "
                "the crew made, preserved as a patch because its commit does not "
                "survive worktree teardown.\n"
            )
        elif reachable is False:
            body += (
                "- ⚠️ **No crew diff was captured** — the crew's work is not "
                "preserved anywhere. Treat this PR as carrying no crew output.\n"
            )
    return body


def create_pr_for_issue(
    issue: dict,
    task_result: dict,
    repo: str,
    review_evidence: Optional[dict] = None,
    verify_result: Optional[dict] = None,
    entire_review: Optional[dict] = None,
    combined_score: float = 0.0,
    work_dir: Optional[str] = None,
    artifact_path: Optional[str] = None,
    crew_used: bool = False,
    patch_path: Optional[str] = None,
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

    # Filter out any entries where blob creation failed (an infra fault: the
    # GitHub blob POST returned no SHA). If none survive, there is nothing to
    # commit and we must abort — but this is a distinct fault from the
    # empty-change case handled below.
    entries = [e for e in entries if e["sha"]]
    if not entries:
        sys.stderr.write("[pr_creator] blob creation failed — aborting\n")
        return None

    # Diff-emptiness guard: the blob(s) were created, yet every entry is
    # byte-for-byte identical to what is already in the base tree. The
    # resulting commit would change nothing — a no-op PR that looks merged but
    # fixes nothing. This is a DIFFERENT fault from blob creation failing:
    # here the content exists, it just matches the base, so we must catch it
    # on its own terms rather than letting it masquerade as a real change.
    # Fail OPEN when we cannot trust the base tree (unreadable or truncated):
    # if we cannot prove the change is empty, we let it through.
    base_obj = branch_tree if isinstance(branch_tree, dict) else {}
    base_entries = (
        {e["path"]: (e.get("mode"), e.get("type"), e.get("sha"))
         for e in base_obj.get("tree", [])}
        if not base_obj.get("truncated")
        else {}
    )
    if entries and all(
        base_entries.get(e["path"]) == (e.get("mode"), e.get("type"), e["sha"])
        for e in entries
    ):
        sys.stderr.write(
            "[pr_creator] resulting commit would change nothing — every entry "
            "matches the base tree; aborting\n"
        )
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
    pr_body = build_pr_body(
        issue=issue,
        response_text=response_text,
        agent=agent,
        review_evidence=review_evidence,
        verify_result=verify_result,
        entire_review=entire_review,
        combined_score=combined_score,
        artifact_path=artifact_path,
        crew_used=crew_used,
        patch_path=patch_path,
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
