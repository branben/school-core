#!/usr/bin/env python3
"""Tests for ce_router.py (Rank 4 — deterministic CE skill dispatch router).

All tests run OFFLINE. route_decision's bookbag write is stubbed via an
injected bookbag_writer so no real bookbag I/O occurs.
"""

from unittest.mock import MagicMock, patch

from scripts.ce_router import (
    choose_skill,
    classify_task,
    route_decision,
    SKILL_RANK_1,
    SKILL_RANK_2,
    SKILL_RANK_3,
    SKILL_RANK_5,
    SKILL_RANK_6,
)
import conductor


# ── choose_skill: all 5 task shapes map correctly ───────────────────────────

def test_failed_gate_maps_to_rank1():
    assert choose_skill(classify_task(has_failed_gate=True)) == SKILL_RANK_1


def test_spec_gap_maps_to_rank6():
    assert choose_skill(classify_task(is_spec_gap=True)) == SKILL_RANK_6


def test_architectural_routing_maps_to_rank3():
    assert choose_skill(classify_task(requires_architectural_routing=True)) == SKILL_RANK_3


def test_new_implementation_maps_to_rank2():
    assert choose_skill(classify_task(is_new_implementation=True)) == SKILL_RANK_2


def test_complex_decomposition_maps_to_rank5():
    assert choose_skill(classify_task(complexity=5)) == SKILL_RANK_5


def test_simple_default_maps_to_rank2():
    # No flags → default to CE workflow (Rank 2).
    assert choose_skill(classify_task()) == SKILL_RANK_2


# ── precedence: conflicting flags resolve deterministically ─────────────────

def test_precedence_failed_gate_beats_new_impl():
    shape = classify_task(has_failed_gate=True, is_new_implementation=True)
    assert choose_skill(shape) == SKILL_RANK_1


def test_precedence_spec_gap_beats_architectural():
    shape = classify_task(is_spec_gap=True, requires_architectural_routing=True)
    assert choose_skill(shape) == SKILL_RANK_6


# ── determinism: same input → same output across 100 runs ───────────────────

def test_determinism_100_runs():
    shapes = [
        classify_task(has_failed_gate=True, is_new_implementation=True, complexity=7),
        classify_task(requires_architectural_routing=True, is_spec_gap=True),
        classify_task(is_new_implementation=True, complexity=2),
        classify_task(),
    ]
    for shape in shapes:
        first = choose_skill(shape)
        for _ in range(100):
            assert choose_skill(shape) == first


# ── route_decision: bookbag logging ──────────────────────────────────────────

def test_route_decision_logs_to_bookbag():
    writer = MagicMock()
    out = route_decision(
        classify_task(has_failed_gate=True),
        bead="bead123",
        repo="__global__",
        bookbag_writer=writer,
    )
    assert out["chosen_skill"] == SKILL_RANK_1
    assert out["logged"] is True
    writer.assert_called_once_with(
        "bead123", "__global__",
        chosen_skill=SKILL_RANK_1,
        chosen_skill_label=out["label"],
    )


def test_route_decision_no_bead_no_log():
    writer = MagicMock()
    out = route_decision(classify_task(), bead=None, bookbag_writer=writer)
    assert out["logged"] is False
    writer.assert_not_called()


def test_route_decision_writer_failure_does_not_raise():
    def boom(bead, repo, **kwargs):
        raise RuntimeError("disk full")

    out = route_decision(classify_task(), bead="b", bookbag_writer=boom)
    assert out["logged"] is False  # swallowed, never breaks dispatch


# ── conductor._principal_dispatch attaches chosen_skill ──────────────────────

def test_dispatch_attaches_chosen_skill():
    fake_result = {"status": "success", "agent": "coder", "bead": "bead-xyz",
                   "domain": "python-coding"}
    with patch.object(conductor, "run_leaf", return_value=fake_result), \
         patch.object(conductor, "locked_update_bookbag", MagicMock()) as w:
        out = conductor._principal_dispatch(
            task="t", role="coder", domain="python-coding",
            difficulty="easy", store=MagicMock(), repo="__global__",
        )
    assert out["chosen_skill"] == SKILL_RANK_2  # default new-implementation
    # bookbag log happened with the dispatched bead.
    w.assert_called_once()
    _, kwargs = w.call_args
    assert kwargs["chosen_skill"] == SKILL_RANK_2


def test_dispatch_chosen_skill_reflects_task_shape():
    fake_result = {"status": "success", "agent": "coder", "bead": "bead-f",
                   "domain": "python-coding"}
    shape = classify_task(has_failed_gate=True)
    with patch.object(conductor, "run_leaf", return_value=fake_result), \
         patch.object(conductor, "locked_update_bookbag", MagicMock()):
        out = conductor._principal_dispatch(
            task="t", role="coder", domain="python-coding",
            difficulty="easy", store=MagicMock(), repo="__global__",
            task_shape=shape,
        )
    assert out["chosen_skill"] == SKILL_RANK_1
