---
title: Pipeline Gaps — 4-Layer Context, Verify Gate, and FirstMate Dispatch
type: feat
status: active
date: 2026-08-11
deepened: 2026-08-11
origin: .scratch/wayfinder-map-pipeline-gaps.md
---

# Pipeline Gaps — 4-Layer Context, Verify Gate, and FirstMate Dispatch

**Date:** 2026-08-11
**Status:** Active
**Plan type:** feat
**Depth:** Deep

## Summary

Close the five verified gaps between what the school-loop issue path exercises today and the full pedagogical stack: thread a per-cycle `session_id` so Layer 3 archival fires, checkpoint-commit sanitized trajectories so Layer 2 has history on fresh checkouts, make the verify gate loud instead of silently inert, dispatch student tasks through the proven FirstMate crew, bring the Entire pre-merge review into the live path, and correct campus.md's overclaims. Direct-Orca execution and the CTO/COO review remain the fallback and the unchanged upper half of the pipeline.

## Problem Frame (historical baseline, before the 2026-08-11 reconciliation)

The wayfinder map (`.scratch/wayfinder-map-pipeline-gaps.md`) confirmed on disk that the live issue path (`issue_bridge` → `director.run_task` → review → score) exercises only a thin slice of the system:

1. **Layer 3 is dead.** `enrich_prompt` includes archival context only when `session_id` is set (`context_orchestrator.py:78`), and `run_task` already carries the parameter (`director.py:582`) — but drops it at the `enrich_prompt` call (`director.py:714`), and the bridge never passes one (`issue_bridge.py:475`). `data/consolidation/` does not exist.
2. **Layer 2 is empty on every fresh checkout.** `data/trajectories/` is not tracked; each school-loop cycle starts in a fresh checkout with zero trajectory history, so the file-RAG no-ops.
3. **Compiler-before-critic was decorative at discovery time.** `verify_gate.py:160` shells out to `nix develop flake.nix#verifyShell`; before the 2026-08-11 runner/toolchain fix, Nix was not installed on the Mac runner, so the gate silently returned the non-blocking `None` path every cycle. The current production preflight policy is documented below.
4. **The crew engine is unplugged.** The FirstMate→Orca→Hermes cycle is proven end-to-end (live drill, 2026-08-11) but never invoked from the issue path; campus.md claims it is wired.
5. **Docs lie.** The campus.md Operational Reality table marks FirstMate and the verify gate as "✅ Wired".
6. **Entire review is documented but dormant.** `src/qodo_pre_merge.py:133` runs `entire review` as a pre-merge sensor (`conductor.py:341-359`), but only in the async conductor path — never in the school-loop's sync path — and skips in practice: `_get_entire_path()` is a bare `shutil.which("entire")` (`src/qodo_pre_merge.py:40-42`), so Orca worktree shells without `~/.local/bin` on PATH return "skipped". school-core has no `.entire/` checkpoint tracking (the intent-aware half), while sound-royale-* repos do.

## Requirements

### Context layers

- R1. The bridge threads a per-cycle `session_id` through `director.run_task`, which forwards it to `enrich_prompt`, so Layer 3 archival context is included when a consolidation exists for that session.
- R2. `data/trajectories/` is committed to git, sanitized via the existing checkpoint pattern, so fresh school-loop checkouts see trajectory history.
- R3. `data/consolidation/` is seeded (directory plus initial index) so the Layer 3 write path has a home.

### Verify gate

- R4. The production school-loop preflight treats missing Nix or an unevaluable `flake.nix#verifyShell` as a visible infrastructure failure and exits the execute job; the hosted board-publishing job still runs from committed state. The reusable Python gate returns a visible soft-skip for direct/manual callers instead of silently returning `None`.
- R5. The verify gate runs hermetically via the flake whenever the toolchain is present; Determinate Nix is the required Mac-runner prerequisite (a uv fallback would be a separate future policy/code change).
- R6. `VERIFY_GATE_STRICT=1` makes an unrunnable reusable gate fatal at the issue level, including no discovered verify commands or an internal gate exception. This is separate from the workflow preflight, which is already hard-fail.

### Dispatch

- R7. The student-task path dispatches through FirstMate (`fm-spawn`, orca backend) and reads back the deliverable (`report.md` + terminal status line) when crew dispatch is enabled.
- R8. On crew spawn failure, grace-expired `blocked` state, or poll timeout, the bridge falls back to direct-Orca in the same cycle; if that also fails, the existing retry-once semantics carry the issue.
- R9. CTO/COO two-judge review and scoring run unchanged on the crew deliverable.

### Docs

- R10. campus.md's Operational Reality table reflects actual wiring: FirstMate = wired behind a flag, the school-loop verify preflight = hard-fail on missing infrastructure while the reusable gate remains visible soft-skip unless strict mode is enabled, Layer 3 = live.

### Pre-merge review (Entire)

- R11. Entire runs as a non-blocking pre-merge sensor in the live issue path, before the two-judge review, with findings surfaced on the issue's board/comment and cycle log.
- R12. The entire CLI is discoverable in worktree contexts and school-core has session tracking enabled (`.entire/`), so the intent-aware review is active rather than skipped.
- R13. The stale `qodo_` naming is removed (module/function renamed to `entire_`), keeping the bookbag-compatible return shape.

## Key Technical Decisions

### KTD-1: Thread `session_id`, don't build a new session system

**Decision:** Derive a per-cycle `session_id` (e.g. `loop-<YYYYMMDD-HHMM>`) in the bridge, pass it through `run_task`, and forward it at the `enrich_prompt` call site.

**Rationale:** The plumbing exists end to end — `run_task` has the parameter (`director.py:582`), the orchestrator gates on it (`context_orchestrator.py:78`) and has `_archival_context`. The work is two pass-throughs plus a caller, not new session machinery.

### KTD-2: Trajectory durability reuses the checkpoint pattern

**Decision:** Add `data/trajectories/` to the existing sanitize + `git add -f` + `[skip ci]` checkpoint in school-loop.yml (`.github/workflows/school-loop.yml:154-173`), capping history to the last N cycles or per-cycle summaries.

**Rationale:** Identical pattern to scores/retry, already proven in production. Trajectories are the Layer 2 corpus; without them every fresh checkout is amnesiac. The cap keeps git history bounded.

### KTD-3: Separate workflow infrastructure failure from reusable gate soft-skip

**Decision:** The production school-loop preflight fails the execute job when Nix or `flake.nix#verifyShell` is unavailable. The hosted `loop` job remains independent and publishes the last committed board state. The reusable `verify_gate.py` API remains visible soft-skip by default for direct/manual callers; `VERIFY_GATE_STRICT=1` escalates that result to an issue failure.

**Rationale:** The scheduled production path promises compiler-before-critic, so it must not process issues while the compiler environment is absent. The library-level soft-skip remains useful for tests, diagnostics, and callers that choose graceful degradation; it is explicit and never mistaken for a passed verification.

### KTD-4: FirstMate wraps the student-task path behind a flag

**Decision:** New `crew_dispatch.py` scaffolds a brief, runs `fm-spawn --backend orca` (scout mode for the pilot), polls the status file, reads `report.md`, and tears down with the documented orca-bug workaround. Enabled by `crew_enabled` (default on in school-loop, off in the unit-test env).

**Rationale:** The crew cycle is proven; a flag keeps the 30+ bridge tests hermetic (they patch `director.run_task`). Direct-Orca stays as fallback so retry-once carries any crew failure. Review lenses are untouched because the deliverable shape (`report.md`) feeds the same review.

### KTD-5: Docs truth lands with the code it describes

**Decision:** campus.md rows are corrected in the same change as the wiring they describe.

**Rationale:** The map proved two "✅ Wired" rows were false. Docs that overclaim cost the next on-call real debugging time.

### KTD-6: Entire is a non-blocking sensor, not a gate

**Decision:** Keep the degrade-gracefully semantics; fix discovery and enablement instead of making Entire blocking.

**Rationale:** Same policy as KTD-3 — a missing CLI must never kill the school cycle. Entire catches mechanical issues (unused vars, type narrowing) the LLM judges miss, so its findings inform them; it doesn't veto them.

### KTD-7: Crew deliverable enters through `run_task`, not around it (deepened 2026-08-11)

**Decision:** `run_task` gains an optional `provided_student_output` param; when set, it substitutes for the internal student `call_model` (`director.py:649`), and the two-judge review + scoring run unchanged on it. Crew-done issues call `run_task(..., provided_student_output=report_text)`; crew failures call plain `run_task` (direct-Orca fallback).

**Rationale:** Review and scoring live *inside* `run_task` (`director.py:954`), so "skip run_task and keep the review" is not achievable without duplicating that machinery. A single optional param keeps the bridge's call shape, makes R9 literally true, and is testable in isolation — same pattern as the existing `skip_review`/`skip_readiness` params.

### KTD-8: In-flight registry prevents duplicate crew dispatch (deepened 2026-08-11)

**Decision:** `crew_dispatch` persists every spawn to `data/crew_runs.json` (`crew_id`, issue, `orca_worktree_id`, status, started_at), committed via the checkpoint pattern like `data/retry_issues.json`. The bridge skips issues with a `running` record; a next-spawn sweep removes stale entries via `orca worktree rm`.

**Rationale:** The cron cadence (`*/5`) is shorter than a crew task (up to 15 min), so overlapping cycles are the norm, not the exception. Without a registry, two cycles crew-dispatch the same issue and both poll GitHub/AgentMail — the duplicate-work and API-limit concern. The registry makes crew dispatch exactly-once across cycles with zero extra API calls (status polling is local file reads).

## High-Level Technical Design

Target issue path after all units:

```
GitHub issue
  └─ issue_bridge.process
       ├─ clone_repo + build_codebase_context        (thin context, unchanged)
       ├─ crew_dispatch (U7–U9, crew_enabled):       (new)
       │     brief → fm-spawn (orca) → poll status → report.md → teardown
       │     └─ done ─▶ run_task(..., provided_student_output=report)  [KTD-7]
       │     └─ fail ─▶ plain run_task (direct-Orca fallback, R8)
       ├─ director.run_task(prompt, domain, difficulty, session_id=cycle_id)   [U1]
       │     └─ enrich_prompt(..., session_id)
       │           ├─ L0 ccc search        (present on Mac, no-ops elsewhere)
       │           ├─ L1 serena LSP        (present on Mac, no-ops elsewhere)
       │           ├─ L2 trajectories RAG  (now seeded from git)              [U2]
       │           └─ L3 archival context  (now fires)                        [U1]
       ├─ _run_verify_gate                 (loud WARN / strict fatal)          [U3]
       ├─ Entire pre-merge sensor          (non-blocking, findings surfaced)    [U6]
       ├─ _run_two_judge_review CTO+COO    (unchanged, runs on crew deliverable)
       └─ evaluate_and_update → scores.json + trajectories → checkpoint        [U2]
```

Crew lifecycle (U7–U9), with all external effects behind `crew_dispatch`:

```
    bridge ─run(crew_dispatch)─▶ scaffold brief ─▶ fm-spawn --backend orca --scout
       │                          (FM_HOME/data/<id>/brief.md)         │
       │ ◀── CrewResult ◀───────── poll $STATE/<id>.status               │
       │      (status, report_path,   working:|resolved: → keep polling   │
       │       teardown_ok)           paused: → keep polling (self-clear) │
       │      └─ data/crew_runs.json  blocked:|needs-decision: → 60s grace │
       │         in-flight registry   done:|failed: → terminal            │
       │         + next-spawn sweep   (timeout 15 min, interval ~15s)     │
       │         + orca worktree rm   └─ read FM_HOME/data/<id>/report.md │
       ├─ done ──▶ run_task(..., provided_student_output=report)   [U8 seam]
       └─ CrewUnavailableError / blocked / timeout ─▶ plain run_task (direct-Orca) ─▶ retry-once
```

## Implementation Units

### U1. Thread `session_id` through the issue path — R1, R3

- `issue_bridge.py` (~:475): derive a per-cycle `session_id`, pass to `run_task`.
- `director.py` (:714): forward `session_id` into `enrich_prompt`.
- `context_orchestrator.py`: accept `session_id` on `enrich_prompt` and call `_archival_context` when set.
- Seed `data/consolidation/` (directory + initial index, tracked).
- Tests: bridge passes the session id; `run_task` forwards it; `enrich_prompt` returns archival context when a consolidation exists for that session.

### U2. Trajectory durability — R2

- `.github/workflows/school-loop.yml`: add `data/trajectories/` to the sanitize list and the `git add -f` checkpoint; the checkpoint step trims stale trajectory files (keep the last N cycles) before committing.
- `scripts/sanitize_data.py`: confirm trajectory files are scrubbed by the existing PII regexes.
- Tests: sanitize round-trip on a trajectory fixture that includes a `/Users/<name>` path (the PII class that leaked before); checkpoint step trims stale trajectories and references the glob.

### U3. Verify gate loudness + runner toolchain — R4, R5, R6

- `verify_gate.py`: detect the Nix binary; when absent or no commands are discoverable, return a visible soft-skip by default and honor `VERIFY_GATE_STRICT=1` as an issue-level fatal escalation.
- `.github/workflows/school-loop.yml`: hard-fail the execute preflight when Nix or `flake.nix#verifyShell` is unavailable; keep the hosted board job independent so it still publishes committed state.
- Ops: Determinate Nix is a required prerequisite on the Mac runner. A uv fallback is not part of the current acceptance bar and would require a separate design.
- Tests: gate soft-skip and strict escalation remain covered; workflow structure asserts both hard preflight exits and independent board publishing.

### U4. FirstMate dispatch in the issue path — R7, R8, R9 (umbrella)

Umbrella unit for the crew path, split into claimable units **U7–U9** on
2026-08-11 (bead `school-core-0x9.4` tracks the umbrella; sub-beads track
U7–U9). The crew cycle itself is proven standalone (live drill 2026-08-10;
`scripts/fm_doctor.sh` preflight already wired into the execute job); these
units wire it into `issue_bridge` behind `crew_enabled` (default on in
school-loop, off in the unit-test env) with direct-Orca fallback.

- **U7** — `crew_dispatch.py` module: the FirstMate lifecycle wrapper.
- **U8** — bridge wiring behind `crew_enabled` + fallback.
- **U9** — crew surfacing + docs truth.

### U7. `crew_dispatch.py` — the FirstMate lifecycle module — R7

**Goal:** A self-contained, unit-testable wrapper that scaffolds a scout brief,
spawns a crewmate via `fm-spawn --backend orca`, polls the status file to a
terminal state, reads `report.md`, and tears down best-effort. Independent of
the bridge; U8 consumes it.

**Dependencies:** None (standalone; `scripts/fm_doctor.sh` is the ops gate,
already wired).

**Files:**
- `crew_dispatch.py` (new) — `CrewResult` + lifecycle functions
- `tests/test_crew_dispatch.py` (new)

**Approach:** Follow the F4 contract grounded in `~/.local/share/firstmate`:
status file at `$FM_HOME/state/<id>.status`, meta at `$FM_HOME/state/<id>.meta`
(backend, worktree, terminal, window), deliverable at
`$FM_HOME/data/<id>/report.md` (scout mode; `DATA="${FM_DATA_OVERRIDE:-$FM_HOME/data}"`
verified in `bin/fm-spawn.sh:214`). Surface contract decisions:
- `CrewResult` dataclass: `crew_id`, `status` (`done`/`timeout`/`blocked`/`failed`/`spawn_failed`), `report_path`, `fallback_reason`, `teardown_ok`.
- **Six-state status machine + `resolved:` closer** (verified in `bin/fm-brief.sh:300-345`): `working:`/`resolved:`/unknown → keep polling; `blocked:`/`needs-decision:` → **short grace (~60s)** then `CrewResult.status = blocked` (fallback_reason `blocked`); `paused:` → **keep polling** (declared external wait that clears on its own — the 15-min timeout bounds it; no grace fallback); `done:`/`failed:` → terminal. `resolved:` after a `blocked:`/`paused:`/`needs-decision:` clears the block and resumes polling.
- **Cadence is event-driven, not heartbeat** (verified: the brief tells the crewmate "report sparingly: only phase changes a supervisor would act on"). A crewmate appends `blocked:` once and then *stops writing* — so a long grace is pure delay; ~60s only catches a fast self-`resolved:` race before falling back.
- Spawn failure → raise a typed `CrewUnavailableError` (the bridge catches it for fallback); poll timeout and `failed:` status → return `CrewResult` with a non-done status.
- **`crew_id` derived from the U1 cycle id** (`fm-<cycle_session_id>-<issue>` — seconds granularity) so overlapping cycles can't collide on `$FM_HOME/state/<id>` files.
- **In-flight registry + sweep (KTD-8):** persist every spawn to `data/crew_runs.json` (`crew_id`, issue, `orca_worktree_id`, status, started_at); on each spawn, sweep stale `running`/`blocked` entries older than N cycles via `orca worktree rm` (log + continue on failure). Status polling is local file reads — zero API cost.
- Poll interval ~15s, 15-minute timeout by default, overridable in tests.
- **Teardown: `fm-teardown.sh` is ALWAYS refused for orca** (verified 2026-08-11): orca worktree ids are `<repoId>::<path>` (live sample `1b3e3f14-…::/Users/brandonbennett/…`), and `fm_backend_validate_task_endpoint` (`fm-teardown.sh:431`) gates every orca teardown on `fm_backend_endpoint_atom_valid` (`fm-backend.sh:505-506`), which only accepts `[A-Za-z0-9._@%+-]` — `::` and `/` never pass. So the sweep and any immediate cleanup MUST call `orca worktree rm --worktree "id:<id>" --force` directly (the exact call `fm_backend_orca_remove_worktree` uses, `orca.sh:184`), never `fm-teardown.sh`. Store the create-returned `worktree-id` verbatim in `crew_runs.json` for this.
- **Sweep path PROVEN live (2026-08-11)** — two disposable worktrees created via the exact backend invocation (`orca worktree create --repo "id:<repo-id>" --name <id> --no-parent --setup skip --json`), then `orca worktree rm --worktree "id:<id>" --force --json` returned `{"removed": true}` for both a bare worktree AND a worktree with a live attached terminal. Verified clean at all three levels: orca worktree list (absent), `git worktree list` (absent), filesystem (dir gone); terminal cleaned too; git branch pruned. No debris left (worktree count returned to pre-drill 17). A single command handles the terminal-attached case — no separate terminal kill needed.
- All subprocess calls behind one thin `_run()` helper so tests patch a single seam.

**Patterns to follow:** `scripts/fm_doctor.sh` FM_HOME resolution
(`$HOME/.hermes/school-core-fm-config`); `verify_gate.py`'s subprocess +
typed-verdict shape; house test style in `tests/test_verify_gate.py`.

**Test scenarios:**
- Happy path: status file transitions `working:` → `done:`; `report.md` exists → `status == done`, `report_path` set, `teardown_ok` True.
- Poll timeout: status file never reaches a terminal line; short overridden timeout → `status == timeout`, no report read.
- Spawn failure: `fm-spawn` exits non-zero → `CrewUnavailableError` raised.
- `failed:` status: terminal line `failed:` → `status == failed` (not timeout).
- `blocked:` status: `blocked:` present → short-grace poll continues; grace (~60s) expires → `status == blocked`.
- `needs-decision:` status: same as `blocked:` (no human in the loop) → grace then `status == blocked`.
- `paused:` status: `paused:` present → keep polling to the 15-min timeout, NO grace fallback (declared external wait clears on its own).
- `resolved:` closer: `blocked:` then `resolved:` → resumes polling (block cleared, not terminal).
- Unknown status line: e.g. `checkpointing:` → keep polling until timeout, not treated as terminal.
- `crew_id` uniqueness: two consecutive spawns (same minute) yield distinct ids when the cycle ids differ.
- In-flight registry: spawn writes `data/crew_runs.json` entry; terminal status updates it; stale-entry sweep invokes `orca worktree rm` and logs on refusal.
- Brief scaffold: `FM_HOME/data/<id>/brief.md` contains the task text and the scout deliverable contract (`report.md`).
- Teardown orca bug: `fm-teardown` refuses (mock the atom-invalid refusal) → logs, `teardown_ok False`, no exception.
- Report missing: `done:` but no `report.md` → `report_path None` + visible warning, not a crash.

**Verification:** `tests/test_crew_dispatch.py` green; module imports
standalone; no bridge changes in this unit.

### U8. Bridge wiring behind `crew_enabled` with direct-Orca fallback — R7, R8, R9

**Goal:** Route the student-task path through the crew module when enabled,
with the same-cycle direct-Orca fallback and retry-once carry specified by R8.

**Dependencies:** U7 (module + in-flight registry).

**Files:**
- `director.py` — add optional `provided_student_output: str = None` to `run_task`; when set, use it in place of the internal student `call_model` (`director.py:649`) so review + scoring run unchanged on the crew deliverable (R9). Invalid with `isolated_phases` (assert or document).
- `issue_bridge.py` — `crew_enabled` read once (env `CREW_ENABLED`, default off in tests), crew path before the direct `run_task` call (`issue_bridge.py:549`), in-flight skip, `CREW_MAX_PER_CYCLE` cap
- `tests/test_issue_bridge.py` — new `TestCrewDispatchPath` class
- `tests/test_director.py` — `provided_student_output` substitution test

**Approach:** When `crew_enabled`:
1. Skip issues with a `running` record in `data/crew_runs.json` (KTD-8 — no duplicate dispatch across overlapping cycles).
2. Up to `CREW_MAX_PER_CYCLE` issues (default 1) go through `crew_dispatch`; the rest use the direct path this cycle (bounds wall-clock under the 30-min job timeout).
3. `done` → `run_task(..., provided_student_output=report_text)`: the crew's `report.md` is the student deliverable; the two-judge review + scoring run unchanged on it (R9).
4. `CrewUnavailableError` (spawn fail), `blocked` (grace expired), or non-done status (timeout/failed) → plain `run_task` same cycle (`issue_bridge.py:549`) = direct-Orca fallback; record `fallback_reason`.
5. If the fallback itself fails → existing retry-once semantics carry (`RETRY_FILE`, `RETRY_LIMIT=2`).
When disabled (test default): byte-for-byte today's path — the existing 30+
bridge tests that patch `director.run_task` stay green unchanged.

**Patterns to follow:** `VERIFY_GATE_STRICT` env-read pattern
(`issue_bridge.py:392`); existing `@patch("director.run_task")` hermetic tests.

**Test scenarios:**
- Flag off: `crew_dispatch` never called; `run_task` called (existing suite).
- Flag on + crew done: `run_task` called with `provided_student_output` == report text; result carries crew fields; review runs on the report.
- `provided_student_output` substitution (director): `run_task(..., provided_student_output=X)` skips the student `call_model`; the output passed to `_run_two_judge_review` is X.
- In-flight skip: issue present in `crew_runs.json` with `running` → not re-dispatched this cycle.
- Per-cycle cap: with `CREW_MAX_PER_CYCLE=1`, the second crew-eligible issue uses the direct path.
- Flag on + spawn failure (`CrewUnavailableError`): plain `run_task` fallback same cycle; result `fallback_reason == spawn_failure`.
- Flag on + poll timeout: falls back direct; `fallback_reason == timeout`.
- Flag on + `blocked` (grace expired): falls back direct; `fallback_reason == blocked`.
- Flag on + crew `failed:` status: falls back direct; `fallback_reason == crew_failed`.
- Flag on + fallback also fails: `run_task` raises → attempt recorded in retry file (attempt 1 → retry next cycle), not marked school-failed.
- Flag parsing: `CREW_ENABLED=0`/absent → off; `1` → on; invalid value → off + WARN.

**Verification:** Full `tests/test_issue_bridge.py` + `tests/test_director.py`
green (existing + new); no behavior change with the flag off.

### U9. Crew surfacing + docs truth — R9, R10

**Goal:** Make crew execution visible (result, `last_run.json`, board/cycle
log) and correct the claims that still overstate wiring, so an operator can
see when an issue ran via crew vs fallback and why.

**Dependencies:** U8 (surfacing reads the crew result shape; docs half lands
with U8).

**Files:**
- `issue_bridge.py` — crew summary fields on the per-issue result + `last_run.json` entry
- `campus.md` — FirstMate row (`:231`) flips ⚠️ Partial → ✅ behind flag
- `README.md` — crew row/file-tree entries
- `.github/workflows/school-loop.yml` — `CREW_ENABLED: "1"` + `CREW_MAX_PER_CYCLE: "1"` env on the execute job, `concurrency: group: school-loop` (queue, `cancel-in-progress: false`) so overlapping 5-min runs don't double-fetch GitHub/AgentMail, comment

**Approach:** Add a compact `crew` block to the result dict (`crew_id`,
`status`, `fallback_reason`, `teardown_ok`) mirroring the `verify`/`entire`
surfacing already shipped in U3/U6; it rides `last_run.json` and the board
comment. The workflow sets the flag on (KTD-4 default) and the concurrency
group (F1) prevents overlapping runs from duplicating GitHub/AgentMail polling;
campus/README describe flag-off fallback semantics and the in-flight
registry. The sweep is owned by U7 (KTD-8), not this unit.

**Test scenarios:**
- `last_run.json` entry carries the `crew` block when the crew path is used; absent when direct.
- Board/comment text includes a crew summary line when used.
- `campus.md` FirstMate row no longer claims un-wired dispatch (grep check).
- Workflow YAML parses; execute job sets `CREW_ENABLED` and `CREW_MAX_PER_CYCLE`; `concurrency.group` present.

**Verification:** Updated bridge suite green; `campus.md`/`README.md` row
greps clean; workflow YAML parses.

### U5. campus.md correction — R10

- `campus.md`: correct the FirstMate, verify-gate, and Layer 3 rows to match the wiring above (the Entire row already reads true once U6 lands). Docs-only; lands after U1–U4.

### U6. Entire pre-merge review in the issue path — R11, R12, R13

- `src/qodo_pre_merge.py`: rename module/function to `entire_` (keep the bookbag-compatible return shape); fix `_get_entire_path` to fall back to `~/.local/bin/entire` explicitly when `shutil.which` misses it in worktree shells.
- `issue_bridge.py` (sync path): invoke the Entire sensor before `_run_two_judge_review`; non-blocking — findings appended to the review dict and surfaced on the board/comment + cycle log (WARN when absent).
- `conductor.py` (:341-359): update import + call to the renamed module.
- Ops (not code): `entire enable` in school-core — verified 2026-08-11 (auto-installs claude-code hooks + git-refs checkpoint backend; `.entire/` gitignored). `entire agent add hermes` is rejected by CLI 0.9.0 (unknown agent; supported: claude-code, codex, copilot-cli, cursor, factoryai-droid, gemini, opencode, pi, vogon) — tracking is repo-wide via the git hooks, so no hermes-specific entry is needed.
- Tests: `_get_entire_path` finds the binary via the explicit fallback; skip path returns gracefully with a WARN; findings parse from `entire review --format text`; existing bridge/conductor tests stay green with the CLI absent.

## Acceptance Examples

Verify-gate behavior (R4/R6):

| Toolchain present | `VERIFY_GATE_STRICT` | Direct/reusable gate behavior | Scheduled school-loop behavior |
|---|---|---|---|
| yes | any | gate runs hermetically via flake | execute proceeds |
| no | unset | visible soft-skip; no fake compile failure | execute preflight fails as infrastructure error; hosted board still publishes |
| no | 1 | strict escalation; issue cannot pass unverified | execute preflight fails before issue processing |
| Nix present but `verifyShell` invalid | any | gate cannot run; soft-skip or strict escalation | execute preflight fails clearly |

FirstMate dispatch failure modes (R8):

| Situation | Behavior |
|---|---|
| `fm-spawn` fails | fall back to direct-Orca same cycle |
| agent never writes `done:` (poll timeout, 15 min) | fall back to direct-Orca same cycle |
| agent writes `blocked:`/`needs-decision:` (grace ~60s expires) | fall back to direct-Orca same cycle; `fallback_reason=blocked` |
| agent writes `paused:` (declared external wait) | keep polling to the 15-min timeout — clears on its own; only timeout falls back |
| overlapping cycle already has the issue in-flight | skip dispatch this cycle; next cycle collects the crew result |
| fallback also fails | existing retry-once semantics carry the issue |
| teardown fails after report read (known orca bug) | log + continue; U7's next-spawn sweep reclaims the worktree |

Entire pre-merge sensor (R11/R12):

| Situation | Behavior |
|---|---|
| CLI discoverable + `.entire/` enabled | `entire review` runs; findings appended to the review dict and surfaced |
| CLI not discoverable (worktree PATH gap) | explicit `~/.local/bin` fallback finds it |
| genuinely absent | non-blocking skip + visible WARN; two-judge review proceeds |

## Risks & Dependencies

- **FirstMate teardown bug — CONFIRMED worse than initially documented (2026-08-11)** — orca worktree ids are `<repoId>::<path>` (live sample verified), and `fm_backend_endpoint_atom_valid` (only `[A-Za-z0-9._@%+-]`) gates the orca branch of `fm_backend_validate_task_endpoint` (`fm-teardown.sh:431`), so `fm-teardown.sh` refuses EVERY orca task, not just edge ids. Mitigation: `crew_dispatch` never calls `fm-teardown.sh` for orca; it calls `orca worktree rm --worktree "id:<id>" --force` directly (the backend adapter's own primitive) for both immediate best-effort cleanup and the next-spawn sweep; upstream report pending.
- **Determinate Nix needs sudo on the self-hosted runner.** If that can't be granted, the researched uv fallback (`astral-sh/setup-uv` + `uv run pytest`) keeps the gate meaningful. Tracked as a fallback, not a blocker.
- **Trajectory growth in git.** Mitigation: history cap (last N cycles or per-cycle summaries) enforced by the checkpoint step.
- **Crew dispatch adds wall-clock per issue** (spawn + agent + report vs direct call). Bounded by `CREW_MAX_PER_CYCLE` (default 1) under the 30-min job timeout; the fallback bounds the rest.
- **Overlapping cycles at the 5-min cadence** (deepened 2026-08-11) — a 15-min crew spans multiple 5-min cycles. Mitigation: `concurrency: group` in the workflow (queue, no cancel) so runs don't double-fetch GitHub/AgentMail, and the `data/crew_runs.json` in-flight registry (KTD-8) so no issue is ever crew-dispatched twice.
- **U4 depends on the crew chain staying green** — `scripts/fm_doctor.sh` is the preflight gate already wired into the execute job.
- **Entire's intent-awareness depends on session tracking** — `entire enable` must run in school-core (creates `.entire/`); without it the review degrades to raw-diff only. Ops sub-task, like U3's Nix install.
- **CLI discovery in Orca worktree shells** — `_get_entire_path` must fall back to `~/.local/bin/entire` explicitly; bare `shutil.which` skips in worktrees.

## Sources / Research

- `.scratch/wayfinder-map-pipeline-gaps.md` — recon canvas with F1–F5 verdicts and delegated findings (F3: Determinate Nix / uv fallback; F4: FirstMate ship/scout contract grounded in `~/.local/share/firstmate/bin/`).
- `director.py:582` (`session_id` param exists), `director.py:714` (`enrich_prompt` call drops it).
- `context_orchestrator.py:78` (`if session_id:` gate), `:191` (`_archival_context`).
- `issue_bridge.py:475` (bridge call without `session_id`).
- `.github/workflows/school-loop.yml:154-173` (checkpoint pattern to extend in U2).
- `verify_gate.py:160` (`nix develop #verifyShell` subprocess), `flake.nix` (`verifyShell`).
- FirstMate contract summary (status file `done:` protocol, `report.md`, meta keys) — in the map's F4 findings.
- `src/qodo_pre_merge.py:133` (`run_qodo_improve` → `entire review`, skip path), `:40-42` (`_get_entire_path` = bare `shutil.which`), `conductor.py:341-359` (async-path pre-merge sensor).
- `campus.md:96` (tool-table row), `campus.md:124-130` ("Why Entire Instead of Qodo?"), `README.md:107`; `sound-royale-ny/.entire/` (reference for `entire enable` output).
