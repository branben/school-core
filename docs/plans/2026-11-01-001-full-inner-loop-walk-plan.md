---
bead:     school-core-yexd
spec:     docs/plans/2026-10-30-001-inner-loop-source-linkage-plan.md · milestone-2
decisions: D1
prev:     docs/plans/2026-10-30-001-inner-loop-source-linkage-plan.md
---

# Full inner-loop walk: close remaining gaps

## Goal

Walk an issue from outer loop (bead) through inner loop (Prime → Plan → Implement → Validate → Review → PR) to merged PR with verified source chain. The CI/grade spine works; the execution half and artifact generation are the gaps.

## Current state

| Phase | Status |
|-------|--------|
| PRIME | Template ready; source block enforced |
| PLAN | Template ready; source block enforced |
| BUILD | `dev-log.md` template exists; not auto-populated |
| TEST | Template ready; source block enforced |
| REVIEW | `guide_gen.py` produces `guide.json`; `review_gate.py` validates |
| PR | `pr_creator.py` renders PR body with source chain; `pr-writeup.html` is a placeholder |

## Gaps remaining

1. **`source.json` generator** — REVIEW outputs `guide.json` only. Nothing produces `source.json` from the actual ticket/spec/artifacts. Hand-written placeholders are the current state.
2. **`pr-writeup.html` generator** — The Tier-3 PR artifact is never rendered. `html_render_md.py` exists but isn't called with source-chain content.
3. **Execution half (SCH-32b)** — crew → Orca worktree → bookbag → PR path untested end-to-end. The grading spine works in isolation.

## Milestones

### M1: `source.json` generator

Write `scripts/source_gen.py` that reads the bead, spec, and four prior artifact paths and emits `docs/source.json`.

**Inputs:** bead ID, spec file path, decisions list, artifact paths
**Output:** `docs/source.json` (validated against the template schema)
**Verify:** unit test that round-trips through `review_gate.py` check #5

**Risky seam:** the generator must not invent artifact paths — it must read them from the actual `docs/` tree, not template defaults.

### M2: `pr-writeup.html` renderer

Extend `scripts/html_render_md.py` (or add a thin wrapper) to render `pr-writeup.md` from `source.json` + PR body evidence.

**Inputs:** `source.json`, review evidence, verify result
**Output:** `docs/pr-writeup.html`
**Verify:** rendered output contains all five source-chain fields

**Risky seam:** the render must not drop fields when `source.json` is incomplete. Fail-closed on missing fields.

### M3: Wire generators into REVIEW → PR handoff

Update `crew_dispatch.py` dispatch template (or `issue_bridge.py`) to invoke `source_gen.py` after REVIEW completes and `html_render_md.py` before PR creation.

**Verify:** guard test that asserts both scripts are called in order
**Integration:** `dispatch_template.py` already validates premises before `_spawn`; extend with artifact-generation premises

### M4: Execution half smoke test

Run one real issue through the full inner loop with crew dispatch enabled. Record where it fails.

**Success criterion:** a PR is opened with both `source.json` and `pr-writeup.html` in the diff, and `review_gate.py` check #5 passes on that PR.

**Known blocker (SCH-32b):** VM stack not wired into live dispatch. This milestone may surface additional infra gaps.

## Sequence

M1 → M2 → M3 → M4. Each milestone is independently reviewable and verifiable. M4 is the integration gate — if it fails, the defect is in execution, not in the generators.

## Open questions

- Who owns `source.json` generation: REVIEW phase (plannotator-guide) or a separate generator script? Recommend script — REVIEW already produces `guide.json`; adding sidecar production there couples two outputs.
- Should `pr-writeup.html` be regenerated on every PR, or only when `source.json` changes? Recommend always — it's cheap and keeps the artifact fresh.
