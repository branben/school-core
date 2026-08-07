"""Tests for the ACRouter experience store (Agent-as-Router outcome feedback).

The ACRouter treats combo selection as an experience-gathering agent: every
routing decision records a success/quality outcome, and future selections use
that experience (epsilon-greedy bandit) to favour combos that actually work
for a given routing context (role/domain) instead of a static COMBO_MAP.
"""

import json
from pathlib import Path

from executor import COMBO_MAP  # to derive candidate set
from router_experience import RouterExperience, combo_candidates_from


def _candidates():
    return combo_candidates_from(COMBO_MAP)


class TestCandidateDerivation:
    def test_candidates_are_distinct_combos(self):
        cands = combo_candidates_from(COMBO_MAP)
        assert isinstance(cands, list)
        # No duplicates
        assert len(cands) == len(set(cands))
        # Every COMBO_MAP value appears as a candidate
        for combo in COMBO_MAP.values():
            assert combo in cands

    def test_empty_map_yields_empty_candidates(self):
        assert combo_candidates_from({}) == []


class TestSelection:
    def test_no_experience_returns_default(self, tmp_path):
        exp = RouterExperience(
            candidates=_candidates(),
            default_resolver=lambda r: COMBO_MAP.get(r),
            file_path=str(tmp_path / "exp.json"),
            exploration_rate=0.0,
        )
        # No data recorded yet → fall back to the static default for that role
        assert exp.select_combo("coder") == COMBO_MAP.get("coder")
        # Unknown role with no experience → None default
        assert exp.select_combo("nonexistent-role") is None

    def test_selects_best_combo_after_experience(self, tmp_path):
        # Two candidate combos for the "coder" context.
        candidates = ["auto/best-free", "oc/deepseek-v4-flash-free"]
        exp = RouterExperience(
            candidates=candidates,
            default_resolver=lambda r: "auto/best-free",
            file_path=str(tmp_path / "exp.json"),
            exploration_rate=0.0,  # exploit only — deterministic
        )
        # Teach the router: "oc/deepseek-v4-flash-free" is great, "auto/best-free" is bad
        for _ in range(5):
            exp.record_outcome("coder", "oc/deepseek-v4-flash-free", success=True, quality=0.9)
        for _ in range(5):
            exp.record_outcome("coder", "auto/best-free", success=False, quality=0.2)
        assert exp.select_combo("coder") == "oc/deepseek-v4-flash-free"

    def test_exploration_can_pick_non_best(self, tmp_path):
        candidates = ["auto/best-free", "oc/deepseek-v4-flash-free"]
        exp = RouterExperience(
            candidates=candidates,
            default_resolver=lambda r: "auto/best-free",
            file_path=str(tmp_path / "exp.json"),
            exploration_rate=1.0,  # always explore
        )
        for _ in range(5):
            exp.record_outcome("coder", "oc/deepseek-v4-flash-free", success=True, quality=0.9)
        # With full exploration, selection is random but must be a valid candidate
        chosen = exp.select_combo("coder")
        assert chosen in candidates

    def test_unknown_candidate_is_rejected_on_record(self, tmp_path):
        exp = RouterExperience(
            candidates=["auto/best-free"],
            default_resolver=lambda r: "auto/best-free",
            file_path=str(tmp_path / "exp.json"),
        )
        # Recording an outcome for a combo not in candidates is ignored
        # (should not raise, should not pollute stats).
        exp.record_outcome("coder", "not-a-real-combo", success=True, quality=1.0)
        assert exp.stats.get("coder", {}).get("not-a-real-combo") is None


class TestOutcomeRecording:
    def test_stats_track_trials_and_quality(self, tmp_path):
        exp = RouterExperience(
            candidates=["auto/best-free", "oc/deepseek-v4-flash-free"],
            default_resolver=lambda r: "auto/best-free",
            file_path=str(tmp_path / "exp.json"),
            exploration_rate=0.0,
        )
        exp.record_outcome("coder", "auto/best-free", success=True, quality=0.8)
        exp.record_outcome("coder", "auto/best-free", success=False, quality=0.3)
        stat = exp.stats["coder"]["auto/best-free"]
        assert stat.trials == 2
        assert stat.successes == 1
        assert stat.success_rate == 0.5
        assert abs(stat.quality - 0.55) < 1e-9  # mean of 0.8 and 0.3
        # Only ever-recorded combo is present
        assert set(exp.stats["coder"].keys()) == {"auto/best-free"}

    def test_experience_is_persisted_and_reloaded(self, tmp_path):
        path = tmp_path / "exp.json"
        exp = RouterExperience(
            candidates=["auto/best-free"],
            default_resolver=lambda r: "auto/best-free",
            file_path=str(path),
            exploration_rate=0.0,
        )
        exp.record_outcome("coder", "auto/best-free", success=True, quality=0.9)
        # New instance reading the same file should see the recorded stats
        exp2 = RouterExperience(
            candidates=["auto/best-free"],
            default_resolver=lambda r: "auto/best-free",
            file_path=str(path),
            exploration_rate=0.0,
        )
        assert exp2.stats["coder"]["auto/best-free"].trials == 1
        assert exp2.stats["coder"]["auto/best-free"].success_rate == 1.0

    def test_default_resolver_used_when_no_experience(self, tmp_path):
        exp = RouterExperience(
            candidates=_candidates(),
            default_resolver=lambda r: COMBO_MAP.get(r),
            file_path=str(tmp_path / "exp.json"),
            exploration_rate=0.0,
        )
        # Selector honours the static mapping until experience overrides it
        for role in ("searcher", "executor", "reviewer", "browser", "coder"):
            assert exp.select_combo(role) == COMBO_MAP.get(role)
