# ADR 0007: Isolated Reasoning Phases (Diversity Collapse Fix)

**Status:** Accepted
**Date:** 2026-08-06

## Context

"Diversity Collapse" (arXiv:2604.18005) shows that when every student in a
batch is handed the *same* shared context, their independent reasoning
converges to identical outputs. In the school-core dispatch path
(`director.run_task`), this manifests because a single enriched
`system_prompt` — built once from the vault (CocoIndex), Engram trajectories,
Serena symbols, semantic anchors, and prior-trajectory blobs — is reused for
the single role attempt. When the framework fans a task out to several student
roles, each role receives the same preamble and collapses to the same answer.

The "wow-thats-tasty" meal plan (main course #3) names this exact remedy:
**isolated reasoning for students**, measured with the **Vendi Score**
(Wang & Blei, 2023) as the diversity metric.

## Decision

Add two stdlib-only modules and a dispatch toggle:

- **`vendi.py`** — pure-Python Vendi Score. Computes eigenvalues of the
  symmetric similarity matrix via Jacobi iteration (no numpy/scipy), then
  `exp(Shannon entropy of normalized eigenvalues)`. V=1 ⇒ collapse, V=n ⇒
  maximally diverse. Char-n-gram Jaccard similarity is the default text
  kernel; a precomputed-matrix path (`vendi_score_from_similarity`) supports
  embedding cosine matrices.
- **`isolated_reasoning.py`** — `run_isolated_phases(...)`:
  * `select_context_blocks` derives a **deterministic, per-student** subset of
    the shared context (seeded by `(student_id, seed)` — shuffle + stochastic
    drop), so two students never see the same context slice;
  * each student reasons in its own `build_isolated_prompt` phase with ONLY
    its isolated blocks (no shared preamble);
  * the batch's Vendi Score is computed and a `collapsed` flag raised when
    V≈1;
  * the **medoid** (output closest to all others by similarity) is promoted
    as the representative `response`, not student #0.
- **`director.run_task(isolated_phases=True, ...)`** — new branch that runs
  `phase_students` roles through `isolated_reasoning` and returns
  `vendi_score`, `collapsed`, `selected_student`, `response`, and
  `phase_responses`. It logs the diversity decision to the Decision Log. The
  branch skips the bookbag/two-judge stage so it composes with the caller's
  own review.

### Why Jacobi (not numpy.linalg.eigh)
The repo's venv ships **no** numpy/scipy/sklearn, and the harness must run
without heavy deps. The similarity matrices here are small, dense, and
positive-semidefinite; Jacobi eigenvalue iteration is numerically stable for
that case and keeps the module import-safe everywhere.

## Consequences

- Positive: a real, measurable diversity gate now exists; dispatch can detect
  collapse (V≈1) and prefer the isolated-phases path when diversity matters.
- Positive: fully testable without an LLM — 28 unit/integration tests cover
  edge cases (empty, single, identical, near-identical, medoid selection,
  determinism, stdlib-only guarantee).
- Positive: no new dependencies; drops cleanly into the existing `run_task`
  signature as an opt-in flag (default `False`, so existing behavior is
  unchanged).
- Negative: drop_rate > 0 means a student may occasionally receive less
  context than the full shared pool — acceptable because the goal is
  decoupling, and the medoid selection guards against any single degraded run.
- Follow-up: wire `run_task(isolated_phases=True)` into the conductor's
  multi-student fan-out and surface `vendi_score`/`collapsed` in the activity
  dashboard.
