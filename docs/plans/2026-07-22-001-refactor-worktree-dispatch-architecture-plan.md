# Refactor: Async Worktree Dispatch Architecture

**Date:** 2026-07-22
**Status:** Complete (6/6 units complete) 🎉
**Plan type:** refactor
**Depth:** Deep

## Build Status

| Unit | Status | Lines | Tests | Key Files |
|------|--------|-------|-------|-----------|
| **U4a** Polling helpers | ✅ **COMPLETE** | ~80 | 6 (smoke) | `bookbag.py` |
| **U1** `teacher.py` | ✅ **COMPLETE** | ~280 | 40 | `teacher.py` |
| **U2** `leaf.py` | ✅ **COMPLETE** | ~280 | 35 | `leaf.py`, `conductor.py` refactored |
| **U3** Async Principal | ✅ **COMPLETE** | ~150 | — | `conductor.py` (async mode), `director.py` (`skip_review`), `leaf.py` (`async_mode`) |
| **U4b** Handoff doc | ✅ **COMPLETE** | ~500 | — | `docs/HANDOFF.md` |
| **U5** Disposal hardening | ✅ **COMPLETE** | ~100 | 9 (smoke) | `orca_executor.py`, `tests/test_orca_execution.py` |

---

## Summary

Refactor `conductor.py` from a single-process synchronous loop into a **persistent worktree hierarchy** with sleep/wake cycles and async bookbag handoff. The Principal (main worktree) dispatches tasks into disposable student leaf worktrees. Two persistent teacher worktrees (CTO, COO) sleep between tasks and wake when a bookbag signals a completed student. Leaf worktrees auto-dispose after the bookbag handoff is acknowledged.

This replaces the current architecture where everything runs in one Python process: task dispatch, student output, CTO review, COO review, and scoring all happen sequentially in `run_conductor()`.

---

## Problem Frame

**Current architecture** (synchronous, single-process):

```
conductor.py (principal process)
  ├── run_task() → LLM call (student output)
  ├── _run_two_judge_review() → inline CTO+COO review
  ├── evaluate_and_update() → score
  └── DisplayTerminal + study-* worktree (side effect only)
```

Problems:
1. **No isolation** — All LLM calls and review happen in one process. A crash or timeout loses the entire pipeline state.
2. **Worktree accumulation** — `study-*` worktrees pile up because there's no auto-disposal mechanism.
3. **No sleep/wake** — The conductor must stay alive for the full loop. It can't pause between rounds and resume.
4. **No async handoff** — The student can't work independently and hand off results asynchronously.
5. **Redundant display terminal** — The `ConductorDisplay` was already removed; the worktree IS the display.

**Target architecture** (async, worktree-based):

```
main (principal worktree, port 58070)
  ├── teacher-cto (persistent worktree, sleeps)
  ├── teacher-coo (persistent worktree, sleeps)
  └── leaf-{role}-{bead} (disposable worktree, per-task)
       └── .hermes/briefs/{bead}.json  ← task brief
       └── ~/.hermes/bookbag/{bead}.json  ← handoff artifact
```

---

## Requirements

1. **R1 — Persistent teacher worktrees**: Two worktrees (`teacher-cto`, `teacher-coo`) created once at principal setup. They sleep via `sleep_state.execute_sleep()` and wake via `sleep_state.execute_wake()` when a bookbag signals a completed student.

2. **R2 — Disposable leaf worktrees**: Each task creates a leaf worktree (`leaf-{role}-{bead}`). The leaf writes a StudentBrief, dispatches the student LLM call, writes output and bookbag, then **auto-disposes** after the bookbag handoff is acknowledged.

3. **R3 — Async bookbag handoff**: The leaf writes its bookbag to `~/.hermes/bookbag/`. The teachers poll for new bookbags (or receive a wake signal). After review, teachers update the bookbag with verdicts. The principal polls for completed bookbags and scores.

4. **R4 — Sleep/wave lifecycle**: Teachers use the existing `SleepState` data model from `sleep_state.py`. Sleep consolidates episodic context; wake restores session state. The Library Log tracks all sleep/wake cycles for audit.

5. **R5 — Auto-disposal**: Leaf worktrees are destroyed via `orca worktree rm --force` after the bookbag handoff is acknowledged by both teachers. No manual `--clean-worktrees` needed.

---

## Key Technical Decisions

### KTD-1: Polling vs. push for bookbag handoff

**Decision:** Polling on a shared bookbag directory.

**Rationale:** The existing `bookbag.py` API already supports `read_bookbag()` and `update_bookbag()` for CRUD on `~/.hermes/bookbag/`. Adding a push mechanism (signals, file watchers, message queue) would add external dependencies. Polling is simpler: teachers poll every N seconds for new bookbags matching their domain. For a personal dev tool with ~5 role types, polling at 5s intervals adds negligible overhead.

**Trade-off:** At scale (100+ concurrent tasks), polling becomes wasteful. The polling interval should be configurable.

### KTD-2: Sleep/wake triggers for teachers

**Decision:** Teachers sleep after each review cycle and wake when a matching bookbag appears.

**Rationale:** `sleep_state.py` already supports the full sleep/wake lifecycle with Library Log audit trails. Sleep consolidates the teacher's episodic history (which tasks they reviewed, what findings they found). Wake restores their state so they can resume review with full context.

**Trigger mechanism:** The teacher's wake is triggered by the presence of a new bookbag with `cto_verdict=""` or `coo_verdict=""` (i.e., not yet reviewed by that teacher). The principal creates the bookbag with empty verdicts; the teacher sees it, reviews it, fills in its verdict.

### KTD-3: Leaf worktree naming and scoping

**Decision:** `leaf-{role}-{bead[:8]}` naming with `--repo <school-core>`.

**Rationale:** Consistent with the validated `orca worktree create --name <name> --repo <path>` pattern. The `leaf-` prefix distinguishes from `teacher-` and `study-` prefixes. The bead hash ensures uniqueness across runs.

**Auto-disposal trigger:** After the bookbag has both CTO and COO verdicts filled AND `accepted` is set, the leaf is safe to destroy.

### KTD-4: Principal as orchestrator, not executor

**Decision:** The principal creates the leaf and teachers, then **polls** for completion rather than blocking.

**Rationale:** This allows the principal to dispatch multiple tasks concurrently (one leaf per task) while teachers review in their own worktrees. The principal's `main()` enters a polling loop: check for completed bookbags → score → log → dispatch next task.

---

## High-Level Technical Design

### Architecture Overview

```
┌──────────────────────────────────────────────────┐
│  main (principal worktree)                        │
│  ─────────────────────                             │
│  orca worktree create --name main --port 58070    │
│  conductor.py --loop --rounds N                   │
│                                                    │
│  1. boot() → create teacher-cto + teacher-coo     │
│  2. dispatch(task) → create leaf, write brief     │
│  3. poll() → check bookbag for completed verdicts │
│  4. score() → evaluate_and_update                 │
│  5. dispose() → orca worktree rm leaf-*           │
└──────────────────────────────────────────────────┘
         │                            │
         │ create worktree            │ create worktree
         ▼                            ▼
┌──────────────────┐    ┌──────────────────────────┐
│ teacher-cto       │    │ teacher-coo               │
│ (persistent)      │    │ (persistent)              │
├──────────────────┤    ├──────────────────────────┤
│ 1. sleep()       │    │ 1. sleep()               │
│ 2. wake() when   │    │ 2. wake() when           │
│    bookbag has   │    │    bookbag has           │
│    cto_verdict=""│    │    coo_verdict=""        │
│ 3. review() →    │    │ 3. review() →            │
│    update_bookbag│    │    update_bookbag        │
│ 4. sleep()       │    │ 4. sleep()               │
└──────────────────┘    └──────────────────────────┘
         ▲                        ▲
         │ bookbag handoff        │
         ▼                        ▼
┌──────────────────────────────────────────────────┐
│  leaf-coder-abc123 (disposable worktree)          │
│  ─────────────────────────────                    │
│  1. StudentBrief → .hermes/briefs/               │
│  2. run_task() → LLM call                        │
│  3. write_bookbag(cto_verdict="",coo_verdict="") │
│  4. [wait for teachers to fill verdicts]         │
│  5. auto-dispose                                  │
└──────────────────────────────────────────────────┘
```

### Sleep/Wake Lifecycle for Teachers

```
                         ┌─────────┐
                         │  BOOT   │
                         └────┬────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  WAKE (init)    │
                    │  load session   │
                    │  hydrate scores │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
              ┌────▶│  SLEEP          │
              │     │  freeze state   │
              │     │  consolidate    │
              │     │  persist + log  │
              │     └────────┬────────┘
              │              │
              │     ┌────────▼────────┐
              │     │  WAKE (resume)  │
              │     │  load session   │
              │     │  check bookbags │
              │     └────────┬────────┘
              │              │
              │     ┌────────▼────────┐
              │     │  REVIEW         │
              │     │  read bookbag   │
              │     │  apply lens     │
              │     │  update verdict │
              │     └────────┬────────┘
              │              │
              └──────────────┘
```

### Bookbag State Machine

```
                    PRINCIPAL                 TEACHERS
                       │                        │
    write_bookbag() ───┤                        │
    (cto_verdict="")   │           ┌─────────────┤
    (coo_verdict="")   │           │ poll()      │
    (accepted=false)   │           ▼             │
                       │     ┌────────────┐      │
                       │     │  CTE reads │      │
                       │     │  bookbag   │      │
                       │     │  reviews   │──────┤
                       │     │  updates   │      │
                       │     │  verdict   │      │
                       │     └────────────┘      │
                       │           │             │
    poll() ────────────┤           ├─────────────┤
    (cto_verdict=PASS) │           │   COO reads │
                       │           │   bookbag   │
                       │           │   reviews   │
                       │           │   updates   │
    poll() ────────────┤           │   verdict   │
    (both verdicts in) │           └─────────────┘
                       │
    accepted? ─────────┤
      YES → score + dispose leaf
      NO  → log failure + dispose leaf
```

---

## Implementation Units

### U1. Create `teacher.py` — persistent teacher worktree wrapper

**Status:** ✅ **COMPLETE**

**Goal:** Encapsulate the lifecycle of a teacher worktree (CTO or COO): boot, sleep, wake, review loop.

**Files:**
- `school-core/teacher.py` (new, ~280 lines)
- `school-core/tests/test_teacher.py` (new, 40 tests)

**Dependencies:** U4a (polling helpers)

**What was built:**
- `TeacherWorktree` class with boot/sleep/wake/review_cycle/run_loop/prune_sessions
- `boot()` creates or rediscoveries persistent worktree (`teacher-cto` or `teacher-coo`)
- `sleep()` calls `sleep_state.execute_sleep()`, clears episodic history
- `wake()` calls `sleep_state.execute_wake()`, graceful on first boot
- `review_cycle()` polls bookbags, applies adversarial lens, updates verdict via `locked_update_bookbag()`
- `run_loop()` infinite sleep/wake/review cycle with periodic session pruning
- `prune_sessions()` removes old session files beyond `max_cycles`
- CLI: `python teacher.py --role cto [--once]`
- Context manager: `with TeacherWorktree("cto") as t:`

**Key issues found and fixed:**
- `prune_sessions()` had wrong glob pattern (never matched) — fixed to `{session_id}*.json` sorted by mtime
- `boot()` had no rediscovery on restart — added Orca worktree list scan fallback (G3)
- Unused imports removed: `json`, `Verdict`, `HandoffTimeoutError`, `StudentBrief`, `uuid`, `BookbagSignal`

**Test coverage:** Construction (5), Boot (5), Sleep/Wake (5), ReviewCycle (7), PruneSessions (6), Close (4), RunLoop (2), CLI (2), Lenses (3) — **40 tests total**

**Verification:** `python teacher.py --role cto --once` boots and runs one review cycle.

---

### U2. Create `leaf.py` — disposable student leaf worktree wrapper

**Status:** ✅ **COMPLETE**

**Goal:** Encapsulate the lifecycle of a disposable student leaf: create worktree, write brief, run task, write bookbag, auto-dispose.

**Files:**
- `school-core/leaf.py` (new, ~280 lines)
- `school-core/tests/test_leaf.py` (new, 35 tests)
- `school-core/conductor.py` (refactored to use `run_leaf()` instead of `run_conductor()`)

**Dependencies:** U4a (polling helpers), U1 (teacher worktree pattern)

**What was built (Phase 1):**
- `StudentLeaf` class — boot/write_brief/run_task/write_output/signal_ready/wait_for_handoff/dispose
- `run_leaf()` convenience function — wraps full lifecycle in try/finally for automatic cleanup
- Context manager: `with StudentLeaf(role, domain) as leaf:`
- `boot()` creates `study-{role}-{rand8}` worktree via `OrcaExecutionManager`
- `write_brief()` writes `StudentBrief` to `.hermes/briefs/{bead}.json` inside the worktree
- `run_task()` delegates to `director.run_task()` (synchronous Phase 1 — LLM in principal process)
- `signal_ready()` writes `.hermes/signals/{bead}.ready` flag for teacher discovery
- `wait_for_handoff()` delegates to `wait_for_verdicts()` (Phase 2 future path)
- `dispose()` calls `close_worktree()`, idempotent

**Key design decisions:**
- **Phase 1** (synchronous): `run_task()` includes full LLM call + bookbag write + CTO/COO review inline in the principal process. `signal_ready()` is informational.
- **Phase 2** (future): leaf writes bookbag only, signals teachers, and waits for async teacher polling via `wait_for_handoff()`.
- LLM call runs in principal process (G2 — leaf is a workspace, not a sandbox)
- Worktree naming: `study-{role}-{rand8}` (consistent with existing prefixed cleanup)
- Bead auto-generation: `{role}-{domain}-{rand8}` (unique per leaf)

**Conductor refactoring:** `run_conductor()` (~80 lines of manual worktree/brief/output management) replaced with direct `run_leaf()` calls. `conductor.py` reduced from ~200 lines to ~140 lines.

**Test coverage:** Construction (6), Boot (4), Brief/Output (4), RunTask (4), Signal (3), Dispose (4), ContextManager (3), RunLeaf (4), CLI (2) — **35 tests total**

**Verification:** `python leaf.py --role coder --domain python-coding --task "Write hello"` creates worktree, runs task, writes output, signals, and disposes.

---

### U3. Refactor `conductor.py` — async dispatch loop

**Status:** ✅ **COMPLETE** (Phase 1 + Phase 2)

**Goal:** Async dispatch with persistent teacher worktrees, principal polling for bookbag completion, and `--async` flag.

**Files:**
- `school-core/conductor.py` (major refactoring, ~300 lines)
- `school-core/director.py` (+`skip_review` parameter on `run_task()`)
- `school-core/leaf.py` (+`async_mode` parameter on `run_leaf()`)
- `school-core/tests/test_leaf.py` (updated assertion for `skip_review=False`)

**What was built:**

**Phase 1 (synchronous, default):**
- `_run_sync_loop()` — extracted from old `main()`, identical behavior
- `_run_single_task()` — extracted from old `main()`, identical behavior
- `_score_and_print_round()` — shared scoring + display for sync mode

**Phase 2 (async, `--async`):**
- `_run_async_loop()` — 4-step pipeline:
  1. **Boot teachers** — creates `teacher-cto` + `teacher-coo` Orca worktrees, starts `TeacherWorktree(role).run_loop()` in dedicated terminals
  2. **Dispatch leaves** — creates StudentLeaf per round, runs LLM only (`skip_review=True`), signals ready. Leaves NOT disposed yet.
  3. **Poll verdicts** — uses `wait_for_verdicts()` to wait for teacher review on each bookbag, reads back findings/scores
  4. **Score + dispose** — calls `evaluate_and_update()` and `leaf.dispose()` for each completed round
  5. **Shutdown teachers** — closes teacher worktrees
- `_boot_teachers()` — creates fresh `OrcaExecutionManager()` per teacher, starts review loop in Orca terminal
- `_shutdown_teachers()` — closes all teacher worktrees
- Graceful fallback: if both teachers can't boot → falls back to sync mode with partial-teacher cleanup

**New CLI flags:**
- `--async` — enable async dispatch (loop mode only)
- `--handoff-timeout` (default 120s) — max seconds to wait for teacher verdicts

**Key design decisions:**
- **Fresh `OrcaExecutionManager`** for terminal control (not `teacher._mgr` private attr) — decouples conductor from teacher internals
- **Both teachers required** — `len(teachers) < 2` triggers sync fallback (partial teacher boot = all bookbags timeout)
- **Leaf disposal deferred** — leaves stay alive until teacher verdicts arrive, then disposed
- **Phase 2 leaves skip `_run_two_judge_review()`** — teachers handle review via their own `review_cycle()`

**Phase 2 verification:** `python conductor.py --loop --rounds 3 --async` boots teachers, dispatches leaves, polls for verdicts, scores, and shuts down.

---

### U4a. Polling helpers (bookbag handoff protocol)

**Status:** ✅ **COMPLETE**

**Files:**
- `school-core/bookbag.py` (~80 lines added to existing ~200)

**What was built:**
- `HandoffTimeoutError` — custom exception extending `TimeoutError`
- `acquire_lock(bead, timeout)` — O_EXCL file-lock with PID-based stale lock detection (G4/R7)
- `release_lock(bead)` — safe lock release with `missing_ok`
- `locked_update_bookbag(bead, lock_timeout, **kwargs)` — atomic read-modify-write with lock
- `BookbagSignal` — `.ready()` / `.check()` / `.clear()` flag-file protocol
- `wait_for_bookbag(bead, required_fields, timeout, interval)` — poll with descriptive timeout error
- `wait_for_verdicts(bead, timeout, interval)` — convenience wrapper returning `(cto, coo)`

**Smoke test results (6/6):** Signal lifecycle, lock exclusion, stale lock recovery, locked update, async CTO→COO handoff, timeout with descriptive error.

---

### U4b. Handoff contract documentation

**Status:** ✅ **COMPLETE**

**Files:**
- `school-core/docs/HANDOFF.md` (new, ~500 lines)

**Content:**
- Bookbag state machine with Phase 1 vs Phase 2 callout diagram
- Signal protocol (BookbagSignal ready/check/clear, combined polling strategy)
- File-lock rules (O_EXCL acquire, PID-based stale detection, locked_update_bookbag)
- Teacher lifecycle contract (boot/sleep/wake/review/run_loop/prune)
- Leaf lifecycle contract (boot/brief/run/output/signal/dispose)
- Timeout configuration reference table (8 variables, all with defaults)
- Error recovery table (teacher crash, leaf crash, principal crash, stale locks)
- Appendix: ASCII end-to-end flow diagram

---

### U5. Add auto-dispose to `OrcaExecutionManager`

**Status:** ✅ **COMPLETE**

**Goal:** Harden worktree disposal with retry logic, force-dispose fallback, and smoke tests.

**Files:**
- `school-core/orca_executor.py` (~100 lines added; now 733 lines total)
- `school-core/tests/test_orca_execution.py` (9 new smoke tests in `TestWorktreeDisposal` class)

**What was built:**

- **`_hard_remove_worktree(path)`** — shared static helper (`@staticmethod`) that tries `git worktree remove --force` → `shutil.rmtree`. Used by both `close_worktree()` and `force_dispose_worktree()`. Eliminates code duplication.

- **`close_worktree(path) → bool`** — hardened with three-tier strategy:
  1. Orca CLI with 3 retries (1s, 2s exponential backoff)
  2. `_hard_remove_worktree()` (git → rm -rf)
  3. Returns `bool` so callers can detect failure
  - **Idempotent**: returns `True` immediately if path doesn't exist
  - **Verifies** each strategy by checking `Path(path).exists()` after each attempt

- **`force_dispose_worktree(path) → bool`** — delegates to `_hard_remove_worktree()`. Skips Orca CLI entirely — call this when Orca is known down.

- **`cleanup_worktrees_by_prefix()`** — updated to use `close_worktree()`'s `bool` return: only increments `removed` counter when removal succeeds.

**Smoke test results (9/9):**
- `test_close_worktree_idempotent_nonexistent` — non-existent path → True
- `test_close_worktree_orca_succeeds_first_try` — Orca succeeds on attempt 1
- `test_close_worktree_retry_on_orca_failure` — Orca fails twice, succeeds third → 3 calls
- `test_close_worktree_falls_back_to_rm_rf` — Orca fails all 3, rm -rf removes it
- `test_close_worktree_git_remove_fallback` — verifies full chain (Orca fails → git → rm -rf)
- `test_force_dispose_removes_directory` — real directory removal without Orca
- `test_force_dispose_idempotent` — non-existent path → True
- `test_force_dispose_nested_structure` — deeply nested directories
- `test_close_worktree_returns_false_when_all_strategies_fail` — all 3 tiers fail → returns False (never raises)

---

## Scope Boundaries

### In scope
- Persistent CTO/COO teacher worktrees with sleep/wake lifecycle
- Disposable student leaf worktrees with auto-disposal
- Async bookbag handoff between leaf and teachers
- Principal orchestrator refactored for async dispatch
- Auto-cleanup: zero manual worktree management needed

### Deferred to follow-up work
- **LLM-in-leaf execution:** Running the LLM call inside the leaf's own terminal (vs principal process) — U2 Phase 2
- **Concurrent dispatch:** Dispatcing all N rounds at once with `--async` flag — U3 Phase 2
- **AgentMail handoff:** Replacing bookbag polling with AgentMail email-based handoff (per faculty-orchestrator skill) — future consideration
- **Scoring worktree:** Creating a dedicated `teacher-scorer` worktree for `evaluate_and_update()` — future optimization

### Out of scope
- Changing the `AdversarialReviewer` implementation (used as-is)
- Changing the `sleep_state.py` data model (used as-is)
- Adding a message queue or external handoff broker
- Multi-machine or distributed worktree management

---

## Deepening Findings (2026-07-22)

### G1. Teacher worktree process model is underspecified

The plan says teachers "sleep" and "wake" but doesn't explain **how** a teacher worktree runs its review loop.

- **What's needed:** Each teacher worktree needs a **running Python process** that:
  1. Polls `~/.hermes/bookbag/` for un-reviewed bookbags matching its lens type
  2. Calls `AdversarialReviewer.review()` when a bookbag is found
  3. Calls `update_bookbag()` to fill the verdict
  4. Calls `sleep_state.execute_sleep()` to persist state
- **How does the process start?** The principal creates the teacher worktree, creates a terminal inside it, then sends `python -c "from teacher import TeacherWorktree; TeacherWorktree('cto').run_loop()"` via `orca terminal send --enter`.
- **If the principal crashes:** The teacher processes keep running in their worktree terminals. But the principal won't be monitoring them. On restart, the principal needs to **rediscover** existing teacher worktrees rather than creating new ones.

**Resolution:** Add `run_loop()` to `TeacherWorktree` that enters an infinite poll-sleep-review cycle. The principal starts this loop inside the teacher's terminal at boot time. The principal also needs a `rediscover_worktrees()` method to find existing teacher worktrees on restart.

---

### G2. Leaf worktree value is limited in Phase 1

In Phase 1, the leaf runs the LLM call in the **principal process** (not inside the leaf's terminal). This means the leaf is just a filesystem + git worktree — the same as the current `study-*` worktrees. The real isolation benefit (student runs in a separate terminal inside their own worktree) doesn't materialize until Phase 2.

**Implication:** Phase 1 leaf worktrees don't provide process isolation. They provide:
- Named workspace scoping (better than `study-*` which had no auto-disposal)
- Structured StudentBrief + output writing
- Auto-disposal on completion

This is still an improvement over the current `study-*` approach, but it's worth scoping the Phase 1 leaf worktree implementation to be minimal — just enough to provide the handoff contract and auto-disposal.

**Resolution:** Don't over-engineer Phase 1 leaves. Keep `LeafWorktree` lean: create worktree → write brief → call `run_task()` in principal → write bookbag → signal teachers → wait for disposal.

---

### G3. Principal crash recovery is absent

If the principal crashes mid-loop:
- Teacher worktrees persist on disk (their process may or may not survive the crash)
- Leaf worktrees remain on disk (orphaned, incomplete)
- Bookbags are on disk with partial verdicts (e.g., CTO filled but COO didn't)
- The `result` dict from `run_task()` is lost (held in memory by the principal)

**Implication:** Without recovery logic, the principal can't resume a loop after a crash. It would need to:
1. Rediscover existing teacher worktrees (G1)
2. Scan `~/.hermes/bookbag/` for in-progress bookbags (verdicts partially filled)
3. Decide whether to continue waiting for partial handoffs or abort them
4. Clean up orphaned leaf worktrees

**Resolution:** Add a `--resume` flag to `conductor.py` that scans for existing teachers and in-progress handoffs. The default `--loop` mode starts fresh (cleans up orphans first). This is a **new requirement (R6)**.

---

### G4. Bookbag write contention risk

`update_bookbag()` reads a JSON file, modifies it in memory, and writes it back. If CTO and COO both update the same bookbag file within the same ~100ms window, the second writer could overwrite the first's changes.

**Current risk:** Very low in the current design. Teachers update DIFFERENT fields (`cto_verdict` vs `coo_verdict`). And the principal is single-threaded. But if teachers ever run concurrently (e.g., if the principal booted both teacher loops), contention is possible.

**Mitigation:** Add a simple file-lock via `.hermes/locks/{bead}.lock` — the polling teacher creates a lock file before reading, deletes it after writing. If the lock exists, skip that bookbag and poll again next cycle.

**Resolution:** Add lock-file protocol to U4 (bookbag handoff schema).

---

### G5. Principal must hold `result` dict across handoff

The principal calls `run_task()` which returns a `result` dict. This dict contains the student's output, which is needed for `evaluate_and_update()` scoring. But scoring happens AFTER both teachers finish reviewing — which could be minutes later.

**Implication:** The principal must keep the `result` dict in memory across the full handoff lifecycle for each leaf. If the principal dispatches 5 rounds sequentially, it holds 5 `result` dicts in memory. If the principal crashes, all in-flight results are lost.

**Mitigation 1 (recommended):** Serialize the `result` dict to the leaf worktree (`.hermes/results/{bead}.json`) after `run_task()` completes. The principal can then discard it from memory and read it back when scoring. This adds crash resilience at the cost of one write per round.

**Mitigation 2 (deferred):** A dedicated scoring worktree (`teacher-scorer`) that holds and processes result dicts asynchronously.

**Resolution:** Add result serialization to the leaf's `.hermes/results/` directory (new sub-requirement in U2).

---

### G6. U4 dependency ordering issue

U1's `review_loop()` polls for bookbags using the polling protocol defined in U4. The plan says U1 depends on U4. But the polling helper (`wait_for_bookbag()`) is part of U4, so U1 can't work standalone without U4's code.

**Fix:** Split U4 into U4a (polling helpers — standalone code) and U4b (handoff contract documentation). U1 depends on U4a.

**Revised dependency chain:** U4a → U1 → U4b → U2 → U3 → U5

---

### G7. Old teacher session data grows unbounded

`sleep_state.py` saves session JSON files to `data/sessions/` with no pruning. Each sleep/wake cycle for a teacher creates:
- 1 session JSON (`{session_id}.json`)
- 1 scores snapshot (`{session_id}_scores.json`)
- 1 consolidation YAML (`{session_id}.yaml`)
- 1 library log entry

For 100 teacher sleep/wake cycles, that's ~300 files.

**Mitigation:** Add a `max_cycles` parameter to `TeacherWorktree.boot()`. Teachers older than N cycles automatically consolidate and archive old sessions. Or use a rolling window: keep the last 10 sessions, archive the rest.

**Resolution:** Add to U1: `TeacherWorktree.prune_sessions(max_sessions=10)` called during `sleep()`.

---

### G8. Force-dispose fallback untested with Orca worktrees

The plan specifies `git worktree remove` as the fallback if `orca worktree rm --force` fails. But Orca-managed worktrees use a specific `.git` pointer file format. `git worktree remove` should work on any git worktree, but it hasn't been tested in this context.

**Mitigation:** Add a validation step in U5 that creates a test worktree, removes it via fallback, and confirms cleanup. This ensures the fallback path is tested before it's needed in production.

**Resolution:** Add a `test_disposal_fallback()` smoke test to U5.

## Revised Open Questions

| Question | Status | Resolution |
|---|---|---|
| Polling interval for bookbag handoff | Deferred to implementation | Default 5s, env var `HANDOFF_POLL_INTERVAL` |
| Should LLM calls run inside the leaf terminal? | **Clarified (G2)** | Phase 1: principal process (acceptable — leaf is a workspace, not sandbox) |
| What happens if a teacher worktree crashes? | **New: needs Rediscovery (G1, G3)** | Add `rediscover_worktrees()` and `--resume` flag |
| Concurrent handoffs — thread-safe bookbag writes? | **New: needs Locking (G4)** | Add file-lock protocol to U4 |
| Principal holds result dict across handoff | **New: needs Serialization (G5)** | Serialize to `.hermes/results/{bead}.json` |
| U4 dependency ordering | **Resolved (G6)** | Split into U4a (polling code) + U4b (documentation) |
| Stale teacher session data | **New: needs Pruning (G7)** | Add `prune_sessions(max=10)` to U1 |
| Force-dispose fallback reliability | **New: needs Test (G8)** | Add smoke test in U5 |

## Revised Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Teacher worktree crashes mid-review | Low | Medium | Principal detects missing teacher, recreates it (G1). Bookbag retains partial verdict. |
| Orca worktree rm fails (already seen) | Medium | Low | Force-dispose fallback + **smoke test (G8)** |
| Principal crashes mid-handoff | Low | **High (G3, G5)** | **NEW** — serialize `result` dict to leaf (G5), add `--resume` flag (G3) |
| Bookbag write contention | Very Low | Medium | **NEW** — add file-lock protocol (G4) |
| Teacher session data growth | Medium | Low | **NEW** — auto-prune sessions older than 10 cycles (G7) |
| Leaf isolation is weak in Phase 1 (no process sandbox) | High | Medium | **Clarified (G2)** — leaf is a workspace, not a sandbox. Phase 2 adds terminal-based isolation. |

---

## System-Wide Impact

| Component | Impact |
|---|---|
| `conductor.py` | Major refactor — synchronous loop becomes async dispatch orchestrator |
| `orca_executor.py` | Minor additions — hardened `close_worktree()`, force-dispose fallback |
| `bookbag.py` | Minor additions — handoff helpers (`wait_for_bookbag`, signal files) |
| `director.py` | No change — `run_task()` still works the same, but called from `leaf.py` instead of `conductor.py` |
| `teacher.py` (new) | New module — ~200 lines of teacher lifecycle code |
| `leaf.py` (new) | New module — ~150 lines of leaf lifecycle code |
| `sleep_state.py` | No change — used as-is |

---

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Teacher worktree crashes mid-review | Low | Medium | Principal detects missing teacher, recreates it. Bookbag retains partial verdict. |
| Orca worktree rm fails (already seen) | Medium | Low | Force-dispose fallback (`git worktree remove` + `rm -rf`) in U5 |
| Bookbag polling timeout during long LLM calls | Low | Low | Configurable timeout (default 120s) with clear error message |
| Concurrent handoffs race condition | Very Low | High | Single-threaded polling in principal; teachers update separate fields (cto_verdict vs coo_verdict) |

---

## Documentation Plan

- `school-core/docs/HANDOFF.md` — Handoff contract and signal protocol (U4)
- `school-core/campus.md` — Update architecture description to reflect teacher/leaf model
- `school-core/AGENTS.md` — Add teacher.py and leaf.py to module index
