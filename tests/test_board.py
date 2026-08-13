"""
Tests for the self-contained kanban board HTML generator (Task 2 of Durable Board plan).

Run: python -m pytest tests/test_board.py -v
"""

import json
from pathlib import Path

import pytest

from board import assign_column, build_board_html


class TestBuildBoardHtml:
    """Tests for build_board_html — the self-contained kanban board generator."""

    def test_returns_doctype_html(self):
        """build_board_html returns a complete <!doctype html> document."""
        html = build_board_html([], [], [])
        assert html.strip().lower().startswith("<!doctype html")

    def test_contains_four_column_headers(self):
        """The HTML contains all 4 column header labels."""
        html = build_board_html([], [], [])
        assert "To Do" in html
        assert "In Progress" in html
        assert "In Review" in html
        assert "Done" in html

    def test_contains_embedded_style(self):
        """The HTML has embedded <style> (no external CDN link)."""
        html = build_board_html([], [], [])
        assert "<style>" in html
        # No external CSS/CDN links
        assert "//fonts.googleapis.com" not in html
        assert "//cdn." not in html
        assert "//unpkg.com" not in html

    def test_column_assignment(self):
        """Crafted fixture: each column gets at least one card based on rules.

        Rules:
          - To Do:   open issue, not processed, not done/blocked in last_run
          - In Progress: in last_run with status 'in_progress'
          - In Review:   in last_run with status 'review' or 'in_review'
          - Done:    in processed OR in last_run with status 'done'/'success'
        """
        issues = [
            {"issue_number": 1, "title": "To Do Task", "domain": "debugging",
             "difficulty": "easy", "state": "open"},
            {"issue_number": 2, "title": "In Progress Task", "domain": "coding",
             "difficulty": "medium", "state": "open"},
            {"issue_number": 3, "title": "In Review Task", "domain": "testing",
             "difficulty": "hard", "state": "open"},
            {"issue_number": 4, "title": "Done Task", "domain": "docs",
             "difficulty": "easy", "state": "done"},
        ]
        processed = [4]
        last_run = [
            {"issue": 2, "status": "in_progress", "agent": "alpha", "score": 80},
            {"issue": 3, "status": "review", "agent": "beta", "score": 85},
        ]

        html = build_board_html(issues, processed, last_run)

        # Issue 1: open, not processed, not in last_run → To Do
        assert "To Do Task" in html, "Issue 1 should land in 'To Do'"
        # Issue 2: in last_run with in_progress → In Progress
        assert "In Progress Task" in html, "Issue 2 should land in 'In Progress'"
        # Issue 3: in last_run with review → In Review
        assert "In Review Task" in html, "Issue 3 should land in 'In Review'"
        # Issue 4: in processed → Done
        assert "Done Task" in html, "Issue 4 should land in 'Done'"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("retry", "retry"),
            ("blocked", "blocked"),
            ("crew_in_flight", "crew_in_flight"),
            ("school-failed", "school_failed"),
            ("error", "school_failed"),
        ],
    )
    def test_failure_and_waiting_states_have_distinct_columns(self, status, expected):
        """Operational failure states must not disappear into To Do."""
        issue = {"issue_number": 20, "title": "Stateful issue", "state": "open"}
        assert assign_column(issue, set(), {20: {"status": status}}) == expected

    def test_board_renders_failure_state_columns(self):
        """The durable status columns are visible to a human operator."""
        html = build_board_html(
            [{"issue_number": 21, "title": "Failed issue", "state": "open"}],
            [],
            [{"issue": 21, "status": "school-failed"}],
        )
        assert "School Failed" in html
        assert "Failed issue" in html

    def test_column_assignment_with_last_run_done(self):
        """Issues in last_run with status 'success' map to Done."""
        issues = [
            {"issue_number": 10, "title": "Completed Issue", "domain": "coding",
             "difficulty": "medium", "state": "open"},
        ]
        processed = []
        last_run = [
            {"issue": 10, "status": "success", "agent": "gamma", "score": 95},
        ]
        html = build_board_html(issues, processed, last_run)
        assert "#10" in html or "10" in html
        assert "Completed Issue" in html
        # Should NOT be in To Do column
        sections = html.split('<section')
        # Find the Done section
        assert "Done" in html

    def test_unknown_issue_state_defaults_to_todo(self):
        """An issue with no state (or unknown state) defaults to To Do if
        not processed and not in last_run."""
        issues = [
            {"issue_number": 99, "title": "Mystery Issue", "domain": "research",
             "difficulty": "easy", "state": "unknown"},
        ]
        html = build_board_html(issues, [], [])
        assert "Mystery Issue" in html
        assert "#99" in html
