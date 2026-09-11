"""GitHub adapter — concrete implementation of GitHubAdapter.

Wraps the `gh` CLI and GitHub API behind the GitHubAdapter interface.
Uses github_fetcher.py for issue fetching and pr_creator.py for PR creation.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from adapters.base import GitHubAdapter, GitHubError


class GitHubCLIAdapter(GitHubAdapter):
    """Concrete GitHub adapter that shells out to the `gh` CLI.

    This adapter wraps the existing ``github_fetcher.py`` functions so that
    callers can depend on the adapter abstraction rather than importing
    the concrete functions directly.
    """

    def __init__(self, timeout: int = 30):
        self._timeout = timeout

    def _gh_command(self, args: list[str], timeout: int | None = None) -> Optional[str]:
        """Run a `gh` CLI command and return stdout, or None on failure."""
        try:
            result = subprocess.run(
                ["gh"] + args,
                capture_output=True,
                timeout=timeout or self._timeout,
                check=False,
                text=True,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return None

        if result.returncode != 0:
            return None
        return result.stdout

    def fetch_issues(
        self,
        repo: str,
        labels: Optional[list[str]] = None,
    ) -> list[dict]:
        """Fetch open issues from a GitHub repo, classify, and return actionable items."""
        try:
            from github_fetcher import fetch_issues
            return fetch_issues(repo, labels)
        except Exception as e:
            raise GitHubError(f"Failed to fetch issues from {repo}: {e}") from e

    def fetch_single_issue(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> Optional[dict]:
        """Fetch one GitHub issue by number."""
        try:
            from github_fetcher import fetch_single_issue
            return fetch_single_issue(owner, repo, number)
        except Exception as e:
            raise GitHubError(f"Failed to fetch issue {owner}/{repo}#{number}: {e}") from e

    def create_pr(
        self,
        repo: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> Optional[str]:
        """Create a PR and return the PR URL."""
        try:
            from pr_creator import create_pr_for_issue
            # This is a simplified wrapper — full implementation would use
            # the lower-level functions in pr_creator.py
            result = self._gh_command([
                "pr", "create",
                "--repo", repo,
                "--base", base_branch,
                "--head", branch,
                "--title", title,
                "--body", body,
            ])
            return result.strip() if result else None
        except Exception as e:
            raise GitHubError(f"Failed to create PR: {e}") from e
