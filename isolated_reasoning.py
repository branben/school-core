"""Isolated reasoning phases for Hermes students (Diversity Collapse fix).

Paper: *Diversity Collapse* (arXiv:2604.18005). When every student is handed
the SAME shared context, their reasoning converges to identical outputs — the
this is the "diversity collapse" failure mode. The remedy (per the paper and
the school-core "wow-thats-tasty" meal plan, main course #3) is to run each
student in an *isolated* reasoning phase whose context is decoupled from the
others, so independent reasoning paths emerge, and then measure how diverse the
outputs actually are with the Vendi Score.

What this module provides:

- ``select_context_blocks`` — deterministically derives a per-student subset
  of the shared context blocks, seeded by ``(student_id, seed)`` so two
  students with different seeds see *different* context slices. This breaks the
  "identical context -> identical reasoning" feedback loop without discarding
  information outright (we drop a stochastic fraction, and additionally
  re-order / permute which blocks each student sees).

- ``run_isolated_phases`` — runs N students, each in its own phase, with its
  own isolated prompt, and computes:
      * the raw responses,
      * the Vendi Score of the responses (effective number of distinct outputs),
      * a ``collapsed`` flag (Vendi ~= 1 means all outputs converged),
      * the *medoid* response — the output closest to all the others — as the
        representative to promote (so we don't just pick student 0).

- ``build_isolated_prompt`` — assembles a phase prompt from the task plus the
  student's isolated context block, with a stable per-student tag so the
  reasoning is anchored to its own context (not a shared preamble).

The module is stdlib-only (no numpy/scipy) and depends on :mod:`vendi` for the
diversity metric. It does NOT call any LLM itself — the caller injects a
``reason_fn(student_id, prompt, seed) -> str`` so this stays testable and can
be wired to ``director.call_model`` in production (see director integration).
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from vendi import vendi_score


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------
@dataclass
class PhaseResult:
    """One student's isolated reasoning phase."""

    student_id: str
    seed: int
    context_blocks: Dict[str, str]
    prompt: str
    response: str


@dataclass
class IsolationResult:
    """Aggregate outcome of a batch of isolated reasoning phases."""

    phases: List[PhaseResult]
    vendi_score: float
    collapsed: bool
    selected_student: Optional[str] = None
    selected_response: Optional[str] = None
    diversity_threshold: float = 1.0


# --------------------------------------------------------------------------
# Context isolation
# --------------------------------------------------------------------------
def _stable_int(*parts: str) -> int:
    """Deterministic 32-bit integer from string parts (no hash() randomness)."""
    h = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def select_context_blocks(
    base_blocks: Dict[str, str],
    student_id: str,
    seed: int,
    drop_rate: float = 0.5,
) -> Dict[str, str]:
    """Derive a per-student, decoupled subset of ``base_blocks``.

    Two students with different ``seed``/``student_id`` see differently
    shuffled + partially-dropped context, which is what prevents the shared
    preamble from forcing every reasoning path to the same place. The result is
    a fresh ``dict`` every call (no shared references that could leak between
    phases), so mutation in one phase never affects another.
    """
    if not base_blocks:
        return {}

    rng = random.Random(_stable_int(student_id, str(seed)))
    keys = list(base_blocks.keys())
    rng.shuffle(keys)  # Different order per student -> different reasoning anchor.

    kept: Dict[str, str] = {}
    for k in keys:
        if rng.random() >= drop_rate:
            kept[k] = base_blocks[k]
    return kept


def build_isolated_prompt(
    task_prompt: str,
    context_blocks: Dict[str, str],
    student_id: str,
) -> str:
    """Assemble one student's fully-isolated phase prompt.

    The prompt is anchored to THIS student's own context blocks only — it never
    references the other students' context, which is what keeps the reasoning
    phase decoupled.
    """
    lines = [f"# Isolated reasoning phase — student {student_id}", ""]
    if context_blocks:
        lines.append("## Your context (isolated to this phase)")
        for key, value in context_blocks.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    lines.append("## Task")
    lines.append(task_prompt)
    lines.append("")
    lines.append(
        f"Reason from the context above as student {student_id}. "
        "Do not assume any context other than what is listed here."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Medoid selection
# --------------------------------------------------------------------------
def _pairwise_jaccard(texts: Sequence[str], ngram_size: int = 3) -> List[List[float]]:
    """Char-n-gram Jaccard similarity matrix (mirrors vendi.pairwise_similarity)."""

    def shingles(t: str) -> set:
        t = t or ""
        if len(t) <= ngram_size:
            return {t}
        return {t[i : i + ngram_size] for i in range(len(t) - ngram_size + 1)}

    sets = [shingles(t) for t in texts]
    n = len(texts)
    S: List[List[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        S[i][i] = 1.0
        for j in range(i + 1, n):
            union = sets[i] | sets[j]
            s = (len(sets[i] & sets[j]) / len(union)) if union else 1.0
            S[i][j] = s
            S[j][i] = s
    return S


def _select_medoid(responses: Sequence[str]) -> int:
    """Index of the medoid (response with the highest mean similarity to all)."""
    if len(responses) <= 1:
        return 0
    S = _pairwise_jaccard(responses)
    best_idx, best_mean = 0, -1.0
    for i in range(len(responses)):
        mean_sim = sum(S[i]) / len(responses)  # includes self (1.0)
        if mean_sim > best_mean:
            best_mean = mean_sim
            best_idx = i
    return best_idx


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run_isolated_phases(
    task_prompt: str,
    students: Sequence[str],
    base_blocks: Dict[str, str],
    reason_fn: Callable[[str, str, int], str],
    seeds: Optional[Sequence[int]] = None,
    drop_rate: float = 0.5,
    diversity_threshold: float = 1.05,
    ngram_size: int = 3,
) -> IsolationResult:
    """Run each student in an isolated reasoning phase and score output diversity.

    Args:
        task_prompt: the shared task all students tackle.
        students: student identifiers (models/agents) to run.
        base_blocks: shared context pool the phases are decoupled FROM.
        reason_fn: ``(student_id, prompt, seed) -> str`` — the actual reasoning
            call. Inject ``director.call_model`` here in production.
        seeds: optional per-student seed; defaults to a deterministic index
            seed so the run is reproducible without the caller supplying seeds.
        drop_rate: fraction of context blocks to drop per phase (0 = all
            students see everything, which re-introduces collapse risk).
        diversity_threshold: Vendi Score at/above which we consider the batch
            *not* collapsed. 1.05 leaves a tiny epsilon for float noise.
        ngram_size: granularity for the Vendi text-similarity measurement.

    Returns an :class:`IsolationResult` with responses, Vendi Score, a
    ``collapsed`` flag, and the medoid (most representative) output to promote.
    """
    students = list(students)
    if seeds is None:
        seeds = list(range(len(students)))
    if len(seeds) != len(students):
        raise ValueError("len(seeds) must equal len(students) when provided")

    phases: List[PhaseResult] = []
    for student_id, seed in zip(students, seeds):
        blocks = select_context_blocks(
            base_blocks, student_id, seed, drop_rate=drop_rate
        )
        prompt = build_isolated_prompt(task_prompt, blocks, student_id)
        response = reason_fn(student_id, prompt, seed)
        phases.append(
            PhaseResult(
                student_id=student_id,
                seed=seed,
                context_blocks=blocks,
                prompt=prompt,
                response=response,
            )
        )

    responses = [p.response for p in phases]
    score = vendi_score(responses, ngram_size=ngram_size) if responses else 0.0
    collapsed = score < diversity_threshold

    selected_student: Optional[str] = None
    selected_response: Optional[str] = None
    if responses:
        idx = _select_medoid(responses)
        selected_student = phases[idx].student_id
        selected_response = responses[idx]

    return IsolationResult(
        phases=phases,
        vendi_score=score,
        collapsed=collapsed,
        selected_student=selected_student,
        selected_response=selected_response,
        diversity_threshold=diversity_threshold,
    )
