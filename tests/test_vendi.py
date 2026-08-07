"""Tests for vendi.py — pure-Python Vendi Score diversity metric.

Vendi Score (Wang & Blei, 2023, arXiv:2304.14970) is the exponential of the
Shannon entropy of the (normalized) eigenvalues of a similarity matrix. It
generalizes "effective number of species" to arbitrary similarity functions.

Key behavioral contracts asserted here:
- Empty input -> 0.0 (undefined, treat as no diversity).
- Single output -> 1.0 (one identity, always "diverse enough" = itself).
- N identical outputs -> 1.0 (collapse: effective count = 1).
- N mutually-distinct outputs -> > 1.0, and increases with distinctness.
- Precomputed similarity matrix path matches text path.
- No numpy/scipy dependency (stdlib only).
"""
from __future__ import annotations

import math

import pytest

from vendi import (
    vendi_score,
    vendi_score_from_similarity,
    pairwise_similarity,
)


# --------------------------------------------------------------------------
# Empty / single
# --------------------------------------------------------------------------
def test_empty_returns_zero():
    assert vendi_score([]) == 0.0


def test_single_returns_one():
    assert vendi_score(["only one output"]) == 1.0


# --------------------------------------------------------------------------
# Collapse: identical outputs stay at Vendi 1.0
# --------------------------------------------------------------------------
def test_identical_outputs_collapse_to_one():
    texts = ["def add(a, b):\n    return a + b"] * 5
    assert vendi_score(texts) == pytest.approx(1.0, abs=1e-6)


def test_near_identical_low_diversity():
    # Whitespace/punctuation-only differences leave char-n-gram Jaccard high
    # but not 1.0, so Vendi is modestly above 1.0 (low diversity, not fully
    # collapsed). The key contract: it stays far below the max for 3 items
    # (~3.0) and well under a genuinely distinct set.
    texts = [
        "The cat sat on the mat.",
        "the cat sat on the mat",
        "The cat sat on the mat !",
    ]
    score = vendi_score(texts)
    assert 1.0 <= score < 2.0
    distinct = [
        "def add(a, b):\n    return a + b",
        "SELECT * FROM users WHERE active = 1;",
        "The quick brown fox jumps over the lazy dog.",
    ]
    assert score < vendi_score(distinct)
    # Coarser n-gram comparison compresses near-identical variants toward 1.0,
    # but they never exceed a genuinely distinct set of the same length.
    assert vendi_score(texts, ngram_size=8) < vendi_score(distinct)


# --------------------------------------------------------------------------
# Diversity: distinct outputs raise Vendi above 1.0
# --------------------------------------------------------------------------
def test_distinct_outputs_above_one():
    texts = [
        "def add(a, b):\n    return a + b",
        "SELECT * FROM users WHERE active = 1;",
        "The quick brown fox jumps over the lazy dog.",
    ]
    score = vendi_score(texts)
    assert score > 1.0


def test_more_diverse_is_higher():
    low = [
        "function sum(x, y) { return x + y; }",
        "function add(a, b) { return a + b; }",
    ]
    high = [
        "function sum(x, y) { return x + y; }",
        "SELECT id FROM orders LIMIT 10;",
        "Once upon a time in a land far away.",
    ]
    assert vendi_score(high) > vendi_score(low) > 1.0


def test_pairwise_orthogonal_gives_two():
    # Two completely disjoint n-gram sets -> similarity 0 -> Vendi = 2.
    texts = ["aaaaaaaa", "bbbbbbbb"]
    # With ngram_size=3, "aaaaaaaa" -> {aaa}*6, "bbbbbbbb" -> {bbb}*6, Jaccard 0.
    assert vendi_score(texts, ngram_size=3) == pytest.approx(2.0, abs=1e-6)


# --------------------------------------------------------------------------
# Configurable n-gram granularity
# --------------------------------------------------------------------------
def test_ngram_size_changes_similarity():
    # Word-level-ish via larger ngram can flip identical short strings.
    a = "hello world"
    b = "hello world"
    assert vendi_score([a, b], ngram_size=5) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------
# Precomputed similarity matrix parity
# --------------------------------------------------------------------------
def test_from_similarity_matches_text():
    texts = ["alpha beta gamma", "alpha beta delta", " totally different"]
    S = pairwise_similarity(texts, ngram_size=3)
    via_text = vendi_score(texts, ngram_size=3)
    via_matrix = vendi_score_from_similarity(S)
    assert via_matrix == pytest.approx(via_text, abs=1e-9)


def test_from_similarity_identity_matrix():
    # All-ones matrix (perfectly similar): eigenvalues (n, 0, ..., 0)
    n = 4
    S = [[1.0] * n for _ in range(n)]
    assert vendi_score_from_similarity(S) == pytest.approx(1.0, abs=1e-6)


def test_from_similarity_requires_square():
    with pytest.raises(ValueError):
        vendi_score_from_similarity([[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])


def test_from_similarity_requires_symmetric():
    with pytest.raises(ValueError):
        vendi_score_from_similarity([[1.0, 0.5], [0.9, 1.0]])


# --------------------------------------------------------------------------
# stdlib-only guarantee (no numpy/scipy/sklearn import inside vendi)
# --------------------------------------------------------------------------
def test_no_heavy_dependencies():
    import importlib.util

    spec = importlib.util.find_spec("vendi")
    assert spec is not None
    # vendi lives at repo root; ensure it does not import numpy/scipy/sklearn.
    import vendi as _v

    # Re-parse the module source for forbidden imports.
    import inspect

    src = inspect.getsource(_v)
    for forbidden in ("numpy", "scipy", "sklearn", "sentence_transformers", "torch"):
        assert f"import {forbidden}" not in src
        assert f"from {forbidden}" not in src
