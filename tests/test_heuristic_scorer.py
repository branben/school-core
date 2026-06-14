"""Tests for heuristic_scorer.py (Tier 2).

Run: python -m pytest tests/test_heuristic_scorer.py -v
"""

import pytest

from heuristic_scorer import HeuristicScorer


@pytest.fixture
def scorer():
    return HeuristicScorer()


class TestHeuristicScorerGrounding:
    def test_grounded_output_scores_high(self, scorer):
        context = "Files: src/main.py src/utils.py README.md"
        output = "I modified src/main.py and updated src/utils.py for this fix."
        result = scorer.score(output, context)
        assert result > 50.0

    def test_ungrounded_output_scores_lower(self, scorer):
        context = "Files: src/main.py src/utils.py"
        output = "I modified foo.py and bar.js for this fix."
        result = scorer.score(output, context)
        assert result < 70.0

    def test_no_context_returns_neutral(self, scorer):
        output = "Here is my solution: import os\nprint(os.getcwd())"
        result = scorer.score(output, "")
        assert 0.0 <= result <= 100.0

    def test_empty_output_returns_zero(self, scorer):
        result = scorer.score("", "")
        assert result == 0.0


class TestHeuristicScorerSyntax:
    def test_valid_syntax_scores_bonus(self, scorer):
        code = "def hello():\n    return 'world'"
        context = ""
        base = scorer._score_syntax(code)
        assert base == 100.0

    def test_invalid_syntax_scores_zero(self, scorer):
        code = "def hello(\n    return"
        base = scorer._score_syntax(code)
        assert base == 0.0


class TestHeuristicScorerComplexity:
    def test_short_output_scores_full(self, scorer):
        code = "x = 1\ny = 2\nz = x + y"
        assert scorer._score_complexity(code) == 100.0

    def test_very_long_output_scores_low(self, scorer):
        code = "\n".join([f"line_{i} = {i}" for i in range(600)])
        score = scorer._score_complexity(code)
        assert score < 100.0

    def test_500_lines_is_full_score(self, scorer):
        code = "\n".join([f"x_{i} = {i}" for i in range(500)])
        assert scorer._score_complexity(code) == 100.0

    def test_1000_lines_is_penalized(self, scorer):
        code = "\n".join([f"x_{i} = {i}" for i in range(1000)])
        score = scorer._score_complexity(code)
        assert score < 50.0


class TestHeuristicScorerWeights:
    def test_score_always_in_range(self, scorer):
        outputs = [
            "",
            "short",
            "def foo(): pass",
            "x" * 10000,
            "import src.main\nfrom src.utils import helper",
        ]
        for output in outputs:
            result = scorer.score(output, "src/main.py src/utils.py")
            assert 0.0 <= result <= 100.0, f"Score {result} out of range for: {output[:50]}"

    def test_perfect_output_near_max(self, scorer):
        context = "module.py utils.py"
        code = (
            "import module\n"
            "from utils import helper\n"
            "def solve():\n"
            "    return helper(module.data)\n"
        )
        result = scorer.score(code, context)
        assert result > 60.0
