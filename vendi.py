"""Vendi Score — a dependency-free diversity metric for model outputs.

The Vendi Score (Wang & Blei, 2023; arXiv:2304.14970) measures the effective
number of *distinct* items in a set, given only a similarity function between
pairs. It is the exponential of the Shannon entropy of the normalized
eigenvalues of a similarity matrix S:

    λ = eigenvalues(S) / trace(S)
    V = exp(-Σ λ_i log λ_i) = exp(H(λ))

Properties:
- V == 1.0 when every item is identical (diversity collapse).
- V == n   when every pair is perfectly dissimilar (max diversity).
- V generalizes "effective number of species" to arbitrary similarities, so
  it does not require a ground-truth label set the way entropy-over-clusters
  would.

This module depends only on the Python standard library so it can run inside
the school-core harness without numpy/scipy. The dominant eigenvalue of the
symmetric similarity matrix is found with Jacobi eigenvalue iteration, which
is numerically stable for the small, dense, positive-semidefinite matrices
this metric produces.

Typical use (text outputs):

    from vendi import vendi_score
    score = vendi_score(student_outputs)            # char n-gram Jaccard
    score = vendi_score(student_outputs, ngram_size=3)

Or supply a precomputed similarity matrix (e.g. embedding cosine):

    from vendi import vendi_score_from_similarity
    score = vendi_score_from_similarity(cosine_matrix)
"""
from __future__ import annotations

import math
from typing import Callable, List, Sequence


# --------------------------------------------------------------------------
# Text similarity
# --------------------------------------------------------------------------
def _char_ngrams(text: str, n: int) -> List[str]:
    """Return the list of length-``n`` character shingles of ``text``.

    A short string yields a single shingle equal to the whole string so that
    two short strings still have a non-degenerate Jaccard denominator.
    """
    if n < 1:
        raise ValueError("ngram_size must be >= 1")
    text = text or ""
    if len(text) <= n:
        return [text]
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    """Jaccard similarity of two shingle multisets, in [0, 1]."""
    if not a and not b:
        return 1.0
    set_a = set(a)
    set_b = set(b)
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


def pairwise_similarity(
    texts: Sequence[str],
    ngram_size: int = 3,
    similarity: Callable[[Sequence[str], Sequence[str]], float] = _jaccard,
) -> List[List[float]]:
    """Build the symmetric similarity matrix for a list of texts.

    The diagonal is 1.0 (every text is perfectly similar to itself). The
    matrix is symmetric by construction.
    """
    shingles = [_char_ngrams(t, ngram_size) for t in texts]
    n = len(texts)
    S: List[List[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        S[i][i] = 1.0
        for j in range(i + 1, n):
            s = similarity(shingles[i], shingles[j])
            S[i][j] = s
            S[j][i] = s
    return S


# --------------------------------------------------------------------------
# Linear algebra (stdlib only)
# --------------------------------------------------------------------------
def _jacobi_eigenvalues(
    A: List[List[float]], max_sweeps: int = 100, tol: float = 1e-12
) -> List[float]:
    """Return the eigenvalues of a real symmetric matrix via Jacobi rotation.

    Only the eigenvalues are needed for the Vendi Score, so off-diagonal
    annihilation is tracked implicitly through the shrinking Frobenius norm of
    the off-diagonal part. The returned values are sorted descending and are
    non-negative for positive-semidefinite input (which a similarity matrix is).
    """
    n = len(A)
    # Work on a deep copy; we only need the diagonal at the end.
    M = [row[:] for row in A]

    for _ in range(max_sweeps):
        # Largest off-diagonal magnitude.
        p = 0
        q = 1
        max_off = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                aij = abs(M[i][j])
                if aij > max_off:
                    max_off = aij
                    p, q = i, j
        if max_off < tol:
            break

        # Rotate (p, q) pair toward zero.
        app = M[p][p]
        aqq = M[q][q]
        apq = M[p][q]
        phi = 0.5 * math.atan2(2.0 * apq, aqq - app)
        c = math.cos(phi)
        s = math.sin(phi)

        # Update only the rows/cols that change (p and q).
        for k in range(n):
            if k == p or k == q:
                continue
            mpk = M[p][k]
            mqk = M[q][k]
            M[p][k] = c * mpk - s * mqk
            M[q][k] = s * mpk + c * mqk
            M[k][p] = M[p][k]
            M[k][q] = M[q][k]
        # Diagonal + pivot cell (computed last to avoid clobbering inputs).
        M[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        M[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        M[p][q] = 0.0
        M[q][p] = 0.0

    return [M[i][i] for i in range(n)]


def _normalized_eigenvalues(S: List[List[float]]) -> List[float]:
    """Eigenvalues of ``S`` normalized to a probability distribution over its trace."""
    n = len(S)
    if n == 0:
        return []
    # Validate square + symmetric.
    for i in range(n):
        if len(S[i]) != n:
            raise ValueError("similarity matrix must be square")
        for j in range(i + 1, n):
            if abs(S[i][j] - S[j][i]) > 1e-9 * max(1.0, abs(S[i][j]), abs(S[j][i])):
                raise ValueError("similarity matrix must be symmetric")

    eigvals = _jacobi_eigenvalues(S)
    trace = sum(eigvals)
    if trace <= 0:
        # Degenerate (all-zero) matrix: no similarity signal.
        return [1.0 / n] * n
    # Clip tiny negatives from numerical noise to keep the log well-defined.
    clipped = [max(ev, 0.0) for ev in eigvals]
    s = sum(clipped)
    return [ev / s for ev in clipped]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def vendi_score_from_similarity(S: Sequence[Sequence[float]]) -> float:
    """Vendi Score from a precomputed symmetric similarity matrix ``S``.

    Returns 0.0 for an empty matrix (undefined) and 1.0 when ``S`` is a single
    element (one identity).
    """
    if len(S) == 0:
        return 0.0
    if len(S) == 1:
        return 1.0

    probs = _normalized_eigenvalues([list(row) for row in S])
    # Shannon entropy of the eigenvalue distribution; exp() maps it to the
    # effective number of distinct items.
    entropy = -sum(p * math.log(p) for p in probs if p > 0.0)
    return math.exp(entropy)


def vendi_score(
    texts: Sequence[str],
    ngram_size: int = 3,
    similarity: Callable[[Sequence[str], Sequence[str]], float] = _jaccard,
) -> float:
    """Vendi Score for a collection of text outputs.

    Convenience wrapper: builds the char-n-gram Jaccard similarity matrix then
    computes the Vendi Score. Larger ``ngram_size`` = coarser comparison.
    """
    if len(texts) == 0:
        return 0.0
    if len(texts) == 1:
        return 1.0
    S = pairwise_similarity(texts, ngram_size=ngram_size, similarity=similarity)
    return vendi_score_from_similarity(S)
