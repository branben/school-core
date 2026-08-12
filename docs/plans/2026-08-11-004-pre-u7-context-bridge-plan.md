---
title: Pre-U7 context and bridge readiness
type: feat
status: active
date: 2026-08-11
origin: docs/plans/2026-08-11-001-feat-pipeline-context-verify-dispatch-plan.md
---

# Pre-U7 context and bridge readiness

## Summary

Finish the remaining bridge and context foundations before implementing FirstMate crew dispatch. This plan lands the already-developed issue-bridge durability behavior, deterministic routing policy, and Layer 3 consolidation write/read path. It deliberately stops before `crew_dispatch.py`, bridge crew wiring, or enabling `CREW_ENABLED`.

## Problem frame

The direct GitHub issue handoff is now landed and tested. The working tree still contains two uncommitted behavioral slices: bridge durability changes and CE routing-policy changes. Beads also identify Layer 3 consolidation write-back as the next P1 foundation before U7. Until these are isolated, tested, and landed, U7 would be built on a mixed snapshot and a pipeline that cannot reliably preserve its context across cycles.

## Scope boundaries

### In scope

- Make bridge dry-run and state-write behavior explicit and regression-tested.
- Land deterministic routing overlays and the full persisted route contract.
- Make existing Layer 3 consolidation write/read artifacts durable by loop session ID, sanitized, and usable from a real fresh git checkout. Runtime generation already exists; this plan adds the workflow checkpoint and safety boundary.
- Update Beads and this plan with evidence of readiness.

### Deferred to U7 and later

- `crew_dispatch.py` and FirstMate lifecycle behavior.
- `CREW_ENABLED` bridge wiring and direct-Orca fallback changes.
- Crew artifact preservation, worktree lifecycle, and live issue proof.
- Broad exception-handling cleanup and unrelated observability refactors.

## Decisions

1. Keep bridge and router commits separate because bridge behavior controls execution/durability while router behavior controls policy selection.
2. Preserve the existing direct path when crew dispatch is absent; this plan does not add a crew dependency.
3. Use the existing Layer 3 sleep/consolidation primitives rather than creating a parallel archival store.
4. Verify Layer 3 with a write in one cycle and a read in a later fresh-cycle context; a passing unit test alone is not sufficient.
5. Do not enable or implement U7 until all units below pass their focused tests and the pre-U7 readiness gate is green. Secret protection is an allowlisted structural scrub plus residual scans; arbitrary unknown secret formats remain outside this plan's guarantee.

## High-level design

```text
bridge cycle(session_id)
  ├─ classify task → deterministic route contract
  ├─ execute existing direct student path
  ├─ write sanitized high-value observations to consolidation/session state
  └─ next fresh cycle(session_id') reads prior consolidation context

bridge dry-run ──> no clone/cache/processed/retry/state writes
route policy  ──> bounded overlays + precedence ──> durable route fields
```

## Implementation units

### U1. Land bridge durability behavior

**Goal:** Make the current bridge dry-run and state-write behavior explicit, safe, and independently reviewable.

**Requirements:** Bridge execution must not create runtime state during dry runs; normal runs must retain existing record/retry behavior.

**Dependencies:** None.

**Files:**

- `issue_bridge.py`
- `tests/test_issue_bridge.py`

**Approach:** Keep the existing bridge interfaces and isolate the dry-run guard at the earliest stateful boundary. Reuse `record_run` and existing atomic persistence helpers. Do not mix routing policy or Layer 3 changes into this unit.

**Execution note:** Test-first characterization of the current dry-run contract, followed by the smallest implementation change.

**Test scenarios:**

- Dry-run with an actionable issue does not clone, remove cache, write processed state, write retry state, or append a run record.
- Dry-run returns the same issue classification/result shape as normal selection.
- Normal non-dry run still records the expected result and preserves existing retry semantics.
- State-writer failure remains visible and does not silently report a successful durable write.

**Verification:** Focused bridge tests pass and the diff contains no router or Layer 3 changes.

### U2. Land deterministic routing policy

**Goal:** Make task-policy overlays deterministic, bounded, and fully persisted in the route contract.

**Requirements:** Route choice must preserve curiosity/human-gate requirements, apply precedence deterministically, cap overlays, and remain backward-compatible for callers that provide only the original task shape.

**Dependencies:** None.

**Files:**

- `scripts/ce_router.py`
- `tests/test_ce_router.py`

**Approach:** Keep the existing base skill selection. Apply policy overlays through one precedence table and return the existing route fields plus bounded overlay metadata. A writer failure must remain non-fatal where the current router contract is best-effort; strict issue-path persistence remains owned by the already-landed conductor code.

**Execution note:** Add failing policy and persistence-contract tests before changing router behavior.

**Test scenarios:**

- A new implementation receives the expected primary route.
- Curiosity and human-gate flags survive route selection.
- Conflicting overlays resolve according to documented precedence.
- Overlay output is capped and deterministic for the same input.
- Plan-review policy replaces or suppresses incompatible overlays.
- Writer failure returns a visible non-logged route result without raising for generic callers.
- Existing minimal task shapes still route successfully.

**Verification:** Focused router tests pass and the route contract matches the conductor consumers.

### U3. Prove Layer 3 consolidation durability

**Goal:** Persist high-value observations by loop session and retrieve them in a later fresh cycle without PII or secrets.

**Requirements:** A cycle writes sanitized consolidation/session state; a later cycle retrieves it; write failures are explicit and do not create false-ready context.

**Dependencies:** U1, U2 only for final integration verification; implementation may proceed independently.

**Files:**

- `director.py`
- `context_orchestrator.py`
- `sleep_state.py`
- `issue_bridge.py`
- `data/consolidation/` seed/index files if required by existing primitives
- `tests/test_director.py`
- `tests/test_context_orchestrator.py`
- `tests/test_sleep_state.py`
- `tests/test_issue_bridge.py`

**Approach:** Thread the existing cycle `session_id` through the bridge and director into `enrich_prompt`. Reuse the existing consolidation writer/reader and sanitize at the persistence boundary. Prove the handoff with a two-cycle test: cycle A writes an observation under its session identity; a fresh cycle B loads it into archival context. Keep Layer 3 optional when no consolidation exists and preserve direct-mode behavior.

**Execution note:** Start with a failing two-cycle integration regression; add lower-level tests only where the existing seams do not cover it.

**Test scenarios:**

- A bridge cycle creates a stable session ID and passes it through the director.
- `enrich_prompt` includes matching archival context when a consolidation exists.
- A fresh process/cycle can read the prior sanitized consolidation.
- Missing consolidation is a safe empty-context result.
- Writer/read failure is visible and does not mark the cycle successful based on missing context.
- Paths, prompts, tokens, and PII are sanitized before persistence.
- Existing calls without a session ID preserve their current behavior.

**Verification:** A temporary git commit/clone plus fresh Python process loads the sanitized archival context, and the persisted fixture contains no home paths or recognized sensitive fields/token formats.

### U4. Pre-U7 readiness gate

**Goal:** Prove the foundation is ready for U7 without beginning crew implementation.

**Dependencies:** U1, U2, U3.

**Files:**

- `docs/plans/2026-08-11-004-pre-u7-context-bridge-plan.md`
- Beads status/notes

**Approach:** Run focused bridge/router/context tests, then the full suite with known environment-sensitive failures isolated and reported. Confirm the working tree is clean or contains only explicitly deferred U7-plan documentation. Confirm direct issue dispatch remains crew-free and `CREW_ENABLED` has not been enabled. Record evidence, close only completed predecessor beads, and leave `school-core-efa` open for the next execution phase.

**Test scenarios:**

- Full relevant test set passes.
- Python compilation and diff hygiene pass.
- No direct path imports or invokes `crew_dispatch`.
- Layer 3 fresh-cycle read proof passes.
- Bridge dry-run and router policy contracts remain green together.

**Verification:** A reviewer can start U7 from a clean, landed base and identify the exact remaining U7 dependencies without relying on unstaged changes.

## Readiness definition

U7 is ready only when:

- U1–U3 are landed as isolated, reviewed commits.
- Focused bridge/router/context tests pass.
- The full suite has no new failures attributable to this plan.
- Layer 3 write/read is proven across separate cycle state.
- No crew code is required to make the pre-U7 suite pass.
- `CREW_ENABLED` remains off until U7/U8/U9 are implemented and explicitly authorized.

## Handoff

After U4 is complete, begin the existing U7–U9 plan at its first standalone lifecycle unit. Do not wire the bridge to FirstMate as part of this plan.
