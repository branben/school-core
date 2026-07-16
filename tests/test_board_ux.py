"""Tests for board UX features: session deep-link, column count badges, title filter.

Run: python -m pytest tests/test_board_ux.py -v
"""

from board import build_board_html


class TestSessionDeepLink:
    """Card with trajectory shows session deep-link."""

    def test_session_link_rendered(self):
        """A card with trajectory in last_run gets a session ↗ link."""
        issues = [
            {
                "issue_number": 1,
                "title": "Task with traj",
                "domain": "coding",
                "difficulty": "easy",
                "state": "open",
            },
        ]
        last_run = [
            {
                "issue": 1,
                "status": "success",
                "agent": "alpha",
                "score": 90,
                "trajectory": "/abs/path/to/20260612_001113_x--_default--m.json",
            },
        ]
        html = build_board_html(issues, [], last_run)
        assert 'href="/trajectory/20260612_001113_x--_default--m.json"' in html
        assert "session ↗" in html

    def test_no_trajectory_no_link(self):
        """Card without trajectory has no session link."""
        issues = [
            {
                "issue_number": 2,
                "title": "No traj",
                "domain": "debugging",
                "difficulty": "medium",
                "state": "open",
            },
        ]
        last_run = [
            {"issue": 2, "status": "success", "agent": "alpha", "score": 80},
        ]
        html = build_board_html(issues, [], last_run)
        assert 'class="card-session"' not in html


class TestColumnCountBadges:
    """Column headers show count badges."""

    def test_column_count_badge(self):
        """Column header contains a count badge with correct count."""
        issues = [
            {
                "issue_number": 1,
                "title": "Task 1",
                "domain": "coding",
                "difficulty": "easy",
                "state": "open",
            },
            {
                "issue_number": 2,
                "title": "Task 2",
                "domain": "debugging",
                "difficulty": "medium",
                "state": "open",
            },
            {
                "issue_number": 3,
                "title": "Task 3",
                "domain": "testing",
                "difficulty": "hard",
                "state": "open",
            },
        ]
        html = build_board_html(issues, [], [])
        # To Do should have 3 items
        assert '<span class="col-count">3</span>' in html
        # Done should have 0 items
        assert '<span class="col-count">0</span>' in html


class TestTitleFilter:
    """Board has a title filter input and JS."""

    def test_filter_input_present(self):
        """HTML contains an input with id='board-filter'."""
        html = build_board_html([], [], [])
        assert 'id="board-filter"' in html

    def test_filter_js_present(self):
        """JS references the filter input."""
        html = build_board_html([], [], [])
        assert "board-filter" in html  # appears in both HTML and JS
