"""
Tests for U3: PR Creator (pr_creator.py)

Run: python -m pytest tests/test_pr_creator.py -v
"""

import json
from unittest.mock import patch

import pytest

from pr_creator import _slugify, branch_name, create_pr_for_issue


# ── Slugify ────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_basic_title(self):
        assert _slugify("Fix login bug") == "fix-login-bug"

    def test_removes_special_chars(self):
        assert _slugify("Bug: crash @ startup!") == "bug-crash-startup"

    def test_truncates_to_max_len(self):
        long = "a" * 100
        assert len(_slugify(long, max_len=40)) <= 40

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("--hello--") == "hello"

    def test_empty_title_returns_empty(self):
        assert _slugify("") == ""


# ── Branch Name ────────────────────────────────────────────────────────────

class TestBranchName:
    def test_format(self):
        name = branch_name(42, "Fix login bug")
        assert name == "school/issue-42-fix-login-bug"

    def test_with_special_chars(self):
        name = branch_name(100, "Bug: crash @ startup!")
        assert "school/issue-100" in name

    def test_long_title_truncated(self):
        very_long = "Implement a very long feature that just keeps going and " * 10
        name = branch_name(999, very_long)
        assert name.startswith("school/issue-999-")
        assert len(name) <= 80  # "school/issue-999-" (16) + 40 slug + some room


# ── Create PR for Issue (mocked gh CLI) ───────────────────────────────────

class TestCreatePR:
    @patch("pr_creator._gh_command")
    def test_dry_run_returns_fake_url(self, mock_gh):
        issue = {"issue_number": 1, "title": "Test", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('hello')", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test", dry_run=True)
        assert url == "https://github.com/user/test/pull/0"
        mock_gh.assert_not_called()

    @patch("pr_creator._gh_command")
    def test_empty_response_returns_none(self, mock_gh):
        issue = {"issue_number": 1, "title": "Test", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "", "agent": "test"}
        url = create_pr_for_issue(issue, task_result, "user/test")
        assert url is None
        mock_gh.assert_not_called()

    @patch("pr_creator._gh_command")
    def test_branch_creation_failure_returns_none(self, mock_gh):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),  # repo view
            None,  # branch creation fails
        ]
        issue = {"issue_number": 5, "title": "Fix crash", "domain": "debugging", "difficulty": "medium"}
        task_result = {"response": "def fix(): pass", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test")
        assert url is None
        assert mock_gh.call_count == 2

    @patch("pr_creator._gh_command")
    def test_successful_pr_creation(self, mock_gh, tmp_path):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),  # repo view
            '{"ref": "refs/heads/school/issue-10-fix-the-thing"}',  # create ref
            "https://github.com/user/test/pull/42",  # pr create
        ]
        issue = {"issue_number": 10, "title": "Fix the thing", "domain": "debugging", "difficulty": "medium"}
        task_result = {"response": "def fix(): return 42\n", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url == "https://github.com/user/test/pull/42"
        assert mock_gh.call_count == 3

    @patch("pr_creator._gh_command")
    def test_pr_creation_uses_correct_args(self, mock_gh, tmp_path):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),  # repo view
            '{"ref": "refs/heads/school/issue-15-add-feature"}',  # create ref
            "https://github.com/user/test/pull/99",  # pr create
        ]
        issue = {"issue_number": 15, "title": "Add feature", "domain": "code-implementation", "difficulty": "easy"}
        task_result = {"response": "# new feature\nprint('done')", "agent": "owl-alpha"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url == "https://github.com/user/test/pull/99"
        # Verify pr create was called with correct args
        pr_call = mock_gh.call_args_list[2]
        pr_args = pr_call[0][0]
        assert "--repo" in pr_args
        assert "user/test" in pr_args[pr_args.index("--repo") + 1]
        assert "--label" in pr_args
        assert "school-automated" in pr_args[pr_args.index("--label") + 1]

    @patch("pr_creator._gh_command")
    def test_handles_repo_view_failure(self, mock_gh):
        mock_gh.return_value = None  # repo view fails
        issue = {"issue_number": 20, "title": "Broken", "domain": "debugging", "difficulty": "hard"}
        task_result = {"response": "# output", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test")
        assert url is None
