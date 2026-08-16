# School-Core at Scale: 20+ Parallel Students

> Design artifact — NOT yet implemented. Informs the next build phase.
> Pedagogy-first. Orca stays as the execution truth-source.

## 0. Why this document exists

The original pipeline was a **single serial loop** (`issue_bridge.bridge_issues`):
one teacher, one whiteboard (the Mac's single Orca daemon), one student at a
time. That loop *proved the learning contract* — a student produces verified,
two-judge-reviewed, graded work, persisted to a bookbag. That contract is the
project's own gate (`fc7.3.6`: "concurrency 1 … not permission to increase
concurrency").

Scaling to **20+ students in parallel** is not "a bigger whiteboard." It is a
different topology: a **school** with a dispatch office, a fleet of classrooms
(Orca daemons), and a grading department that reviews finished work
*asynchronously* from dispatch. This doc designs that topology using the
modules already present in the repo.

**Orca is not replaced.** Orca is where a coding student's code is actually
written, built, and tested in a real worktree — the source of the verify
contract's authority. Scaling means *more Orca capacity* (a fleet of daemons /
machines), not a different substrate. Bot Mode (`school_bot_execute`) is kept
for *non-coding* roles (research, triage, docs) that don't need a worktree.

## 1. The school metaphor → module map (already built)

| School role | Existing module | Scale behavior |
|---|---|---|
| **Curriculum router** | `github_fetcher.fetch_issues` (domain/difficulty classification) | Stateless; already safe to call per issue. |
| **Student identity** | Hermes SOUL / `capabilities.resolve_capability` | Each role = a profile + capability bundle. |
| **Classroom / execution** | `crew_dispatch.dispatch_crew` → FirstMate → **Orca worktree** | One worktree per crew; fleet = many daemons. |
| **Locker / portfolio** | `bookbag.py` (per-bead JSON + `BookbagSignal` flag-file handoff) | Already async-handoff ready. |
| **Administration / tracking** | Kanban board state (`activity_server.py`), `_mark_github_issue` | Observability; one board, many in-flight. |
| **Institutional memory** | `compound_learning.CompoundLearningStore` + `_record_compound_observation` | Cross-cycle learning. |
| **Admission office** | `crew_admission.decide_admission` + `_crew_active_count` (lock-safe `fcntl` registry) | **Already concurrency-safe** — the fc7.3 repair. |
| **Grading department** | Two-judge CTO+COO review (`ReviewPacket`, `adversarial_reviewer`) + verify gate | Must become a *queue consumer*, not inline. |
| **Transcript / ledger** | `scoring.ScoreStore` | **NOT lock-safe today** — must fix before scale. |

The only two pieces missing for 20+ are: (a) a **dispatch scheduler** that fans
issues out to available Orca worktrees, and (b) a **grading queue** so review
is decoupled from dispatch. Everything else already exists.

## 2. Target topology (three tiers, producer/consumer)

```
            ┌─────────────────────────────────────────────┐
            │  CURRICULUM  (github_fetcher + capabilities) │  pull + classify issues
            └───────────────────────┬─────────────────────┘
                                     │ issues (domain, difficulty, role)
                                     ▼
            ┌─────────────────────────────────────────────┐
            │  DISPATCH OFFICE  (NEW: school-scheduler)     │  capability-aware assignment
            │  - admission via lock-safe registry          │  to AVAILABLE worktrees
            │  - bounded by fleet capacity                 │
            └───────────┬───────────────────────┬──────────┘
         dispatch N crews                grading jobs enqueued
                        ▼                        ▼
   ┌───────────────────────────┐      ┌───────────────────────────────┐
   │ ORCA FLEET (many daemons) │      │ GRADING QUEUE (NEW)           │
   │ each hosts K worktrees    │      │ finished crews → teacher bots  │
   │ student works + verifies  │      │ run two-judge + verify gate   │
   │ writes bookbag            │      │ score → lock-safe ledger      │
   └────────────┬──────────────┘      └───────────────┬──────────────┘
                │ report.md + verification              │ graded result
                ▼                                      ▼
            ┌──────────────────────────────────────────────────────┐
            │  OBSERVABILITY  (Kanban board, compound_learning)     │  one board, many lanes
            └──────────────────────────────────────────────────────┘
```

Dispatch and grading are **independent pipelines** joined only by the
bookbag + a job record. A student finishing does not block the next dispatch;
a grader stalling does not block the next student. Throughput is limited by the
*minimum* of (fleet size, grader concurrency, ledger write throughput) — not by
one teacher watching one whiteboard.

## 3. The two new components

### 3.1 Dispatch Office (`school_scheduler.py`, NEW)
- Owns the **admission decision** using the *existing* lock-safe
  `_crew_active_count(CREW_RUNS_FILE)` (already concurrency-correct from the
  fc7.3 repair). `dispatch_crew` writes a `running` record on spawn, so a
  concurrent decision observes in-flight crews and `configured_cap` holds.
- Maps an issue → a **capability/role** (reuses `resolve_capability`) → an
  **available Orca worktree** in the fleet (round-robin / least-loaded across
  daemons). FirstMate already targets a specific Orca backend per spawn.
- Bounded by **fleet capacity**, not the old `CREW_MAX_PER_CYCLE` serial cap.
  `CREW_MAX_PER_CYCLE` becomes "max in-flight per scheduler cycle," safe to
  raise because admission is lock-safe.
- On crew `done`: enqueues a **grading job** (bookbag path + verification
  + report) to the grading queue; does NOT grade inline.

### 3.2 Grading Department (`school_grader.py` + queue) — IMPLEMENTED

> Status: landed as of this slice. The grading queue + consumer exist and are
> tested (`tests/test_school_grader.py`, 9 tests). The dispatch-office
> scheduler (§3.1) is the next slice.

- `GradingQueue` — file-backed durable JSONL queue (data/grading_queue.jsonl),
  fcntl-locked for concurrent enqueues from a fleet of dispatchers, dedup by
  `(issue_number, crew_id)` (N2.1).
- `grade(job, ...)` consumer — finalizes one finished job: two-judge acceptance
  decision (reuses `ReviewPacket`, never re-gates the clean base — N5.3),
  idempotent lock-safe `ScoreStore` write (N5.2 / N2.3 — replay = no-op),
  `compound_learning` observation (fail-soft), GitHub label via the non-fatal
  `LabelWriteQueue` (N7.2). Never raises.
- `drain(queue, ...)` — processes all pending jobs in **bounded waves**
  (`bounded_grader_pool_size`, N6.1) so ledger writes never exceed what the
  lock-safe store can absorb.
- CLI: `python -m school_grader --drain [--score-store PATH]
  [--compound-store PATH] [--max-workers N]` — a standalone pipeline stage.
- Integration seam: `issue_bridge.process_issues` enqueues a `GradingJob` on
  every successful task (non-fatal). At cap=1 the inline finalization still
  runs (behavior unchanged); the queue is the ready hook for the 20+ separate
  grading stage. The consumer's idempotency means the inline+queue pairing
  never double-grades.

This extraction is *mechanical*: the review/grade logic already lived in one
cohesive block; it is now reachable behind a queue consumer, decoupled from
dispatch. The actual switch (inline finalization → separate drain stage)
happens when concurrency rises, not before.

## 4. Must-fix before 20+ (small, known)

1. **`ScoreStore` lock** (`scoring.py:save`): `open("w")` + `json.dump` with
   no lock. At 20 concurrent graders this risks a torn write / leaderboard
   wipe on next `load()`. Fix: wrap `save()` in the *same* `fcntl.flock`
   pattern already used by the crew registry (`_registry_lock`). Trivial,
   high-value, should land regardless of scale work.
2. **Orca fleet capacity**: define how daemons are addressed (env / registry
   of Orca endpoints). Today one daemon + 30-min CI job timeout is the real
   ceiling; the fleet removes both.
3. **CI lock relaxation**: `school-loop.yml` `school-core-live-orca`
   concurrency group currently forbids two `execute` jobs. With a fleet, the
   lock should key per-daemon, not globally.

## 5. Discipline: respect the fc7.3.6 gate

`fc7.3.6` (still OPEN) is the project's own check: prove the single-student
full-path pilot passes *before* raising concurrency. This design does **not**
bypass it — the 2-concurrency step (loop refactor + `CREW_MAX_PER_CYCLE=2`)
remains the *last validation of the single-student contract*, and the fleet/queue
work builds on top of that proven base. Recommendation: close `fc7.3.6` first.

## 6. Incremental path (no big-bang rewrite)

1. **Land the `ScoreStore` lock** (§4.1) — independent, safe now.
2. **Close `fc7.3.6`** — concurrency-1 pilot, prove contract.
3. **2-concurrency** — loop refactor + raise cap (already designed earlier).
4. **Extract grading into a queue consumer** (§3.2) — decouples grading.
5. **Dispatch office + Orca fleet** (§3.1) — the actual 20+ scale-out.

Each step is independently testable and keeps Orca as the classroom.

## 7. Open questions for the architect (you)

- Fleet shape: multiple Orca daemons on one Mac, or across machines? (Sets
  the addressing model in §4.2.)
- Grading queue: in-repo file broker (matches current zero-dep style) or a
  real broker? (Recommend file-backed for now.)
- Does the 2-concurrency loop refactor (step 3) still make sense as an
  intermediate, or go straight to the scheduler once `fc7.3.6` closes?
