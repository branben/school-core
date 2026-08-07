# ADR: ACRouter Outcome-Feedback Routing for OmniRoute Combos

- **Status:** Accepted
- **Paper:** Agent as Router (arXiv:2606.22902) — "tasty paper #4"
- **Task:** P2.1 — Apply ACRouter pattern to OmniRoute combo selection

## Context

`school-core/executor.py` contains `COMBO_MAP`, a static `role -> combo`
mapping (e.g. `coder -> auto/best-free`). It was previously flagged as an
"orphaned" routing layer (architecture doc, Gap F): the live student path did
not consult it, so combo selection never adapted to which models actually
succeed for a given role.

The ACRouter pattern reframes routing itself as an *experience-gathering agent*:
every routing decision produces an outcome (success + quality), and that
experience biases future selections. This is a multi-armed bandit over combo
choice — exactly the right tool because (a) we have a small discrete action
space (the candidate combos in `COMBO_MAP`), (b) outcomes are noisy (model
quality varies per task), and (c) we want to keep exploring while exploiting.

## Decision

Implement an epsilon-greedy bandit in a new module, `router_experience.py`:

- `RouterExperience` holds per-`(context, combo)` stats (`trials`,
  `successes`, quality sum) persisted to `data/router_experience.json`.
- `select_combo(context)` returns the experience-best combo, falling back to
  the static `COMBO_MAP` (via `default_resolver`) on cold start. With
  probability `exploration_rate` (default 0.15) it explores a random candidate.
- `record_outcome(context, combo, success, quality)` feeds the result back.
  `value = success_rate*(1-w) + quality*w` (w=0.5), tie-broken by trial count.

Wiring:

- `executor.call_model` now calls `select_combo(agent_name)` instead of a raw
  `COMBO_MAP.get`. The chosen combo is remembered so the outcome can be
  recorded against it.
- `director.run_task` records the routing outcome after the two-judge review:
  `success = review["accepted"]`, `quality = task_score/100`. A hard routing
  failure (primary + A2A fallback both error) records `success=False,
  quality=0`. Recording is wrapped in try/except so feedback is best-effort and
  never breaks the task pipeline.

Configuration (env vars, all optional):

- `ROUTER_EXPERIENCE_PATH`: path to the persistence file, or `""` for
  in-memory (used by tests — keeps the repo `data/` dir clean under pytest).
- `ROUTER_EXPLORATION_RATE`: epsilon in [0, 1] (default 0.15).

## Consequences

- Positive: combo selection now learns from real outcomes; `COMBO_MAP` is no
  longer dead/orphaned code but serves as a safe cold-start prior. Cheap to
  extend to per-`(role, domain)` contexts.
- Negative: introduces a persistent state file (`data/router_experience.json`)
  that grows with usage. The router's choices become non-deterministic under
  exploration — acceptable for a learning router, but tests pin
  `exploration_rate=0` where determinism matters.
- Backward compatible: with no recorded experience, behaviour is identical to
  the old static `COMBO_MAP`.

## Tests

- `tests/test_router_experience.py` — bandit selection, cold-start fallback,
  persistence/reload, candidate derivation.
- `tests/test_acrouter_executor.py` — executor wiring: cold-start honours
  `COMBO_MAP`, outcome round-trips through the router.
