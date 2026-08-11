---
title: Pipeline Gaps — 4-Layer Context, Verify Gate, and FirstMate Dispatch
type: feat
status: active
date: 2026-08-11
origin: .scratch/wayfinder-map-pipeline-gaps.md
---

# Pipeline Gaps — 4-Layer Context, Verify Gate, and FirstMate Dispatch

**Date:** 2026-08-11
**Status:** Active
**Plan type:** feat
**Depth:** Deep

## Summary

Close the five verified gaps between what the school-loop issue path exercises today and the full pedagogical stack: thread a per-cycle `session_id` so Layer 3 archival fires, checkpoint-commit sanitized trajectories so Layer 2 has history on fresh checkouts, make the verify gate loud instead of silently inert, dispatch student tasks through the proven FirstMate crew, bring the Entire pre-merge review into the live path, and correct campus.md's overclaims. Direct-Orca execution and the CTO/COO review remain the fallback and the unchanged upper half of the pipeline.

## Problem Frame

The wayfinder map (`.scratch/wayfinder-map-pipeline-gaps.md`) confirmed on disk that the live issue path (`issue_bridge` → `director.run_task` → review → score) exercises only a thin slice of the system:

1. **Layer 3 is dead.** `enrich_prompt` includes archival context only when `session_id` is set (`context_orchestrator.py:78`), and `run_task` already carries the parameter (`director.py:582`) — but drops it at the `enrich_prompt` call (`director.py:714`), and the bridge never passes one (`issue_bridge.py:475`). `data/consolidation/` does not exist.
2. **Layer 2 is empty on every fresh checkout.** `data/trajectories/` is not tracked; each school-loop cycle starts in a fresh checkout with zero trajectory history, so the file-RAG no-ops.
3. **Compiler-before-critic is decorative.** `verify_gate.py:160` shells out to `nix develop flake.nix#verifyShell`; Nix is not installed on the Mac runner, so the gate silently returns the non-blocking `None` path every cycle.
4. **The crew engine is unplugged.** The FirstMate→Orca→Hermes cycle is proven end-to-end (live drill, 2026-08-11) but never invoked from the issue path; campus.md claims it is wired.
5. **Docs lie.** The campus.md Operational Reality table marks FirstMate and the verify gate as "✅ Wired".
6. **Entire review is documented but dormant.** `src/qodo_pre_merge.py:133` runs `entire review` as a pre-merge sensor (`conductor.py:341-359`), but only in the async conductor path — never in the school-loop's sync path — and skips in practice: `_get_entire_path()` is a bare `shutil.which("entire")` (`src/qodo_pre_merge.py:40-42`), so Orca worktree shells without `~/.local/bin` on PATH return "skipped". school-core has no `.entire/` checkpoint tracking (the intent-aware half), while sound-royale-* repos do.

## Requirements

### Context layers

- R1. The bridge threads a per-cycle `session_id` through `director.run_task`, which forwards it to `enrich_prompt`, so Layer 3 archival context is included when a consolidation exists for that session.
- R2. `data/trajectories/` is committed to git, sanitized via the existing checkpoint pattern, so fresh school-loop checkouts see trajectory history.
- R3. `data/consolidation/` is seeded (directory plus initial index) so the Layer 3 write path has a home.

### Verify gate

- R4. When the toolchain (nix) is absent, the verify gate appends a WARN finding that surfaces on the issue's board/comment and in the cycle log, instead of silently returning `None`.
- R5. The verify gate runs hermetically via the flake whenever the toolchain is present; installing Determinate Nix on the Mac runner is the preferred ops path (uv fallback the documented alternative), not a code acceptance bar.
- R6. A strict mode (`VERIFY_GATE_STRICT=1`) makes a missing toolchain fatal, for CI and manual runs that demand enforcement.

### Dispatch

- R7. The student-task path dispatches through FirstMate (`fm-spawn`, orca backend) and reads back the deliverable (`report.md` + terminal status line) when crew dispatch is enabled.
- R8. On crew spawn failure or poll timeout, the bridge falls back to direct-Orca in the same cycle; if that also fails, the existing retry-once semantics carry the issue.
- R9. CTO/COO two-judge review and scoring run unchanged on the crew deliverable.

### Docs

- R10. campus.md's Operational Reality table reflects actual wiring: FirstMate = wired behind a flag, verify gate = warn until toolchain installed, Layer 3 = live.

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

### KTD-3: Verify gate is warn-and-continue, never silent, never fatal by default

**Decision:** When nix is absent, the gate appends a WARN finding and returns a non-blocking result; `VERIFY_GATE_STRICT=1` flips it fatal.

**Rationale:** A missing toolchain must not kill the school cycle — a board with a WARN beats a dead loop — but silence is what hid this gap for weeks. Loudness is the fix; strictness is opt-in for runs that demand enforcement.

### KTD-4: FirstMate wraps the student-task path behind a flag

**Decision:** New `crew_dispatch.py` scaffolds a brief, runs `fm-spawn --backend orca` (scout mode for the pilot), polls the status file, reads `report.md`, and tears down with the documented orca-bug workaround. Enabled by `crew_enabled` (default on in school-loop, off in the unit-test env).

**Rationale:** The crew cycle is proven; a flag keeps the 30+ bridge tests hermetic (they patch `director.run_task`). Direct-Orca stays as fallback so retry-once carries any crew failure. Review lenses are untouched because the deliverable shape (`report.md`) feeds the same review.

### KTD-5: Docs truth lands with the code it describes

**Decision:** campus.md rows are corrected in the same change as the wiring they describe.

**Rationale:** The map proved two "✅ Wired" rows were false. Docs that overclaim cost the next on-call real debugging time.

### KTD-6: Entire is a non-blocking sensor, not a gate

**Decision:** Keep the degrade-gracefully semantics; fix discovery and enablement instead of making Entire blocking.

**Rationale:** Same policy as KTD-3 — a missing CLI must never kill the school cycle. Entire catches mechanical issues (unused vars, type narrowing) the LLM judges miss, so its findings inform them; it doesn't veto them.

## High-Level Technical Design

Target issue path after all units:

```
GitHub issue
  └─ issue_bridge.process
       ├─ clone_repo + build_codebase_context        (thin context, unchanged)
       ├─ crew_dispatch (U4, crew_enabled):          (new)
       │     brief → fm-spawn (orca) → poll status → report.md → teardown
       │     └─ fallback: call_model direct (Orca)   (R8)
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

- `verify_gate.py`: detect the nix binary; when absent, append a WARN finding that flows to the issue's board/comment and cycle log (the `None` path replaced by a visible result); honor `VERIFY_GATE_STRICT=1` as fatal.
- `.github/workflows/school-loop.yml`: toolchain preflight WARN in the execute job.
- Ops (not code, not an acceptance bar): install Determinate Nix on the Mac runner; if sudo can't be granted, the uv fallback is the documented alternative.
- Tests: gate returns WARN (not silent `None`) when nix is missing; strict mode raises; the WARN text appears in the surfacing path.

### U4. FirstMate dispatch in the issue path — R7, R8, R9

- New `crew_dispatch.py`: scaffold brief → `fm-spawn --backend orca` (scout, hermes-fm-wrapper) → poll `$STATE/$ID.status` for a terminal line with a 15-minute timeout → read `report.md` → best-effort teardown via the documented manual-cleanup helper (log + continue when the orca teardown bug trips, so worktrees don't leak silently).
- `issue_bridge.py`: `crew_enabled` path with direct-Orca fallback on spawn failure and poll timeout (retry-once carries the issue).
- Tests: mocked `fm-spawn` + status-file simulation, including spawn-fail and poll-timeout paths; existing bridge suite stays green (flag off in tests).

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

| Toolchain present | `VERIFY_GATE_STRICT` | Behavior |
|---|---|---|
| yes | any | gate runs hermetically via flake |
| no | unset | WARN surfaces on the board/comment and cycle log; loop continues |
| no | 1 | gate fatal; execute job fails loudly |

FirstMate dispatch failure modes (R8):

| Situation | Behavior |
|---|---|
| `fm-spawn` fails | fall back to direct-Orca same cycle |
| agent never writes `done:` (poll timeout, 15 min) | fall back to direct-Orca same cycle |
| fallback also fails | existing retry-once semantics carry the issue |
| teardown fails after report read (known orca bug) | log + continue; leftover worktrees swept next cycle |

Entire pre-merge sensor (R11/R12):

| Situation | Behavior |
|---|---|
| CLI discoverable + `.entire/` enabled | `entire review` runs; findings appended to the review dict and surfaced |
| CLI not discoverable (worktree PATH gap) | explicit `~/.local/bin` fallback finds it |
| genuinely absent | non-blocking skip + visible WARN; two-judge review proceeds |

## Risks & Dependencies

- **FirstMate teardown bug** — `fm_backend_endpoint_atom_valid` rejects `:` and `/` in `orca_worktree_id`, so `fm-teardown.sh` refuses orca tasks. Mitigation: `crew_dispatch` tears down best-effort (log + continue) and sweeps leftover worktrees on the next cycle; upstream report pending.
- **Determinate Nix needs sudo on the self-hosted runner.** If that can't be granted, the researched uv fallback (`astral-sh/setup-uv` + `uv run pytest`) keeps the gate meaningful. Tracked as a fallback, not a blocker.
- **Trajectory growth in git.** Mitigation: history cap (last N cycles or per-cycle summaries) enforced by the checkpoint step.
- **Crew dispatch adds wall-clock per issue** (spawn + agent + report vs direct call). Acceptable at school cadence; the fallback bounds the risk.
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
