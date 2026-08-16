# Worst-Day-Ever: School-Core Scale Architecture

> Adversarial stress-test of `docs/school-core-scale-architecture.md`, following
> the **worst-day-ever** methodology (8 attack dimensions, 6 safeguards,
> severity-ranked disaster report).
>
> Mode: **dry-run design review** (no execution, synthesized scenarios, no
> secrets, no external calls, no mutation — per the 6 safeguards).
> Purpose: place an **error-boundary node** at each dimension so the specific
> worst-day failure is *caught before* it reaches students/GitHub/Orca.

## The 8 dimensions → mapped to the school topology

| # | Dimension | School surface it attacks |
|---|---|---|
| 1 | Input Boundary | Issue bodies → crew briefs; agent names; bookbag payloads |
| 2 | State Machine Torture | crew registry lifecycle; bookbag handoff; grading-job transitions |
| 3 | Temporal / Timing | CREW_TIMEOUT (900s) vs 30-min job; clock skew; counter races |
| 4 | AuthN/Z Shadow | worktree/bookbag cross-tenant; role scope mismatch |
| 5 | Data Integrity Cascade | `crew_runs.json` corruption; `scores.json` wipe; verify-contract false pass |
| 6 | Concurrent Load | 20+ graders; lock escalation; fleet exhaustion; thundering herd |
| 7 | External Dependency Failure | Orca daemon crash; FirstMate down; GitHub API timeout |
| 8 | Brownfield Mining | replay real past failures (16-turn timeout, `node: command not found`) |

---

## DIMENSION 1 — Input Boundary

**Worst day:** A GitHub issue body contains (a) a 2 MB Unicode RTL-override
string, (b) null bytes, (c) shell metacharacters, (d) a `NaN`/`Infinity` in a
numeric field the director parses. The brief writer (`_write_brief`) concatenates
it into the crew launch brief and the Hermes task prompt. A student agent
executes a crafted instruction hidden by RTL override; a shell metacharacter in
the issue title reaches `orca terminal create` quoting.

**Error-boundary nodes:**
- **N1.1 — Sanitize at the curriculum edge.** `github_fetcher` must scrub
  issue title/body through the *same* `_scrub_comment_text` already used elsewhere
  (confirmed in-use at issue_bridge.py:270/274) before anything downstream sees
  it. Reject-or-escape RTL overrides, strip null bytes, cap length.
- **N1.2 — Quoting contract at spawn.** `dispatch_crew` already shells out via
  `_run()` (subprocess list-args, not shell) — keep it that way; never
  interpolate issue text into a `shell=True` string. Add a regression test that
  fires a `; rm -rf` title through the full brief path.
- **N1.3 — Numeric guard.** Director domain/difficulty overrides must coerce
  with `math.isfinite` before arithmetic; `NaN`/`Inf` → fall back to default,
  never propagate.

---

## DIMENSION 2 — State Machine Torture

**Worst day:** (a) A crew's `running` record is double-written (retry logic
re-dispatches the same issue while the first is still polling) → two Orca
worktrees for one issue, both write the same bookbag, grading queue gets two
jobs. (b) The `BookbagSignal` flag-file handoff races: student writes
`done.flag` while the reviewer bot reads the bookbag JSON mid-write → partial
JSON. (c) A grading job is ACKed twice → graded twice, score updated twice
(EMA double-applied).

**Error-boundary nodes:**
- **N2.1 — Idempotency key on issue_number.** `_crew_active_issue(CREW_RUNS_FILE, num)`
  already skips an issue whose crew is still active (issue_bridge.py:1009). Extend
  it to the *grading queue*: a job keyed by `(issue_number, crew_id)` is
  deduplicated before enqueue. Re-dispatch of an in-flight issue → no-op.
- **N2.2 — Atomic bookbag writes.** `bookbag.py` must write via `os.replace(tmp, path)`
  (atomic rename), never in-place `write_text`. The `BookbagSignal` flag is
  already a separate handoff file — keep the *data* write atomic so a concurrent
  reader never sees partial JSON.
- **N2.3 — Grading-job ACK + idempotent scoring.** Grader marks a job
  `consumed` in the queue *before* scoring; a second delivery is dropped. Score
  update is keyed by crew_id so a replay is a no-op, not a second EMA.

---

## DIMENSION 3 — Temporal / Timing

**Worst day:** A crew polls for the full CREW_TIMEOUT_SECONDS=900s, but the CI
job is hard-capped at **30 minutes = 1800s** (school-loop.yml:139). Two crews at
cap=2 each take 900s → 1800s exactly, but the *grader* then needs time → job
killed mid-grade, bookbag written, score never recorded, GitHub issue left
unlabeled. Clock skew between the Mac and GitHub makes `started_at` comparisons
wrong, so `sweep_stale_runs` reclaims a live crew as "stale."

**Error-boundary nodes:**
- **N3.1 — Budget-aware admission (already partly exists).** `decide_admission`
  checks `remaining < crew_timeout + reserve` (crew_admission.py:35). Enforce it
  *per in-flight crew*, not just per-cycle: with cap=2, reserve 2×timeout +
  grading budget. If the cycle can't fit N crews, admit fewer.
- **N3.2 — Monotonic clock for lifecycle.** Use `time.monotonic()` (not wall
  clock) for all `started_at`/stale comparisons in `sweep_stale_runs` and
  `_record_run`. Wall-clock skew must never cause a live crew to be reclaimed.
- **N3.3 — Graceful job-deadline handoff.** When the 30-min boundary approaches,
  in-flight crews are allowed to finish but *new* dispatch stops; graders get a
  soft deadline that flushes the queue to durable state rather than dying
  mid-write.

---

## DIMENSION 4 — AuthN/Z Shadow

**Worst day:** (a) A `student-coder` crew, resolving capability for issue A, is
handed a worktree that still contains issue B's uncommitted diff (Orca worktree
reuse / cleanup race) → cross-contamination of student output. (b) A teacher
(CTO/COO) bot, reviewing issue A's bookbag, reads issue B's bookbag because the
path is derived from a wrong bead id → horizontal escalation across issues.
(c) A `force_agent` override lets a low-trust role run with a high-trust profile.

**Error-boundary nodes:**
- **N4.1 — Worktree isolation invariant.** `dispatch_crew` already creates a
  unique `worktree_id` per crew (crew_dispatch.py:805). Add a **pre-verify**:
  before a student writes, assert the worktree contains *only* issue A's
  baseline (git-status clean except the scaffold). Any foreign diff → abort +
  alert (the fc7.3.2 "verify crew patch before teardown" lesson, extended to
  pre-flight).
- **N4.2 — Bookbag path bound to bead id + capability scope.** Bookbag paths are
  derived from `bead_id` (compound_learning / agentmail_poller). Enforce that a
  reader's role is *authorized* for that bead (teacher yes, sibling-student no).
  Wrong-id read → 403-equivalent (refuse, log).
- **N4.3 — `force_agent` allowlist.** `force_agent` must be constrained to the
  capability's own `profile`; a role cannot escalate to a higher-trust profile.

---

## DIMENSION 5 — Data Integrity Cascade

**Worst day (the sharpest):** `crew_runs.json` is truncated by a crashed write →
on next read, `_load_runs` hits `except (OSError, json.JSONDecodeError): return []`
(crew_dispatch.py:427–428). The registry **silently becomes empty** → admission
sees 0 in-flight → **over-admits** → fleet exhaustion. Parallel: `scores.json`
is torn by the lock-free `ScoreStore.save()` → on reload, `load()` hits
JSONDecodeError and **re-seeds from SEED_AGENTS, wiping the entire leaderboard**.
And the verify-contract killer: a grader, unable to read the crew's persisted
verification, **re-runs the gate on the clean cached base** (`repo_path`) →
false PASS (the exact fc7.3 hazard).

**Error-boundary nodes:**
- **N5.1 — Never silently swallow registry corruption.** `_load_runs` must
  distinguish "file absent" (valid → `[]`) from "file corrupt" (invalid →
  quarantine to `crew_runs.json.corrupt-<ts>`, alert, and **fail closed**:
  assume MAX in-flight so admission denies rather than over-admits). This is the
  single highest-value node.
- **N5.2 — Lock-safe ScoreStore (already identified, §4.1 of design).** Wrap
  `save()` in the *same* `fcntl.flock` pattern the crew registry uses
  (`_registry_lock`). A torn write can no longer corrupt the ledger; reload can
  fall back to the `.lock`-guarded temp file.
- **N5.3 — Verify-contract hard invariant.** Grading MUST use
  `crew_premerge_verification` from the bookbag; if it is absent, the grade is
  **FAIL (verification_unavailable)**, never a re-run on `repo_path`. Reusing
  the clean base after teardown is forbidden by contract (enforced at
  issue_bridge.py:1368 today — extend to the grading queue).

---

## DIMENSION 6 — Concurrent Load

**Worst day:** 20 graders finish near-simultaneously → 20 concurrent
`ScoreStore.save()` calls (lock-free) → write skew + leaderboard corruption.
The fleet hits capacity and 20 new crews queue; the dispatch office's
least-loaded selector has a race → two crews assigned the same worktree. The
`school-core-live-orca` CI concurrency lock (school-loop.yml:130) serializes two
execute jobs → a stalled job holds the lock → the second waits 30 min → both
time out (deadlock-by-lock).

**Error-boundary nodes:**
- **N6.1 — Bounded grader pool + lock-safe ledger.** Graders run in a
  `ThreadPoolExecutor(max_workers=G)` where G ≤ fleet capacity; all score writes
  go through N5.2's locked `save()`. No unbounded concurrency on the ledger.
- **N6.2 — Worktree lease.** The dispatch office issues a **lease** per
  worktree (claimed in the locked registry). Two assignments to one worktree →
  second sees the lease held → picks the next free one. No race.
- **N6.3 — Per-daemon CI lock, not global.** `concurrency.group` should key on
  the Orca daemon identity (`school-core-live-orca-<daemon>`), not one global
  group — so a fleet of N daemons runs N execute jobs without mutual lock-out,
  and a single stalled job only blocks its own daemon.

---

## DIMENSION 7 — External Dependency Failure

**Worst day:** The Orca daemon crashes mid-worktree → the student's process
dies; `dispatch_crew`'s poll sees the terminal gone → `CrewUnavailableError` →
fallback to direct path, but the **worktree is orphaned** (Orca-local identity
lost). FirstMate is down → every spawn fails → the whole cycle falls back to
direct model path, overloading OmniRoute. GitHub API times out during
`_mark_github_issue` → the issue is graded but never labeled → board desync.

**Error-boundary nodes:**
- **N7.1 — Orphan reclamation + cap.** `sweep_stale_runs` already reclaims
  stale records (crew_dispatch.py:554). Strengthen: on daemon crash, the
  dispatch office marks affected crews `aborted`, triggers worktree GC, and
  **caps** retries so a dead daemon doesn't loop. The existing
  `CrewUnavailableError → direct fallback` is the right shape; keep it.
- **N7.2 — Graceful dependency degradation (matches repo idiom).** Every
  external call already follows "timeout → return None/empty" (github_fetcher,
  cocoindex_client, context_orchestrator all do this). The grading queue must
  treat GitHub-label failure as *non-fatal*: record the grade, queue the label
  write for retry, never block grading on GitHub.
- **N7.3 — Backpressure on fallback.** If FirstMate is down, cap direct-path
  fallbacks per cycle (don't let 20 crews all hammer OmniRoute at once) — a
  simple semaphore on the fallback path.

---

## DIMENSION 8 — Brownfield Mining

**Worst day (replay real history):** The design's own beads record the actual
past disasters — replay them as regression scenarios:
- `fc7.3.6` note: *"16-turn replay reached FirstMate/Orca but Hermes timed out
  at 360s with clean teardown."* → re-run at cap=2 to confirm budget math holds.
- `issue_bridge.py:154` note: *"crew spawn failed with 'node: command not
  found' from backends/orca.sh:27"* → if `node` is missing on a fleet daemon,
  spawn must fail closed + alert, not hang.
- `fc7.3` acceptance: *"target checkout and Orca resources remain clean"* → a
  student must never leave the Orca daemon dirty for the next student.

**Error-boundary nodes:**
- **N8.1 — Regression suite from beads.** Each historical failure becomes a
  permanent test: timeout-at-scale, missing-node spawn, worktree-cleanup-after-
  crew. The worst-day-ever run itself adds to this suite.
- **N8.2 — Resource-clean assertion.** Post-cycle, assert Orca worktrees and
  FM-local state are clean; a leftover is a test failure, surfaced (not silent)
  — directly enforcing the fc7.3 acceptance criterion at fleet scale.

---

## Disaster Report (design-phase)

Resilience is scored 0–10 per dimension for the **current design + proposed
boundary nodes** (nodes not yet implemented score lower).

| Dim | Name | Score | Worst finding (pre-node) | Node that closes it |
|---|---|---|---|---|
| 1 | Input Boundary | 4 | RTL/shell injected into brief | N1.1–N1.3 |
| 2 | State Machine | 5 | Double-dispatch + bookbag race | N2.1–N2.3 |
| 3 | Temporal | 3 | 900s crew vs 30-min job cascade | N3.1–N3.3 |
| 4 | AuthN/Z Shadow | 4 | Worktree/bookbag cross-tenant | N4.1–N4.3 |
| 5 | Data Integrity | **2** | `_load_runs` swallows corruption → over-admit; `ScoreStore` wipe | **N5.1, N5.2, N5.3** |
| 6 | Concurrent Load | 3 | Lock-free ledger + global CI lock deadlock | N6.1–N6.3 |
| 7 | External Dep | 6 | Orphan worktree + GitHub-label desync | N7.1–N7.3 |
| 8 | Brownfield | 5 | Historical failures not yet regression-tested | N8.1–N8.2 |

**Overall resilience: 4.0 / 10** (design-only; nodes are proposed, not coded).

**CRITICAL (must land before any scale-up):**
- **N5.1** — stop swallowing `crew_runs.json` corruption (over-admission root cause).
- **N5.2** — lock `ScoreStore.save()` (leaderboard wipe on reload).
- **N5.3** — forbid verify re-run on clean base (false-pass root cause).

**HIGH:**
- N3.1 budget-aware admission at cap=2; N6.3 per-daemon CI lock; N4.1 worktree
  isolation pre-verify.

---

## Next step

These nodes are a **design contract**. The three CRITICAL nodes (N5.1, N5.2,
N5.3) are small, independent, and implementable now without the full scheduler.
Recommended: land them as the first build slice, then re-run this grill to lift
Dimension 5 from 2 → 8.
