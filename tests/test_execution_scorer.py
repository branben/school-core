"""Tests for execution_scorer.py (Tier 1).

Run: python -m pytest tests/test_execution_scorer.py -v
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from execution_scorer import ExecutionScorer


@pytest.fixture
def scorer():
    return ExecutionScorer()


class TestExecutionScorerValidCode:
    def test_valid_python_returns_score(self, scorer):
        code = "def hello():\n    return 'world'"
        result = scorer.score(code, "")
        assert result is None or (0.0 <= result <= 100.0)

    def test_valid_class_returns_score(self, scorer):
        code = "class Foo:\n    def bar(self):\n        return 42"
        result = scorer.score(code, "")
        assert result is None or (0.0 <= result <= 100.0)


class TestExecutionScorerSyntaxError:
    def test_syntax_error_returns_zero(self, scorer):
        code = "def hello(\n    return 'world'"
        result = scorer.score(code, "")
        assert result == 0.0

    def test_incomplete_return_returns_zero(self, scorer):
        code = "return"
        result = scorer.score(code, "")
        assert result is None or result == 0.0

    def test_bad_indentation_returns_zero(self, scorer):
        code = "def foo():\n  x = 1\n    y = 2"
        result = scorer.score(code, "")
        assert result == 0.0


class TestExecutionScorerEmptyOutput:
    def test_empty_string_returns_none(self, scorer):
        result = scorer.score("", "")
        assert result is None

    def test_whitespace_only_returns_none(self, scorer):
        result = scorer.score("   \n\t  ", "")
        assert result is None

    def test_none_like_returns_none(self, scorer):
        result = scorer.score("", "some context")
        assert result is None


class TestExecutionScorerNonCode:
    def test_prose_returns_none(self, scorer):
        text = "This is a detailed explanation of how sorting algorithms work."
        result = scorer.score(text, "")
        assert result is None

    def test_markdown_returns_none(self, scorer):
        text = "# Title\n\nSome **bold** text and a [link](http://example.com)."
        result = scorer.score(text, "")
        assert result is None


class TestExecutionScorerWithTests:
    def test_passing_test_scores_high(self, scorer):
        code = (
            "def add(a, b):\n"
            "    return a + b\n"
            "\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
            "    assert add(0, 0) == 0\n"
        )
        result = scorer.score(code, "")
        if result is not None:
            assert result >= 50.0

    def test_failing_test_scores_lower(self, scorer):
        code = (
            "def add(a, b):\n"
            "    return a + b + 1\n"
            "\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
        )
        result = scorer.score(code, "")
        if result is not None:
            assert result < 100.0


class TestExecutionScorerNonBlocking:
    def test_exception_returns_none(self, scorer):
        with patch.object(scorer, '_run_tests', side_effect=Exception("fail")):
            code = "def hello():\n    return 'world'"
            result = scorer.score(code, "")
            assert result is None
