"""Tests for isolated_reasoning.py — isolated reasoning phases for students.

The Diversity Collapse paper (arXiv:2604.18005) shows that when every student
receives the SAME shared context, their reasoning converges to identical
outputs. The fix implemented here: run each student in an *isolated* reasoning
phase with a per-student context that is decoupled from the others, then
measure the effective diversity of the resulting outputs with the Vendi Score.

These tests assert the core contracts:

1. Context isolation — each phase gets its own independent context dict; one
   phase's context block selection cannot leak into another's.
2. Anti-collapse mechanism — feeding students *isolated* (per-seed-varied)
   context yields higher Vendi (more diverse outputs) than feeding them the
   same context; identical outputs collapse to Vendi ~1.
3. Deterministic & reproducible given (students, seeds).
4. Collapse detection flag + medoid selection of a representative output.
5. No numpy/scipy dependency (stdlib only).
"""
from __future__ import annotations

import copy
import re

import pytest

from isolated_reasoning import (
    IsolationResult,
    PhaseResult,
    build_isolated_prompt,
    run_isolated_phases,
    select_context_blocks,
)


# A reason_fn that echoes a checksum of its prompt. Deterministic across runs
# for a fixed (student, seed, context) so we can test reproducibility.
def _checksum_reason_fn(student_id, prompt, seed):
    return f"[{student_id}] h={hash(prompt) % 100000:05d}"


# A reason_fn that responds to the *content* of the isolated context, not to
# student identity. This models a model that reasons from its context: when
# every student sees the same context it returns the same answer (collapse);
# when contexts are decoupled per student the answers diverge (diversity).
def _context_content_reason_fn(student_id, prompt, seed):
    if "## Your context" not in prompt:
        return "keys="
    body = prompt.split("## Your context", 1)[1].split("## Task", 1)[0]
    keys = re.findall(r"^- ([A-Za-z_]+):", body, flags=re.M)
    return "keys=" + ",".join(sorted(keys))


# A reason_fn that always returns the same canonical answer regardless of input
# (e.g. greedy decoding collapsing to one degenerate output). Used to prove the
# ``collapsed`` flag triggers when every phase emits identical text.
def _constant_reason_fn(student_id, prompt, seed):
    return "canonical identical answer"


# --------------------------------------------------------------------------
# Context isolation
# --------------------------------------------------------------------------
def test_select_context_blocks_is_deterministic_per_seed():
    base = {"vault": "v", "prior": "p", "anchor": "a", "serena": "s"}
    a = select_context_blocks(base, "stu-1", seed=7, drop_rate=0.5)
    b = select_context_blocks(base, "stu-1", seed=7, drop_rate=0.5)
    assert a == b  # determinism


def test_select_context_blocks_independent_objects():
    base = {"vault": "v", "prior": "p"}
    b1 = select_context_blocks(base, "stu-1", seed=1, drop_rate=0.0)
    b2 = select_context_blocks(base, "stu-2", seed=2, drop_rate=0.0)
    # Mutating one must not mutate the other (no shared reference leakage).
    b1["vault"] = "TAMPERED"
    assert b2["vault"] == "v"


def test_run_phases_do_not_share_context_objects():
    base = {"vault": "v", "prior": "p", "anchor": "a"}
    result = run_isolated_phases(
        task_prompt="do the thing",
        students=["a", "b", "c"],
        base_blocks=base,
        reason_fn=_checksum_reason_fn,
        seeds=[1, 2, 3],
        drop_rate=0.5,
    )
    blocks = [p.context_blocks for p in result.phases]
    # Every phase holds a distinct dict object.
    assert len({id(b) for b in blocks}) == len(blocks)
    # Mutating the first does not change the others.
    first = blocks[0]
    first["vault"] = "TAMPERED"
    assert all(b.get("vault") != "TAMPERED" for b in blocks[1:])


# --------------------------------------------------------------------------
# Anti-collapse mechanism
# --------------------------------------------------------------------------
def test_shared_context_collapses():
    base = {"vault": "v", "prior": "p"}
    # drop_rate 0 => every student sees the SAME full context => same response
    # content => Vendi ~1 (diversity collapse), independent of student id.
    result = run_isolated_phases(
        task_prompt="solve",
        students=["a", "b", "c", "d"],
        base_blocks=base,
        reason_fn=_context_content_reason_fn,
        seeds=[1, 2, 3, 4],
        drop_rate=0.0,
    )
    assert result.vendi_score == pytest.approx(1.0, abs=1e-6)
    assert result.collapsed is True


def test_isolated_context_increases_diversity():
    base = {"vault": "v", "prior": "p", "anchor": "a", "serena": "s"}
    result = run_isolated_phases(
        task_prompt="solve",
        students=["a", "b", "c"],
        base_blocks=base,
        reason_fn=_context_content_reason_fn,
        seeds=[11, 23, 37],
        drop_rate=0.7,
    )
    # Decoupled per-student contexts => distinct context content => diverse.
    assert result.vendi_score > 1.0
    assert result.collapsed is False
    # And provably higher than the shared-context (collapsed) baseline.
    collapsed = run_isolated_phases(
        task_prompt="solve",
        students=["a", "b", "c"],
        base_blocks=base,
        reason_fn=_context_content_reason_fn,
        seeds=[1, 2, 3],
        drop_rate=0.0,
    )
    assert result.vendi_score > collapsed.vendi_score


def test_distinct_responses_diverse_vs_identical():
    base = {"vault": "v"}
    distinct = run_isolated_phases(
        task_prompt="t",
        students=["a", "b", "c"],
        base_blocks=base,
        # Each student returns a uniquely-identifying response.
        reason_fn=lambda sid, p, s: f"response-from-{sid}",
        seeds=[1, 2, 3],
    )
    assert distinct.vendi_score > 1.0
    assert distinct.collapsed is False


# --------------------------------------------------------------------------
# Determinism of the whole run
# --------------------------------------------------------------------------
def test_run_is_deterministic():
    base = {"vault": "v", "prior": "p", "anchor": "a"}
    kw = dict(
        task_prompt="t", students=["a", "b"], base_blocks=base,
        reason_fn=_checksum_reason_fn, seeds=[5, 9], drop_rate=0.5,
    )
    r1 = run_isolated_phases(**kw)
    r2 = run_isolated_phases(**kw)
    assert r1.vendi_score == r2.vendi_score
    assert [p.response for p in r1.phases] == [p.response for p in r2.phases]
    assert r1.selected_response == r2.selected_response


# --------------------------------------------------------------------------
# Medoid selection (most representative output)
# --------------------------------------------------------------------------
def test_medoid_selection_picks_centroid():
    # b ("alpha beta") is the medoid: it is char-n-gram closest to both
    # a ("alpha") and c ("alpha beta gamma zeta"). Jaccard distances: a-b
    # small, b-c small, a-c large -> b is the centroid.
    base = {"vault": "v"}
    result = run_isolated_phases(
        task_prompt="t",
        students=["a", "b", "c"],
        base_blocks=base,
        reason_fn=lambda sid, p, s: {
            "a": "alpha",
            "b": "alpha beta",
            "c": "alpha beta gamma zeta",
        }[sid],
        seeds=[1, 2, 3],
    )
    assert result.selected_student == "b"
    assert result.selected_response == "alpha beta"


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------
def test_empty_students():
    result = run_isolated_phases(
        task_prompt="t", students=[], base_blocks={},
        reason_fn=_checksum_reason_fn,
    )
    assert isinstance(result, IsolationResult)
    assert result.phases == []
    assert result.vendi_score == 0.0
    assert result.collapsed is True
    assert result.selected_response is None


def test_single_student():
    result = run_isolated_phases(
        task_prompt="t", students=["only"], base_blocks={"v": "x"},
        reason_fn=lambda sid, p, s: "solo", seeds=[1],
    )
    assert result.vendi_score == pytest.approx(1.0, abs=1e-6)
    assert result.selected_student == "only"
    assert result.selected_response == "solo"


def test_build_isolated_prompt_includes_context_and_task():
    prompt = build_isolated_prompt(
        "write a function", {"vault": "V", "prior": "P"}, student_id="s1"
    )
    assert "write a function" in prompt
    assert "V" in prompt and "P" in prompt
    assert "s1" in prompt


# --------------------------------------------------------------------------
# stdlib-only guarantee
# --------------------------------------------------------------------------
def test_no_heavy_dependencies():
    import inspect
    import isolated_reasoning as _m

    src = inspect.getsource(_m)
    for forbidden in ("numpy", "scipy", "sklearn", "sentence_transformers", "torch"):
        assert f"import {forbidden}" not in src
        assert f"from {forbidden}" not in src
