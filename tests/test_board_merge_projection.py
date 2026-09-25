"""Behavioral tests for read-only candidate/merge board projection."""

import json

from board import build_board_html
from activity_server import ActivityHandler


def test_board_projection_exposes_candidate_and_merge_fields():
    issues = [{
        "issue_number": 7, "title": "Exact candidate", "domain": "coding", "difficulty": "medium",
        "candidate_id": "candidate-1", "head_sha": "a" * 40, "base_sha": "b" * 40,
        "local_gate": "current", "pr_state": "open", "ci_state": "current",
        "approval_state": "approved", "merge_state": "confirmed", "beads_state": "closed",
    }]
    html = build_board_html(issues, [], [])
    assert "candidate-1" in html
    assert "a" * 40 in html
    assert "confirmed" in html
    assert "merge" in html.lower()


def test_board_projection_keeps_missing_and_contradictory_evidence_visible():
    issues = [{
        "issue_number": 8, "title": "Unknown candidate", "state": "open",
        "candidate_id": "candidate-2", "head_sha": "c" * 40,
        "merge_state": "unknown", "beads_state": "open",
    }]
    html = build_board_html(issues, [], [])
    assert "candidate-2" in html
    assert "unknown" in html.lower()
    assert "no external" not in html.lower()


def test_board_has_no_merge_or_approval_action_controls():
    html = build_board_html([], [], [])
    lowered = html.lower()
    assert "onclick" not in lowered
    assert "<form" not in lowered
    assert "<button" not in lowered
