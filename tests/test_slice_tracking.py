"""Focused tests for the slice-tracking contract (U5).

Covers the pure logic that is safe to test without a browser or the bd CLI:
the lane mapping and the bd-command export builder. The triage.html template
is hand-verified at desktop/mobile widths per the build contract.
"""

import json

import pytest

from scripts.build_board_json import (
    build_bd_commands,
    build_board,
    card_from_run_dict,
    infer_tag,
    lane_for,
    latest_by_issue,
)


# ---------------------------------------------------------------------------
# Lane mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("in_progress", "now"),
        ("crew_in_flight", "now"),
        ("in_review", "next"),
        ("retry", "next"),
        ("blocked", "next"),
        ("success", "cut"),
        ("done", "cut"),
        ("error", "cut"),
        ("school-failed", "cut"),
        (None, "later"),
        ("", "later"),
        ("mystery_status", "later"),
    ],
)
def test_lane_for(status, expected):
    assert lane_for(status) == expected


def test_card_preserves_original_status_as_reason():
    card = card_from_run_dict(
        {"issue": 42, "status": "school-failed", "rejection": "cto=FAIL"}
    )
    assert card["lane"] == "cut"
    assert card["reason"] == "cto=FAIL"
    assert card["id"] == "42"


def test_card_has_tag_and_actor():
    card = card_from_run_dict(
        {"issue": 7, "status": "in_progress", "domain": "python-testing", "agent": "coder"}
    )
    assert card["tag"] == "python-testing"
    assert card["actor"] == "coder"


def test_infer_tag_from_title_when_no_domain():
    assert infer_tag({"domain": "_default"}, "security: bump react-router") == "security"


# ---------------------------------------------------------------------------
# bd-command export (the apply-gate contract)
# ---------------------------------------------------------------------------


def test_bd_commands_emit_changed_lanes_only():
    # orig_lane is the pipeline truth; unchanged cards emit nothing (idempotent).
    board = [
        {"id": "10", "lane": "now", "orig_lane": "next"},   # changed → claim
        {"id": "11", "lane": "next", "orig_lane": "next"},  # unchanged → skip
        {"id": "12", "lane": "cut", "orig_lane": "later"},  # changed → close
    ]
    out = build_bd_commands(board)
    assert out.splitlines() == [
        "bd update 10 --claim",
        'bd close 12 --reason="Triage cut"',
    ]


def test_bd_commands_emit_expected_invocations():
    # the full lane mapping
    board = [
        {"id": "10", "lane": "now", "orig_lane": "later"},
        {"id": "11", "lane": "next", "orig_lane": "now"},
        {"id": "12", "lane": "cut", "orig_lane": "now"},
    ]
    assert build_bd_commands(board).splitlines() == [
        "bd update 10 --claim",
        "bd update 11 --status open",
        'bd close 12 --reason="Triage cut"',
    ]


def test_bd_commands_skip_unchanged_cards():
    board = [
        {"id": "13", "lane": "cut", "orig_lane": "cut"},
        {"id": "14", "lane": "now", "orig_lane": "in_progress"},
    ]
    out = build_bd_commands(board)
    assert out.splitlines() == ["bd update 14 --claim"]


def test_bd_commands_skip_cards_without_id_or_lane():
    board = [
        {"id": "", "lane": "cut", "orig_lane": "next"},
        {"id": "5", "lane": None, "orig_lane": "now"},
        {"id": "6", "lane": "later", "orig_lane": "now"},
    ]
    assert build_bd_commands(board).splitlines() == ["bd update 6 --status open"]


# ---------------------------------------------------------------------------
# build_board integration over the real pipeline fields
# ---------------------------------------------------------------------------


def test_build_board_latest_wins_per_issue():
    runs = [
        {"issue": 1, "status": "retry"},
        {"issue": 1, "status": "in_progress"},
        {"issue": 2, "status": "success"},
    ]
    latest = latest_by_issue(runs)
    assert latest["1"]["status"] == "in_progress"
    board = build_board(runs, max_cards=10)["board"]
    lanes = {c["id"]: c["lane"] for c in board}
    assert lanes == {"1": "now", "2": "cut"}


def test_build_board_uses_cache_titles():
    runs = [{"issue": 99, "status": "blocked"}]
    titles = {"99": "Fix sync toast"}
    card = build_board(runs, titles, max_cards=10)["board"][0]
    assert card["title"] == "Fix sync toast"
    assert card["reason"] == "blocked"


def test_build_board_outputs_self_contained_shapes():
    runs = [{"issue": 1, "status": "success"}, {"issue": 2, "status": "blocked"}]
    out = build_board(runs, max_cards=10)
    assert set(out) == {"generated_at", "board"}
    for card in out["board"]:
        assert set(card) >= {"id", "title", "lane", "actor", "reason", "tag"}


def test_board_json_matches_real_export_contract(tmp_path):
    """Re-runs the actual exporter against realistic pipeline JSON."""
    from scripts.build_board_json import main

    last_run = tmp_path / "last_run.json"
    last_run.write_text(json.dumps([{"issue": 3, "status": "retry", "agent": "coder"}]))
    cache = tmp_path / "issues_cache.json"
    cache.write_text(json.dumps([{"issue_number": 3, "title": "Triage #03"}]))
    out = tmp_path / "board.json"
    rc = main(["--out", str(out), "--last-run", str(last_run), "--issues-cache", str(cache)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["board"][0]["title"] == "Triage #03"
    assert data["board"][0]["lane"] == "next"