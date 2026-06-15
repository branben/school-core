#!/usr/bin/env python3
"""
github_fetcher.py — Fetch GitHub issues via `gh` CLI, classify, and map to Director domains.

Usage:
    from github_fetcher import fetch_issues, list_repos, load_config

    issues = fetch_issues("owner/repo")
    repos = list_repos()
    config = load_config()
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from triage_classifier import classify_issue

CONFIG_PATH = Path(__file__).parent / "config" / "github.yaml"

# Category → Director domain mapping
DOMAIN_MAP = {
    "bug": "debugging",
    "enhancement": "code-implementation",
}

# Label keywords that signal a specific domain override
DOMAIN_OVERRIDE_KEYWORDS = {
    "python-testing": ["test", "testing", "pytest", "unittest", "coverage"],
    "code-review": ["review", "lint", "audit", "security"],
    "git-operations": ["git", "branch", "merge", "rebase", "clone"],
}

DIFFICULTY_MAP = {
    "easy": "easy",
    "medium": "medium",
    "hard": "hard",
}


def load_config(path: Optional[str] = None) -> dict:
    """Load GitHub config from YAML file. Returns defaults if file missing."""
    cfg_path = Path(path) if path else CONFIG_PATH
    defaults = {
        "repo": "",
        "poll_interval_seconds": 300,
        "labels": ["bug", "enhancement"],
        "difficulty_overrides": {},
        "domain_overrides": {},
    }
    if not cfg_path.exists():
        return defaults
    try:
        import yaml
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        for k in defaults:
            data.setdefault(k, defaults[k])
        return data
    except ImportError:
        sys.stderr.write("[github_fetcher] PyYAML not installed — using defaults\n")
        return defaults
    except Exception as e:
        sys.stderr.write(f"[github_fetcher] Failed to load config: {e}\n")
        return defaults


def _gh_command(args: list[str], timeout: int = 30) -> Optional[str]:
    """Run a `gh` CLI command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, timeout=timeout, check=False, text=True,
        )
    except FileNotFoundError:
        sys.stderr.write("[github_fetcher] gh CLI not found in PATH\n")
        return None
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[github_fetcher] gh command timed out: {' '.join(args[:3])}\n")
        return None

    if result.returncode != 0:
        err = result.stderr.strip()[:200]
        if "not authenticated" in err.lower() or "auth" in err.lower():
            sys.stderr.write(f"[github_fetcher] gh not authenticated: {err}\n")
        else:
            sys.stderr.write(f"[github_fetcher] gh error (rc={result.returncode}): {err}\n")
        return None

    return result.stdout


def _map_domain(category: str, labels: list[str], title: str) -> str:
    """Map a triage category + labels to a Director domain."""
    label_names = [l.lower() for l in labels]
    title_l = title.lower()

    # Check for domain overrides based on keywords in labels/title
    for domain, keywords in DOMAIN_OVERRIDE_KEYWORDS.items():
        for kw in keywords:
            if any(kw in l for l in label_names) or kw in title_l:
                return domain

    # Default mapping from category
    return DOMAIN_MAP.get(category, "_default")


def _map_difficulty(labels: list[str], config: dict) -> str:
    """Determine difficulty from labels and config overrides."""
    label_names = [l.lower() for l in labels]
    overrides = config.get("difficulty_overrides", {})
    for label in label_names:
        if label in overrides:
            mapped = DIFFICULTY_MAP.get(overrides[label])
            if mapped:
                return mapped
    return "medium"


def fetch_issues(repo: str, labels: Optional[list[str]] = None) -> list[dict]:
    """Fetch open issues from a GitHub repo, classify, and return actionable items.

    Returns list of dicts with keys:
        issue_number, title, body, domain, difficulty, prompt, category, state
    """
    args = ["issue", "list", "--repo", repo, "--state", "open",
            "--json", "number,title,labels,body", "--limit", "50"]

    if labels:
        for label in labels:
            args.extend(["--label", label])

    stdout = _gh_command(args)
    if stdout is None:
        return []

    try:
        raw_issues = json.loads(stdout)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[github_fetcher] Failed to parse gh output: {e}\n")
        return []

    config = load_config()
    domain_overrides = config.get("domain_overrides", {})
    results = []

    for issue in raw_issues:
        number = issue.get("number", 0)
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        gh_labels = issue.get("labels", [])
        label_names = [l.get("name", "") for l in gh_labels] if isinstance(gh_labels, list) else []

        category, state = classify_issue(title, label_names, body)

        if state != "ready-for-agent":
            continue

        # Domain mapping: config overrides > label-based > category default
        domain = None
        for override_label, override_domain in domain_overrides.items():
            if any(override_label in l.lower() for l in label_names):
                domain = override_domain
                break
        if domain is None:
            domain = _map_domain(category, label_names, title)

        difficulty = _map_difficulty(label_names, config)
        prompt = f"{title}\n\n{body}"

        results.append({
            "issue_number": number,
            "title": title,
            "body": body,
            "domain": domain,
            "difficulty": difficulty,
            "prompt": prompt,
            "category": category,
            "state": state,
        })

    return results


def list_repos() -> list[str]:
    """List available GitHub repositories via `gh repo list`."""
    stdout = _gh_command(["repo", "list", "--limit", "50", "--json", "nameWithOwner"])
    if stdout is None:
        return []
    try:
        repos = json.loads(stdout)
        return [r.get("nameWithOwner", "") for r in repos if r.get("nameWithOwner")]
    except json.JSONDecodeError:
        return []


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GitHub Issue Fetcher")
    parser.add_argument("repo", nargs="?", help="Repository (owner/repo)")
    parser.add_argument("--list-repos", action="store_true", help="List available repos")
    parser.add_argument("--labels", help="Comma-separated label filter")
    args = parser.parse_args()

    if args.list_repos:
        for r in list_repos():
            print(r)
        sys.exit(0)

    if not args.repo:
        parser.print_help()
        sys.exit(1)

    label_filter = args.labels.split(",") if args.labels else None
    issues = fetch_issues(args.repo, label_filter)
    if not issues:
        print("No actionable issues found.")
        sys.exit(0)

    print(f"Found {len(issues)} actionable issue(s):")
    for i in issues:
        print(f"  #{i['issue_number']} [{i['domain']}/{i['difficulty']}] {i['title'][:70]}")
