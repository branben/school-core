"""Tests for cost-aware routing (U2)."""

import pytest
from routing import route_task, _best_cost_aware
from scoring import ScoreStore, COST_TIERS


def _make_store():
    """Create a ScoreStore with models at different cost tiers."""
    store = ScoreStore(file_path="/tmp/test_scores_cost.json")
    store.scores = {
        "foundry-coder-1.5b": {"python-testing": 65},  # tier 1
        "foundry-coder-7b": {"python-testing": 68},    # tier 2
        "auto/best-free": {"python-testing": 70},      # tier 3
        "north-coding": {"python-testing": 72},         # tier 4
        "agy/gemini-3.5-flash-high": {"python-testing": 75},  # tier 4
    }
    return store


class TestBestCostAware:
    """Unit tests for the _best_cost_aware helper function."""

    @pytest.fixture
    def store(self):
        return _make_store()

    def test_easy_task_prefers_cheaper_model(self, store):
        """On easy tasks, a cheaper model with slightly lower score wins over expensive high-score."""
        eligible = list(store.scores.keys())
        # For easy (penalty=10):
        # foundry-coder-1.5b: 65 - (1*10) = 55
        # foundry-coder-7b:   68 - (2*10) = 48
        # auto/best-free:     70 - (3*10) = 40
        # north-coding:       72 - (4*10) = 32
        # agy/gemini:         75 - (4*10) = 35
        # Winner: foundry-coder-1.5b (cheapest, 55)
        best = _best_cost_aware(eligible, store, "python-testing", "easy")
        assert best == "foundry-coder-1.5b"

    def test_hard_task_picks_highest_score(self, store):
        """On hard tasks (penalty=0), the highest-scoring model wins regardless of cost."""
        eligible = list(store.scores.keys())
        best = _best_cost_aware(eligible, store, "python-testing", "hard")
        # No penalty, so agy/gemini (75) should win
        assert best == "agy/gemini-3.5-flash-high"

    def test_blocker_same_as_hard(self, store):
        """Blocker tasks also have no cost penalty."""
        eligible = list(store.scores.keys())
        best = _best_cost_aware(eligible, store, "python-testing", "blocker")
        assert best == "agy/gemini-3.5-flash-high"

    def test_medium_prefers_mid_cost_on_close_scores(self, store):
        """Medium tasks balance cost and score — cheaper mid-tier wins over slightly higher cloud."""
        eligible = list(store.scores.keys())
        # For medium (penalty=5):
        # foundry-coder-1.5b: 65 - (1*5) = 60
        # foundry-coder-7b:   68 - (2*5) = 58
        # auto/best-free:     70 - (3*5) = 55
        # north-coding:       72 - (4*5) = 52
        # agy/gemini:         75 - (4*5) = 55
        # Winner: foundry-coder-1.5b (60)
        best = _best_cost_aware(eligible, store, "python-testing", "medium")
        assert best == "foundry-coder-1.5b"

    def test_tie_prefers_cheaper_model(self, store):
        """On equal cost-adjusted scores, the cheaper model wins."""
        # Free model (tier 0) with score 40 vs cloud model (tier 4) with score 80 on easy
        store.scores["foundry-smollm3-3b"] = {"python-testing": 40}  # tier 0
        store.scores["agy/gemini-3.5-flash-high"] = {"python-testing": 80}  # tier 4
        eligible = ["foundry-smollm3-3b", "agy/gemini-3.5-flash-high"]
        # easy (penalty=10):
        # smollm3: 40 - (0*10) = 40
        # gemini:  80 - (4*10) = 40
        # Tie → cheaper wins
        best = _best_cost_aware(eligible, store, "python-testing", "easy")
        assert best == "foundry-smollm3-3b"

    def test_single_agent_always_selected(self, store):
        """With only one eligible agent, it's selected regardless of cost."""
        best = _best_cost_aware(["north-coding"], store, "python-testing", "easy")
        assert best == "north-coding"

    def test_same_tier_higher_score_wins(self, store):
        """Two agents at the same cost tier: higher score wins."""
        eligible = ["north-coding", "agy/gemini-3.5-flash-high"]  # both tier 4
        # easy (penalty=10):
        # north-coding: 72 - 40 = 32
        # gemini: 75 - 40 = 35
        best = _best_cost_aware(eligible, store, "python-testing", "easy")
        assert best == "agy/gemini-3.5-flash-high"


class TestRouteTaskCostAware:
    """Integration tests: route_task with cost-aware routing."""

    def test_routing_result_includes_cost_tier(self):
        store = _make_store()
        result = route_task(store, "python-testing", "easy")
        assert result.chosen_agent is not None
        assert result.cost_tier is not None
        assert isinstance(result.cost_tier, int)

    def test_force_agent_bypasses_cost(self):
        """Force agent should still report cost tier but not use cost-aware selection."""
        store = _make_store()
        result = route_task(
            store,
            "python-testing",
            "easy",
            force_agent="north-coding",
        )
        assert result.chosen_agent == "north-coding"
        assert result.cost_tier == 4
