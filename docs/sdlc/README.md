# SDLC documentation layer

# SDLC documentation layer

The approved SDLC policy (plan:
[`docs/plans/2026-09-13-001-feat-effective-html-sdlc-plan.md`](../plans/2026-09-13-001-feat-effective-html-sdlc-plan.md))
applies a three-tier documentation model ("Proper documentation type") to both
loops — outer (epic → PRD → SPEC → tickets) and inner (Prime → Plan →
Implement → Validate → Review → PR).

| Page | What it is |
|---|---|
| [`loops.md`](loops.md) | **Canonical overview** of the inner + outer loops, phase→form table, session flow |
| [`context-skills.md`](context-skills.md) | The "accurate skills at each phase" layer: ripwire verbs + plannotator authoring skills per phase |
| [`../templates/phase-map.md`](../templates/phase-map.md) | The canonical phase → form → tool mapping table (also linked from the plan) |
| [`../templates/TIER.md`](../templates/TIER.md) | The three-tier documentation model + build/verify contract |

Operational ritual: [`../templates/daily-loop.md`](../templates/daily-loop.md)
(start-of-day: `bd ready` → board refresh → one slice → bead write).
The related Iteration plan to track work on the SDLC layer is
[`docs/templates/slice-tracking-contract.md`](../templates/slice-tracking-contract.md)

This directory holds the SDLC doc layer for school-core. The **tier model** (3 tiers:
disposable viewport → canonical HTML → canonical markdown + HTML render) governs
which vehicle carries each artifact. Gate files in this directory are **pointers**
to their implementation vehicles (skills, CI steps, scripts), not process manual
