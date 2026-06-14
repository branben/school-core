"""
Tests for U6: Growth Measurement — difficulty-adjusted capability tracking.

Run: python -m pytest tests/test_growth_tracker.py -v
"""

import json
import math
from datetime import datetime, timezone, timedelta

import pytest

from growth_tracker import (
    GrowthTracker,
    calculate_difficulty_weight,
    CapabilityProfile,
    GrowthReport,
)


@pytest.fixture
def tracker(tmp_path):
    return GrowthTracker(data_dir=str(tmp_path / "growth"))


def _ts(days_ago=0, hours_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.isoformat()


class TestHappyPathGrowth:
    def test_growth_score_improves_with_harder_tasks(self, tracker):
        tracker.record_performance("ada", "python-coding", 80.0, 2.0, timestamp=_ts(days_ago=2))
        tracker.record_performance("ada", "python-coding", 60.0, 3.0, timestamp=_ts(days_ago=1))
        profile = tracker.get_capability_profile("ada")
        assert profile.agent == "ada"
        assert not profile.insufficient_data
        assert len(profile.difficulty_progression) == 2
        assert profile.difficulty_progression[0] == 2.0
        assert profile.difficulty_progression[1] == 3.0

    def test_difficulty_weighted_growth_rate_positive(self, tracker):
        tracker.record_performance("ada", "code-review", 50.0, 1.0, timestamp=_ts(days_ago=4))
        tracker.record_performance("ada", "code-review", 60.0, 1.5, timestamp=_ts(days_ago=3))
        tracker.record_performance("ada", "code-review", 70.0, 2.0, timestamp=_ts(days_ago=2))
        tracker.record_performance("ada", "code-review", 80.0, 2.5, timestamp=_ts(days_ago=1))
        profile = tracker.get_capability_profile("ada")
        assert profile.growth_rate > 0


class TestNoGrowth:
    def test_flat_scores_yield_zero_growth_rate(self, tracker):
        for i in range(10):
            tracker.record_performance("turing", "testing", 70.0, 1.0, timestamp=_ts(days_ago=10 - i))
        profile = tracker.get_capability_profile("turing")
        assert abs(profile.growth_rate) < 1e-6


class TestRegression:
    def test_recent_lower_scores_marked_as_regression(self, tracker):
        tracker.record_performance("grace", "planning", 80.0, 2.0, timestamp=_ts(days_ago=10))
        tracker.record_performance("grace", "planning", 85.0, 2.0, timestamp=_ts(days_ago=9))
        tracker.record_performance("grace", "planning", 40.0, 2.0, timestamp=_ts(days_ago=2))
        tracker.record_performance("grace", "planning", 35.0, 2.0, timestamp=_ts(days_ago=1))
        report = tracker.query_growth("grace", days=7)
        assert report.is_better_than_previous is False

    def test_recent_higher_scores_not_regression(self, tracker):
        tracker.record_performance("grace", "planning", 30.0, 1.0, timestamp=_ts(days_ago=10))
        tracker.record_performance("grace", "planning", 35.0, 1.0, timestamp=_ts(days_ago=9))
        tracker.record_performance("grace", "planning", 70.0, 2.0, timestamp=_ts(days_ago=2))
        tracker.record_performance("grace", "planning", 75.0, 2.0, timestamp=_ts(days_ago=1))
        report = tracker.query_growth("grace", days=7)
        assert report.is_better_than_previous is True


class TestCapabilityProfile:
    def test_strengths_and_weaknesses(self, tracker):
        tracker.record_performance("min", "code-review", 85.0, 2.0, timestamp=_ts(days_ago=3))
        tracker.record_performance("min", "code-review", 90.0, 2.5, timestamp=_ts(days_ago=2))
        tracker.record_performance("min", "planning", 25.0, 1.0, timestamp=_ts(days_ago=1))
        tracker.record_performance("min", "planning", 30.0, 1.2, timestamp=_ts(days_ago=0))
        profile = tracker.get_capability_profile("min")
        assert "code-review" in profile.strengths
        assert "planning" in profile.weaknesses

    def test_no_strengths_when_all_mid(self, tracker):
        tracker.record_performance("phi", "testing", 50.0, 1.0, timestamp=_ts(days_ago=1))
        profile = tracker.get_capability_profile("phi")
        assert profile.strengths == []
        assert profile.weaknesses == []


class TestDifficultyWeight:
    def test_harder_task_has_higher_weight(self):
        easy = calculate_difficulty_weight(files_touched=1, test_failures=0, complexity=0)
        hard = calculate_difficulty_weight(files_touched=5, test_failures=3, complexity=2)
        assert hard > easy

    def test_formula_base_is_1(self):
        assert calculate_difficulty_weight(0, 0, 0) == 1.0

    def test_formula_values(self):
        w = calculate_difficulty_weight(3, 2, 1)
        expected = 1.0 + 0.5 * math.log2(4) + 0.3 * 2 + 0.2 * 1
        assert abs(w - expected) < 1e-9


class TestEmptyHistory:
    def test_insufficient_data(self, tracker):
        profile = tracker.get_capability_profile("unknown")
        assert profile.insufficient_data
        assert profile.strengths == []
        assert profile.weaknesses == []

    def test_empty_growth_report(self, tracker):
        report = tracker.query_growth("unknown", days=7)
        assert report.tasks_attempted == 0
        assert report.is_better_than_previous is None


class TestTimeWindow:
    def test_query_last_7_days(self, tracker):
        tracker.record_performance("ada", "coding", 90.0, 2.0, timestamp=_ts(days_ago=1))
        tracker.record_performance("ada", "coding", 85.0, 1.8, timestamp=_ts(days_ago=2))
        tracker.record_performance("ada", "coding", 30.0, 1.0, timestamp=_ts(days_ago=20))
        report = tracker.query_growth("ada", days=7)
        assert report.tasks_attempted == 2
        assert report.agent == "ada"
        assert report.period == "last 7 days"

    def test_14_day_window_captures_more(self, tracker):
        tracker.record_performance("ada", "coding", 90.0, 2.0, timestamp=_ts(days_ago=3))
        tracker.record_performance("ada", "coding", 70.0, 1.5, timestamp=_ts(days_ago=10))
        tracker.record_performance("ada", "coding", 30.0, 1.0, timestamp=_ts(days_ago=20))
        report = tracker.query_growth("ada", days=14)
        assert report.tasks_attempted == 2

    def test_report_counts_successes(self, tracker):
        tracker.record_performance("ada", "coding", 60.0, 1.5, timestamp=_ts(days_ago=2))
        tracker.record_performance("ada", "coding", 40.0, 1.0, timestamp=_ts(days_ago=1))
        report = tracker.query_growth("ada", days=7)
        assert report.tasks_succeeded == 1

    def test_avg_difficulty_in_report(self, tracker):
        tracker.record_performance("ada", "coding", 80.0, 2.0, timestamp=_ts(days_ago=2))
        tracker.record_performance("ada", "coding", 70.0, 3.0, timestamp=_ts(days_ago=1))
        report = tracker.query_growth("ada", days=7)
        assert report.avg_difficulty == 2.5

