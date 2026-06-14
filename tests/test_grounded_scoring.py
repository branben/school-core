"""Tests for grounded scoring system: GroundedScoreCalculator + EMA integration.

Run: python -m pytest tests/test_grounded_scoring.py -v
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import scoring
from scoring import (
    GroundedScoreCalculator,
    GroundedScore,
    ScoreStore,
)
from execution_scorer import ExecutionScorer
from heuristic_scorer import HeuristicScorer


# ── GroundedScore Dataclass ──────────────────────────────────────────────────

class TestGroundedScore:
    def test_instantiation(self):
        gs = GroundedScore(
            execution_score=80.0,
            heuristic_score=70.0,
            llm_score=90.0,
            combined=78.0,
        )
        assert gs.execution_score == 80.0
        assert gs.heuristic_score == 70.0
        assert gs.llm_score == 90.0
        assert gs.combined == 78.0

    def test_defaults(self):
        gs = GroundedScore(
            execution_score=None,
            heuristic_score=50.0,
            llm_score=None,
            combined=35.0,
        )
        assert gs.details == {}
        assert gs.execution_score is None


# ── GroundedScoreCalculator ──────────────────────────────────────────────────

@pytest.fixture
def calc():
    return GroundedScoreCalculator()


class TestGroundedCalculatorAllTiers:
    def test_all_tiers_present(self, calc):
        code = "def hello():\n    result = 'world'\n    return result\n"
        context = "src/main.py\n"
        result = calc.calculate(code, context, llm_score=80.0)
        assert isinstance(result, GroundedScore)
        assert result.execution_score is None or isinstance(result.execution_score, float)
        assert 0.0 <= result.heuristic_score <= 100.0
        assert result.llm_score == 80.0
        assert 0.0 <= result.combined <= 100.0

    def test_missing_tier1_uses_default(self, calc):
        code = "This is a prose explanation, not code."
        context = ""
        result = calc.calculate(code, context, llm_score=70.0)
        assert result.execution_score is None
        assert 0.0 <= result.combined <= 100.0

    def test_missing_tier3_uses_default(self, calc):
        code = "def foo():\n    return 42\n"
        context = "main.py\n"
        result = calc.calculate(code, context, llm_score=None)
        assert result.llm_score is None
        assert 0.0 <= result.combined <= 100.0

    def test_combined_formula_all_tiers(self, calc):
        code = "a = 1\n"
        context = ""
        with patch.object(ExecutionScorer, 'score', return_value=80.0):
            with patch.object(HeuristicScorer, 'score', return_value=60.0):
                result = calc.calculate(code, context, llm_score=90.0)
        expected = (0.8 * 0.5 + 0.6 * 0.3 + 0.9 * 0.2) * 100.0
        assert abs(result.combined - expected) < 0.01

    def test_combined_formula_missing_exec(self, calc):
        code = "a = 1\n"
        context = ""
        with patch.object(ExecutionScorer, 'score', return_value=None):
            with patch.object(HeuristicScorer, 'score', return_value=60.0):
                result = calc.calculate(code, context, llm_score=90.0)
        expected = (0.5 * 0.5 + 0.6 * 0.3 + 0.9 * 0.2) * 100.0
        assert abs(result.combined - expected) < 0.01

    def test_combined_formula_missing_llm(self, calc):
        code = "a = 1\n"
        context = ""
        with patch.object(ExecutionScorer, 'score', return_value=80.0):
            with patch.object(HeuristicScorer, 'score', return_value=60.0):
                result = calc.calculate(code, context, llm_score=None)
        expected = (0.8 * 0.5 + 0.6 * 0.3 + 0.5 * 0.2) * 100.0
        assert abs(result.combined - expected) < 0.01

    def test_combined_formula_only_heuristic(self, calc):
        code = "not code, just explanation"
        context = ""
        with patch.object(ExecutionScorer, 'score', return_value=None):
            with patch.object(HeuristicScorer, 'score', return_value=50.0):
                result = calc.calculate(code, context, llm_score=None)
        expected = (0.5 * 0.5 + 0.5 * 0.3 + 0.5 * 0.2) * 100.0
        assert abs(result.combined - expected) < 0.01

    def test_weights_sum_to_one(self, calc):
        code = "x = 1\n"
        context = ""
        result = calc.calculate(code, context, llm_score=50.0)
        weights = result.details
        total = weights["exec_weight"] + weights["heuristic_weight"] + weights["llm_weight"]
        assert abs(total - 1.0) < 0.001

    def test_empty_output_scores_zero(self, calc):
        result = calc.calculate("", "", llm_score=0.0)
        assert result.heuristic_score == 0.0
        assert result.combined < 40.0


# ── EMA Integration ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path):
    scores_file = tmp_path / "scores.json"
    store = ScoreStore(str(scores_file))
    store.add_agent("test-agent")
    return store


class TestEMAIntegration:
    def test_grounded_score_feeds_into_update(self, tmp_store):
        grounded_score = 80.0
        new = tmp_store.update_score("test-agent", "_default", grounded_score)
        assert 0.0 <= new <= 100.0

    def test_ema_formula_unchanged(self, tmp_store):
        tmp_store.set_score("test-agent", "_default", 50.0)
        new = tmp_store.update_score("test-agent", "_default", 80.0)
        expected = 50.0 * 0.7 + 80.0 * 0.3
        assert new == pytest.approx(expected)

    def test_multiple_updates_track_via_ema(self, tmp_store):
        tmp_store.update_score("test-agent", "_default", 80.0)
        tmp_store.update_score("test-agent", "_default", 90.0)
        new = tmp_store.update_score("test-agent", "_default", 70.0)
        assert 0.0 <= new <= 100.0

    def test_difficulty_weight_stored(self, tmp_store):
        tmp_store.update_score("test-agent", "python-coding", 80.0, difficulty_weight=1.5)
        assert tmp_store.get_difficulty_weight("test-agent", "python-coding") == 1.5

    def test_difficulty_weight_default(self, tmp_store):
        assert tmp_store.get_difficulty_weight("test-agent", "_default") == 1.0

    def test_difficulty_weight_clamped(self, tmp_store):
        tmp_store.set_difficulty_weight("test-agent", "_default", 5.0)
        assert tmp_store.get_difficulty_weight("test-agent", "_default") == 2.0
        tmp_store.set_difficulty_weight("test-agent", "_default", -1.0)
        assert tmp_store.get_difficulty_weight("test-agent", "_default") == 0.0


# ── Difficulty Weight Persistence ────────────────────────────────────────────

class TestDifficultyWeightPersistence:
    def test_save_and_load_weights(self, tmp_path):
        scores_file = tmp_path / "scores.json"
        store = ScoreStore(str(scores_file))
        store.add_agent("agent-x")
        store.update_score("agent-x", "code-implementation", 75.0, difficulty_weight=1.2)

        store2 = ScoreStore(str(scores_file))
        assert store2.get_difficulty_weight("agent-x", "code-implementation") == 1.2

    def test_weights_in_json(self, tmp_path):
        scores_file = tmp_path / "scores.json"
        store = ScoreStore(str(scores_file))
        store.add_agent("agent-y")
        store.update_score("agent-y", "debugging", 60.0, difficulty_weight=0.8)

        data = json.loads(scores_file.read_text())
        assert f"_difficulty_debugging" in data.get("agent-y", {})

    def test_regular_scores_unaffected(self, tmp_path):
        scores_file = tmp_path / "scores.json"
        store = ScoreStore(str(scores_file))
        store.add_agent("agent-z")
        store.set_score("agent-z", "_default", 50.0)
        store.set_difficulty_weight("agent-z", "_default", 1.1)

        data = json.loads(scores_file.read_text())
        assert data["agent-z"]["_default"] == 50.0
