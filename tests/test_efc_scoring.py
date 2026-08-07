"""Tests for EFC Scoring (U1) — EFCScorer, cost tiers, and compliance helpers."""

import pytest
from scoring import EFCScorer, EFCScore, COST_TIERS, COST_PENALTY_BY_DIFFICULTY


class TestEFCScoreDataclass:
    def test_instantiation(self):
        s = EFCScore(informative=0.7, valid=1.0, retained=0.5, composite=35.0)
        assert s.informative == 0.7
        assert s.valid == 1.0
        assert s.retained == 0.5
        assert s.composite == 35.0


class TestEFCScorer:
    """Test the I×V×R EFC scoring formula."""

    # ── Core formula: V=0 zeroes composite ──

    def test_composite_zero_when_validity_zero(self):
        """Failed task (task_score=0) gives V=0 → composite=0 regardless of response."""
        result = EFCScorer.score(task_score=0, response="some detailed long response here")
        assert result.valid == 0.0
        assert result.composite == 0.0

    def test_composite_zero_on_empty_response(self):
        """Empty response gives R=0 → composite=0 even if score is high."""
        result = EFCScorer.score(task_score=90, response="")
        assert result.retained == 0.0
        assert result.composite == 0.0

    # ── I factor ──

    def test_informative_scales_with_score(self):
        result = EFCScorer.score(task_score=50, response="x" * 3000)
        assert result.informative == 0.5

    def test_informative_caps_at_one(self):
        result = EFCScorer.score(task_score=200, response="x" * 3000)
        assert result.informative == 1.0

    def test_informative_zero_when_score_zero(self):
        result = EFCScorer.score(task_score=0, response="x" * 3000)
        assert result.informative == 0.0

    # ── V factor ──

    def test_valid_one_when_score_positive(self):
        result = EFCScorer.score(task_score=1, response="x" * 3000)
        assert result.valid == 1.0

    def test_valid_zero_when_score_zero(self):
        result = EFCScorer.score(task_score=0, response="x" * 3000)
        assert result.valid == 0.0

    # ── R factor ──

    def test_retained_scales_with_length(self):
        r200 = EFCScorer.score(task_score=100, response="x" * 200)
        r1000 = EFCScorer.score(task_score=100, response="x" * 1000)
        assert r200.retained < r1000.retained

    def test_retained_caps_at_one(self):
        result = EFCScorer.score(task_score=100, response="x" * 5000)
        assert result.retained == 1.0

    def test_retained_zero_on_empty(self):
        result = EFCScorer.score(task_score=100, response="")
        assert result.retained == 0.0

    # ── Composite: I × V × R × 100 ──

    def test_composite_increases_with_longer_response(self):
        short = EFCScorer.score(task_score=70, response="x" * 200).composite
        long_ = EFCScorer.score(task_score=70, response="x" * 1500).composite
        assert short < long_

    def test_composite_saturates_at_one_hundred(self):
        result = EFCScorer.score(task_score=100, response="x" * 3000)
        assert result.composite == 100.0

    def test_composite_typical_code_solution(self):
        """~500 char code solution with good score gets moderate composite."""
        result = EFCScorer.score(task_score=75, response="x" * 500)
        # I=0.75, V=1.0, R=0.167, composite=0.75*1*0.167*100=12.5
        assert result.composite == 12.5
        assert result.informative == 0.75
        assert result.valid == 1.0
        assert round(result.retained, 3) == 0.167

    def test_composite_near_max_on_excellent_response(self):
        """High score + full explanation gets near-max composite."""
        result = EFCScorer.score(task_score=95, response="x" * 2800)
        # I=0.95, V=1.0, R=0.933, composite=0.95*1*0.933*100=88.67
        assert result.composite > 80

    def test_rounding_precision(self):
        """Composite rounded to 2 decimal places."""
        result = EFCScorer.score(task_score=33, response="x" * 777)
        assert isinstance(result.composite, float)
        # Check at most 2 decimal places
        str_val = str(result.composite)
        if "." in str_val:
            decimals = len(str_val.split(".")[1])
            assert decimals <= 2, f"Expected ≤2 decimal places, got {decimals}"


class TestCostTiers:
    def test_free_models_tier_zero(self):
        assert COST_TIERS.get("foundry-smollm3-3b") == 0
        assert COST_TIERS.get("foundry-phi4") == 0
        assert COST_TIERS.get("foundry-coder-0.5b") == 0

    def test_small_local_models_tier_one(self):
        assert COST_TIERS.get("foundry-coder-1.5b") == 1

    def test_mid_local_models_tier_two(self):
        assert COST_TIERS.get("foundry-coder-7b") == 2

    def test_auto_routed_tier_three(self):
        assert COST_TIERS.get("auto/best-free") == 3

    def test_cloud_models_tier_four(self):
        assert COST_TIERS.get("agy/gemini-3.5-flash-high") == 4
        assert COST_TIERS.get("mistral/mistral-small-latest") == 4
        assert COST_TIERS.get("north-coding") == 4

    def test_unknown_model_defaults_to_four(self):
        assert COST_TIERS.get("nonexistent-model", 4) == 4


class TestCostPenalties:
    def test_easy_penalty_is_ten(self):
        assert COST_PENALTY_BY_DIFFICULTY.get("easy") == 10

    def test_medium_penalty_is_five(self):
        assert COST_PENALTY_BY_DIFFICULTY.get("medium") == 5

    def test_hard_penalty_is_zero(self):
        assert COST_PENALTY_BY_DIFFICULTY.get("hard") == 0

    def test_blocker_penalty_is_zero(self):
        assert COST_PENALTY_BY_DIFFICULTY.get("blocker") == 0
