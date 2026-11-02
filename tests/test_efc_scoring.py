"""Tests for EFC Scoring (U1) — EFCScorer, cost tiers, and compliance helpers."""

import pytest
from src.core.scoring import EFCScorer, EFCScore, COST_TIERS, COST_PENALTY_BY_DIFFICULTY, SEED_AGENTS
import src.core.leaderboard as leaderboard
class TestComboPricing:
    def test_codestral_has_its_own_score_and_cost_entry(self):
        combo = "mistral/codestral-latest"
        assert combo in SEED_AGENTS
        assert combo in COST_TIERS
        assert combo in leaderboard.SEED_SCORES
        assert leaderboard.AGENT_NAMES[combo] == "Codestral"

    def test_historical_gemini_entry_is_preserved(self):
        assert "gemini-3-flash-preview" in SEED_AGENTS
        assert "gemini-3-flash-preview" in COST_TIERS
        assert "gemini-3-flash-preview" in leaderboard.SEED_SCORES


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


# ---------------------------------------------------------------------------
# Leaderboard helpers — previously 11.9% coverage
# ---------------------------------------------------------------------------

class TestGateForScore:
    def test_none_below_easy(self):
        # GATES["easy"] == 0, so score 0 maps to "easy" by the current rule.
        assert leaderboard.gate_for_score(-1) == "none"
        assert leaderboard.gate_for_score(0) == "easy"

    def test_easy(self):
        assert leaderboard.gate_for_score(1) == "easy"
        assert leaderboard.gate_for_score(24.9) == "easy"

    def test_medium(self):
        assert leaderboard.gate_for_score(25) == "medium"
        assert leaderboard.gate_for_score(49.9) == "medium"

    def test_hard(self):
        assert leaderboard.gate_for_score(50) == "hard"
        assert leaderboard.gate_for_score(74.9) == "hard"

    def test_diploma(self):
        assert leaderboard.gate_for_score(75) == "diploma"
        assert leaderboard.gate_for_score(100) == "diploma"


class TestAgentTier:
    def test_foundry_is_local(self):
        assert leaderboard.agent_tier("foundry-coder-7b") == ("local", "#22c55e")

    def test_openhands_is_a2a(self):
        assert leaderboard.agent_tier("openhands") == ("a2a", "#5b8def")

    def test_cloud_fallback(self):
        assert leaderboard.agent_tier("gemini-3-flash-preview") == ("cloud", "#a855f7")


class TestAgentRole:
    def test_diploma_is_faculty(self):
        assert leaderboard.agent_role("any", 75) == "Faculty"

    def test_hard_is_teacher(self):
        assert leaderboard.agent_role("any", 50) == "Teacher"

    def test_medium_is_senior_student(self):
        assert leaderboard.agent_role("any", 25) == "Senior Student"

    def test_below_medium_is_student(self):
        assert leaderboard.agent_role("any", 24) == "Student"


class TestLoadScores:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(leaderboard, "SCORES_PATH", tmp_path / "missing.json")
        assert leaderboard.load_scores() == {}

    def test_valid_json_returns_dict(self, tmp_path, monkeypatch):
        p = tmp_path / "scores.json"
        p.write_text('{"agent-a": {"_default": 10}}')
        monkeypatch.setattr(leaderboard, "SCORES_PATH", p)
        assert leaderboard.load_scores() == {"agent-a": {"_default": 10}}


class TestLoadTrajectories:
    def test_missing_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(leaderboard, "TRAJECTORY_DIR", tmp_path / "nope")
        assert leaderboard.load_trajectories() == []

    def test_skips_malformed_json(self, tmp_path, monkeypatch):
        d = tmp_path / "traj"
        d.mkdir()
        (d / "bad.json").write_text("not json")
        (d / "good.json").write_text('{"timestamp":"2026-01-01","agent":"a","domain":"d","new_score":1}')
        monkeypatch.setattr(leaderboard, "TRAJECTORY_DIR", d)
        out = leaderboard.load_trajectories()
        assert len(out) == 1
        assert out[0]["agent"] == "a"

    def test_sorted_by_filename(self, tmp_path, monkeypatch):
        d = tmp_path / "traj"
        d.mkdir()
        (d / "b.json").write_text('{"timestamp":"2026-01-02","agent":"b","domain":"d","new_score":1}')
        (d / "a.json").write_text('{"timestamp":"2026-01-01","agent":"a","domain":"d","new_score":1}')
        monkeypatch.setattr(leaderboard, "TRAJECTORY_DIR", d)
        out = leaderboard.load_trajectories()
        assert [t["agent"] for t in out] == ["a", "b"]


class TestComputeScoreHistory:
    def test_seed_scores_present(self):
        hist = leaderboard.compute_score_history([])
        assert "foundry-coder-7b" in hist
        assert ("seed", 25.0) in hist["foundry-coder-7b"]["_default"]

    def test_trajectories_appended(self):
        trajs = [{"agent": "a", "domain": "_default", "new_score": 40, "timestamp": "2026-01-01"}]
        hist = leaderboard.compute_score_history(trajs)
        assert ("2026-01-01", 40.0) in hist["a"]["_default"]

    def test_sorted_by_timestamp(self):
        trajs = [
            {"agent": "a", "domain": "_default", "new_score": 10, "timestamp": "2026-01-02"},
            {"agent": "a", "domain": "_default", "new_score": 20, "timestamp": "2026-01-01"},
        ]
        hist = leaderboard.compute_score_history(trajs)
        pts = hist["a"]["_default"]
        assert pts[0][0] == "2026-01-01"
        assert pts[1][0] == "2026-01-02"


class TestComputeStats:
    def test_empty_inputs(self):
        stats = leaderboard.compute_stats([], {})
        assert stats["total"] == 0
        assert stats["success_rate"] == 0
        assert stats["gate_crossings"] == []

    def test_success_rate(self):
        trajs = [
            {"domain": "d", "error": None, "old_score": 10, "new_score": 20},
            {"domain": "d", "error": "boom", "old_score": 10, "new_score": 5},
        ]
        stats = leaderboard.compute_stats(trajs, {"a": {"_default": 20}})
        assert stats["successes"] == 1
        assert stats["failures"] == 1
        assert stats["success_rate"] == 50.0

    def test_gate_crossings_detected(self):
        trajs = [
            {"agent": "a", "domain": "_default", "old_score": 20, "new_score": 30, "timestamp": "2026-01-01"},
            {"agent": "a", "domain": "_default", "old_score": 30, "new_score": 60, "timestamp": "2026-01-02"},
        ]
        stats = leaderboard.compute_stats(trajs, {"a": {"_default": 60}})
        crossings = stats["gate_crossings"]
        assert len(crossings) == 2
        assert crossings[0]["from_gate"] == "easy"
        assert crossings[0]["to_gate"] == "medium"
        assert crossings[1]["to_gate"] == "hard"

    def test_session_agents_detected(self):
        stats = leaderboard.compute_stats([], {"ses_123": {"_default": 1}, "real-a": {"_default": 1}})
        assert "ses_123" in stats["session_agents"]
        assert "real-a" not in stats["session_agents"]


class TestBuildSparkline:
    def test_less_than_two_points_returns_placeholder(self):
        assert '<span style="color:var(--text-dim);font-size:11px">—</span>' == leaderboard.build_sparkline([])
        assert '<span style="color:var(--text-dim);font-size:11px">—</span>' == leaderboard.build_sparkline([(1, 10)])

    def test_produces_svg_path(self):
        pts = [("t1", 10), ("t2", 20), ("t3", 15)]
        svg = leaderboard.build_sparkline(pts)
        assert svg.startswith("<svg")
        assert "<path" in svg
        assert "M" in svg

    def test_color_matches_gate(self):
        pts = [("t1", 10), ("t2", 80)]
        svg = leaderboard.build_sparkline(pts)
        assert 'fill="#a855f7"' in svg  # diploma purple

    def test_flat_range_avoids_zero_div(self):
        pts = [("t1", 50), ("t2", 50)]
        svg = leaderboard.build_sparkline(pts)
        assert "<path" in svg


class TestBuildHtml:
    def test_smoke_with_minimal_data(self):
        scores = {"agent-a": {"_default": 10, "debugging": 20}}
        history = {"agent-a": {"_default": [("seed", 10), ("t1", 15)]}}
        stats = {
            "total": 1, "successes": 1, "failures": 0, "success_rate": 100.0,
            "domain_counts": {"debugging": 1}, "agent_total": {"agent-a": 1},
            "agent_success": {"agent-a": 1}, "agent_deltas": {"agent-a": 5.0},
            "gate_crossings": [], "session_agents": set(),
        }
        html = leaderboard.build_html(scores, history, stats)
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>Agent School — Live Leaderboard</title>" in html
        assert "agent-a" in html
