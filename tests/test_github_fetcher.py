"""
Tests for U1: GitHub Issue Fetcher (github_fetcher.py)

Run: python -m pytest tests/test_github_fetcher.py -v
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from github_fetcher import (
    fetch_issues,
    load_config,
    _map_domain,
    _map_difficulty,
    DOMAIN_MAP,
)


# ── Config Tests ──────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        """Returns default values when config file does not exist."""
        fake = str(tmp_path / "nonexistent.yaml")
        cfg = load_config(fake)
        assert cfg["repo"] == ""
        assert cfg["poll_interval_seconds"] == 300
        assert cfg["labels"] == ["bug", "enhancement"]

    def test_loads_yaml_file(self, tmp_path):
        """Parses fields from a real YAML file."""
        cfg_file = tmp_path / "github.yaml"
        cfg_file.write_text("repo: user/test\npoll_interval_seconds: 600\nlabels:\n  - bug\n")
        cfg = load_config(str(cfg_file))
        assert cfg["repo"] == "user/test"
        assert cfg["poll_interval_seconds"] == 600
        assert cfg["labels"] == ["bug"]

    def test_fills_missing_keys_with_defaults(self, tmp_path):
        """Partial config fills missing fields with defaults."""
        cfg_file = tmp_path / "github.yaml"
        cfg_file.write_text("repo: user/test\n")
        cfg = load_config(str(cfg_file))
        assert cfg["repo"] == "user/test"
        assert cfg["poll_interval_seconds"] == 300  # default
        assert cfg["labels"] == ["bug", "enhancement"]  # default

    def test_resolves_self_sentinel(self, tmp_path, monkeypatch):
        """The reserved '__self__' value resolves to the checkout's default repo."""
        monkeypatch.setenv("AGENT_SCHOOL_REPO", "owner/resolved-repo")
        cfg_file = tmp_path / "github.yaml"
        cfg_file.write_text(
            "repo: __self__\n"
            "orchestrator_repo: __self__\n"
            "target_repos:\n"
            "  - slug: branben/sound-royale-ny\n"
            "  - slug: __self__\n"
        )
        cfg = load_config(str(cfg_file))
        assert cfg["repo"] == "owner/resolved-repo"
        assert cfg["orchestrator_repo"] == "owner/resolved-repo"
        slugs = [t.get("slug") for t in cfg["target_repos"]]
        assert slugs == ["branben/sound-royale-ny", "owner/resolved-repo"]


# ── Domain Mapping Tests ──────────────────────────────────────────────────

class TestDomainMapping:
    def test_bug_maps_to_debugging(self):
        assert _map_domain("bug", [], "") == "debugging"

    def test_enhancement_maps_to_code_implementation(self):
        assert _map_domain("enhancement", [], "") == "code-implementation"

    def test_test_label_override(self):
        """Label with 'test' keyword overrides default domain."""
        assert _map_domain("enhancement", ["test"], "") == "python-testing"

    def test_review_label_override(self):
        assert _map_domain("bug", ["code-review"], "") == "code-review"

    def test_git_label_override(self):
        assert _map_domain("enhancement", ["git"], "") == "git-operations"

    def test_unknown_category_defaults_to_default(self):
        assert _map_domain("unknown", [], "") == "_default"

    def test_label_override_in_title(self):
        """Security keyword in title triggers code-review domain."""
        assert _map_domain("bug", ["bug"], "this is a security issue") == "code-review"


# ── Difficulty Mapping Tests ──────────────────────────────────────────────

class TestDifficultyMapping:
    def test_defaults_to_medium(self):
        assert _map_difficulty([], {"difficulty_overrides": {}}) == "medium"

    def test_label_override(self):
        cfg = {"difficulty_overrides": {"p0": "hard"}}
        assert _map_difficulty(["p0"], cfg) == "hard"

    def test_label_override_medium(self):
        cfg = {"difficulty_overrides": {"p1": "medium"}}
        assert _map_difficulty(["p1"], cfg) == "medium"

    def test_override_takes_label_presence(self):
        cfg = {"difficulty_overrides": {"security": "hard"}}
        assert _map_difficulty(["security", "bug"], cfg) == "hard"

    def test_unknown_label_falls_back(self):
        cfg = {"difficulty_overrides": {"p0": "hard"}}
        assert _map_difficulty(["p1"], cfg) == "medium"


# ── Fetch Issues (mocked gh CLI) ──────────────────────────────────────────

class TestFetchIssues:
    @patch("github_fetcher._gh_command")
    def test_returns_actionable_issues(self, mock_gh):
        """Only 'ready-for-agent' issues are returned."""
        mock_gh.return_value = json.dumps([
            {
                "number": 1,
                "title": "Fix login bug",
                "body": "Detailed bug report with reproduction steps" * 15,
                "labels": [{"name": "T-bug"}, {"name": "P1"}],
            },
            {
                "number": 2,
                "title": "Add feature",
                "body": "Feature description with requirements",
                "labels": [{"name": "enhancement"}],
            },
        ])
        issues = fetch_issues("user/test")
        assert len(issues) == 2
        assert issues[0]["issue_number"] == 1
        assert issues[0]["domain"] == "debugging"
        assert issues[1]["issue_number"] == 2
        assert issues[1]["domain"] == "code-implementation"

    @patch("github_fetcher._gh_command")
    def test_filters_non_actionable(self, mock_gh):
        """Issues with state != 'ready-for-agent' are excluded."""
        mock_gh.return_value = json.dumps([
            {
                "number": 3,
                "title": "Vague report",
                "body": "",
                "labels": [{"name": "bug"}],
            },
        ])
        issues = fetch_issues("user/test")
        # Single bug label + empty body → needs-triage, not ready-for-agent
        assert len(issues) == 0

    @patch("github_fetcher._gh_command")
    def test_handles_empty_response(self, mock_gh):
        """Returns empty list when no issues match filters."""
        mock_gh.return_value = json.dumps([])
        assert fetch_issues("user/test") == []

    @patch("github_fetcher._gh_command")
    def test_handles_gh_failure(self, mock_gh):
        """Returns empty list when gh CLI call fails."""
        mock_gh.return_value = None
        assert fetch_issues("user/test") == []

    @patch("github_fetcher._gh_command")
    def test_passes_label_filter(self, mock_gh):
        """Label filter is forwarded to gh CLI args."""
        mock_gh.return_value = json.dumps([])
        fetch_issues("user/test", labels=["bug"])
        args = mock_gh.call_args[0][0]
        assert "--label" in args
        assert "bug" in args[args.index("--label") + 1]

    @patch("github_fetcher._gh_command")
    def test_prompt_includes_title_and_body(self, mock_gh):
        """The prompt field contains both title and body."""
        mock_gh.return_value = json.dumps([
            {
                "number": 4,
                "title": "Fix crash",
                "body": "Detailed crash report with stack trace and reproduction" * 15,
                "labels": [{"name": "T-bug"}, {"name": "P1"}],
            },
        ])
        issues = fetch_issues("user/test")
        assert len(issues) == 1
        assert "Fix crash" in issues[0]["prompt"]
        assert "stack trace" in issues[0]["prompt"]
