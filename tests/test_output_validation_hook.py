"""Tests for output validation hook — TDD RED phase.

The hook validates that a role's output matches its expected format BEFORE
the chain wastes tokens on review and PR creation. Currently, a coder can
output "cat README.md" and the chain flows it through two judges and a PR
attempt before discovering the output was never parseable.

This test file defines the contract. Run with:
    pytest tests/test_output_validation_hook.py -v
"""

import pytest
from director import _validate_role_output


class TestValidateRoleOutputCoder:
    """Coder role must produce a fenced code block with # <path> header."""

    def test_valid_fenced_code_block_passes(self):
        response = "```python\n# hello.txt\nHello World\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is True
        assert result["error"] is None

    def test_shell_command_fails(self):
        response = "cat README.md"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False
        assert "fenced" in result["error"].lower() or "code block" in result["error"].lower()

    def test_prose_without_fence_fails(self):
        response = "I would recommend adding a function that does X."
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False

    def test_relative_path_with_directory_passes(self):
        # src/main.py is a legitimate multi-level path
        response = "```python\n# src/main.py\nx = 1\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is True

    def test_absolute_path_fails(self):
        response = "```python\n# /etc/passwd\nx = 1\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False

    def test_traversal_path_fails(self):
        response = "```python\n# ../etc/passwd\nx = 1\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False

    def test_option_flag_path_fails(self):
        response = "```python\n# -rf\nx = 1\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False

    def test_fence_without_path_header_fails(self):
        response = "```python\nprint('hello')\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False
        assert "path" in result["error"].lower()

    def test_markdown_heading_as_path_fails(self):
        # ## Installation should NOT be treated as a valid path
        response = "```markdown\n## Installation\n\nSome text\n```"
        result = _validate_role_output(response, "coder")
        assert result["valid"] is False

    def test_empty_response_fails(self):
        result = _validate_role_output("", "coder")
        assert result["valid"] is False

    def test_none_response_fails(self):
        result = _validate_role_output(None, "coder")
        assert result["valid"] is False


class TestValidateRoleOutputSearcher:
    """Searcher role produces commands — must contain at least one command-like line."""

    def test_valid_search_command_passes(self):
        response = "rg -n 'TODO' --type py"
        result = _validate_role_output(response, "searcher")
        assert result["valid"] is True

    def test_prose_without_command_fails(self):
        response = "I think you should search for TODO comments in the codebase."
        result = _validate_role_output(response, "searcher")
        assert result["valid"] is False


class TestValidateRoleOutputReviewer:
    """Reviewer role produces findings — must contain structured findings."""

    def test_valid_findings_passes(self):
        response = "- [HIGH] file.py:42 — off-by-one in loop bound"
        result = _validate_role_output(response, "reviewer")
        assert result["valid"] is True

    def test_empty_fails(self):
        result = _validate_role_output("", "reviewer")
        assert result["valid"] is False


class TestValidateRoleOutputExecutor:
    """Executor role produces shell commands — must contain at least one command."""

    def test_valid_command_passes(self):
        response = "git log --oneline -10"
        result = _validate_role_output(response, "executor")
        assert result["valid"] is True

    def test_prose_without_command_fails(self):
        response = "You should check the git history."
        result = _validate_role_output(response, "executor")
        assert result["valid"] is False


class TestValidateRoleOutputBrowser:
    """Browser role produces selectors/actions — must contain a CSS selector or action."""

    def test_valid_selector_passes(self):
        response = "document.querySelector('#submit-btn')"
        result = _validate_role_output(response, "browser")
        assert result["valid"] is True

    def test_prose_without_selector_fails(self):
        response = "Click the submit button."
        result = _validate_role_output(response, "browser")
        assert result["valid"] is False


class TestValidateRoleOutputUnknownRole:
    """Unknown roles skip validation (pass-through)."""

    def test_unknown_role_passes_anything(self):
        response = "anything"
        result = _validate_role_output(response, "unknown_role")
        assert result["valid"] is True
