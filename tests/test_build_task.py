"""Tests for _build_task_from_issue format reminder."""

import pytest
import sys
sys.path.insert(0, '/Users/brandonbennett/school-core/src')
from school_core.conductor.tasks import _build_task_from_issue


class TestBuildTaskFromIssue:
    """Format reminder appended to coder-domain task prompts."""

    def test_coder_domain_gets_format_reminder(self):
        issue = {
            "title": "Add installation section to README",
            "description": "## Task\n\nAdd a README section.\n\n## Domain\ndocumentation / easy",
            "domain": "code-implementation",
        }
        task = _build_task_from_issue(issue)
        assert "## Output Format (CRITICAL)" in task
        assert "# <path>" in task
        assert "MUST respond with fenced code blocks" in task

    def test_python_coding_gets_reminder(self):
        issue = {
            "title": "Write a function",
            "description": "Write a function that validates email",
            "domain": "python-coding",
        }
        task = _build_task_from_issue(issue)
        assert "## Output Format (CRITICAL)" in task

    def test_default_domain_gets_reminder(self):
        issue = {
            "title": "Some default task",
            "description": "Do something",
            "domain": "_default",
        }
        task = _build_task_from_issue(issue)
        assert "## Output Format (CRITICAL)" in task

    def test_code_search_domain_no_reminder(self):
        issue = {
            "title": "Search for TODO",
            "description": "Find all TODO comments",
            "domain": "code-search",
        }
        task = _build_task_from_issue(issue)
        # code-search is NOT a coder domain — no format reminder
        assert "## Output Format (CRITICAL)" not in task

    def test_non_code_domain_no_reminder(self):
        issue = {
            "title": "Visual design task",
            "description": "Create a landing page",
            "domain": "visual-design",
        }
        task = _build_task_from_issue(issue)
        # visual-design is NOT in the reminder list
        assert "## Output Format (CRITICAL)" not in task

    def test_task_contains_title_and_description(self):
        issue = {
            "title": "My Title",
            "description": "My Description",
            "domain": "code-implementation",
        }
        task = _build_task_from_issue(issue)
        assert "My Title" in task
        assert "My Description" in task
