# School Core — Wayfinder Map (Path A era, 2026-07-31)

> Live state of the Agent School pipeline after the Path A daemon-mode refactor
> (2 fixed Python daemons in 2 fixed terminals), the `--gc-terminals` live-
> daemon guard, and the drift-check cron helper. Each session orients here
> before choosing a ticket / frontier.

---

## Destination

What does production-ready Path A look like? Find the Frontiers the current
architecture has NOT yet answered — long-running stability beyond 1 hour of
serve, drift observability at fleet scale, observability gaps, recovery story
after orca-side failure, and the surface area of older CLI flags the refactor
left behind — so next session can pick the next decision.

## Notes

- Domain: Agent School (`~/school-core`).
- Skills every session should consult: `diagnose`, `tdd`, `code-review-and-quality`.
- Standing preference: idempotent ops, no orca allocation without an existing
  handle, prefer `path:` and `code:` worktree mode over `--provider hermes`.
- Repo conventions: tiered tests (unit / integration / E2E); pytest-via-venv;
  exception messages prefixed with the function's purpose.

---

## Frontiers (F1–F5)

### F1 — Path A architecture completeness

**Hypothesis:** `_launch_serve` → 2 fixed terminals → `_teardown_serve` chain
is exercised for the happy path but the error/edge branches (orca-CLI timing
out mid-launch, send-to-terminal failing on a partially-spawned terminal,
create-worktree race on a stale registry) may not be tested.

**Evidence files:**
- `~/school-core/conductor.py` (around lines 1467–1648
  for the serve/teardown block; ~1080–1110 for serve-state I/O helpers).
- `~/school-core/tests/test_conductor_daemon.py` to see
  which branches ARE covered.
- `~/school-core/orca_executor.py` for the actual
  `_run_orca`, `create_terminal`, `close_terminal` calls in flight.

**Read-only queries:**
- `grep -n "^def _launch_serve\|^def _teardown_serve\|^def _find_or_create_terminal\|^def principal_dispatch_loop\|^def teacher_both_loop" conductor.py`
- `grep -n "def test_" tests/test_conductor_daemon.py` (coverage map)
- Cross-reference: for each error path inside serve/teardown, does a test exercise it?

### F2 — gc-terminals guard + drift-check pipeline scaling

**Hypothesis:** The guard correctly skips 2 live daemons today (verified live).
But the predicate `title.startswith("agent-school-")` and the grep on
`🔍 would close: '(no-title)'` are brittleness points — at fleet scale (say
50+ stale tabs from interrupted serves), the drift-check script must still:
(a) parse the count within a 30s orca-CLI timeout, (b) preserve the guard's
contract under high-candidate-count, (c) keep state-file writes atomic under
repeated cron overlap.

**Evidence files:**
- `~/school-core/conductor.py` (`_gc_terminals` around
  line ~1332; the `load_serve_state` helper around line ~1097).
- `~/school-core/scripts/terminal_drift_check.sh` (the
  cron helper just shipped — its grep-stdout parser).
- `~/.school-core/drift_alert_state.json` for current
  state-shape; format spec already documented in the script docstring.

**Read-only queries:**
- Walk `_gc_terminals` from candidates filter → close loop. Count how many
  `mgr.close_terminal` calls before the function returns (for cost estimate).
- Verify the script's grep anchor `🔍 would close: '(no-title)'` is invariant
  in the conductor dry-run path (per the shape-detector the reviewer added).

### F3 — Observability gap

**Hypothesis:** No metrics, no log aggregation, no heartbeat signaling in the
2 daemon loops. Either the principal or teacher-both daemon can wedge
silently (e.g., stuck in a slow LLM call) and the only signal is the
absence of bookbag writes. There's no `last_tick_at`, no `pid_alive`, no
uptime metric.

**Evidence files:**
- `~/school-core/conductor.py` — focus `principal_dispatch_loop`
  and `teacher_both_loop`.
- `~/school-core/activity_log.py` — what does `_log` record?
- `~/school-core/school_mail.py` — best-effort notify.

**Read-only queries:**
- `grep -n "metrics\|heartbeat\|last_tick\|uptime\|ping\|alive" conductor.py`
- For each `_log.*` call in the daemons, list the event types and the
  downstream consumer.

### F4 — Recovery story for orca-side failure

**Hypothesis:** When orca crashes/reboots mid-serve, the daemons die but
`serve-state.json` survives with stale handles. `--gc-terminals` correctly
refuses to close them (guard works), but there's no automated recovery: the
operator must run `--stop-serve && --serve` to refresh. The bookbag updates
made by a half-dead daemon may be lost. No `--auto-recover-on-guard-miss`
flag.

**Evidence files:**
- `~/school-core/conductor.py` — `_teardown_serve`'s
  `--serve-state-path` semantics, any `--resume` style logic.
- `~/school-core/teacher.py` — `TeacherWorktree.boot()`
  rediscover-or-create fallback.
- Orca CLI docs (via web, NOT a filesys asset).

**Read-only queries:**
- `grep -n "crash\|recover\|resume\|restart\|sticky\|heal" conductor.py teacher.py`
- Identify the "stale daemon, fresh serve-state" race explicitly.

### F5 — Orphaned CLI flags from pre-Path A

**Hypothesis:** `--list-bookbags`, `--clean-worktrees`, `--resume`, `--doubt`,
`--async`, `--issue`, `--loop`, `--rounds` are still in the argparse schema,
and the no-mutating ones (`--list-bookbags`, `--issue`, `--doubt` driven) may
still work end-to-end. The mutating ones (`--loop`, `--async`, `--resume`,
`--clean-worktrees`) read/write orca state and need live verification.

**Evidence files:**
- `~/school-core/conductor.py` — `main()` argparse and
  the dispatch routes (`_run_issue`, `_run_async_loop`, `_run_sync_loop`,
  `_resume_loop`).
- `~/school-core/tests/test_conductor_daemon.py` for
  the existing loop tests.

**Read-only queries:**
- For each flag, run `python3 conductor.py --help` and confirm the flag is
  registered. Then run the no-mutating ones to confirm graceful
  behavior (`--list-bookbags`, `--issue` against a known URL).

---

## Decisions so far

- Path A is the active architecture: 2 fixed Python daemons in 2 fixed
  terminals launched by `--serve`, replacing the 3 cron Orca automations.
- `--gc-terminals` MUST skip handles present in `serve-state.json` (Reviewer
  #8 fix shipped + 4 tests).
- `--stop-serve` runs `--gc-terminals` LAST so a re-serve always lands on
  a clean slate.
- Drift-check lives at `scripts/terminal_drift_check.sh`; exit codes
  `0`/`1`/`2`; idempotent via `mktemp` + EXIT trap.
- The 3 legacy close-time tool calls (`close_worktree` + `orca worktree
  remove` + `git worktree prune`) live in `teacher.py:TeacherWorktree.close()`
  to keep Orca's internal registry and git's admin-snapshot in sync.

*(Updated 2026-07-31 from this session's recon run — see **Recon findings**
at the end of the map.)*

- Drift-check's grep anchor `🔍 would close: '(no-title)'` is invariant:
  every `--gc-terminals --gc-terminals-dry-run` invocation emits the
  `gc-terminals: closed N orphaned terminal(s)` summary line (verified
  live + by the shape-detector the previous review added). Either side
  breaking would be caught at exit 2.
- Two `mgr.close_terminal` call sites for `_gc_terminals`:
  `conductor.py:1454` (standalone `--gc-terminals`) and `conductor.py:1632`
  (`--stop-serve`'s teardown pass). Both gated by the same serve-state
  guard; either path can run independently safely.
- `_gc_terminals` wall-clock at 200-candidate fleet is a **rough
  midpoint estimate, not measured (supersedes Cycle 1's ~60s
  figure)**: ~300ms orca-list + ~200ms/close ≈ ~40s under typical
  conditions. **However**, the `close_terminal`
  call sites at `conductor.py:1454` and `conductor.py:1632` have **no
  per-call timeout** — a wedged terminal can block the loop far beyond
  the 15s list call. Pinning this down is F3 (observability) +
  F4 (recovery) territory; do NOT treat 40–60s as a hard cap.
- All 24 argparse flags in `main()` are wired or are scalar parameter
  modifiers (rough count — not a per-func audit; some entries are
  `dest=` aliases not strict wiring). The "orphaned flags" hypothesis
  (F5) was OVERSTATED — most reach a function via `if args.X:` branch,
  the rest are scalar args (`--task`, `--domain`, `--repo`,
  `--daemon-interval`, etc.). What's actually missing is **fresh live
  tests** for most of them, not missing dispatch.
- `_resume_loop` at conductor.py:848 IS wired (called from `if args.resume`
  at line 487). The "it boots fresh teachers even when old worktree
  handles exist" duplicate-terminals side-effect claim comes from the
  function's own docstring comment at conductor.py:901–905 ("NOTE:
  _boot_teachers() always creates new terminals and starts new run_loop()
  processes, even when TeacherWorktree.boot() rediscovers existing
  worktrees from a pre-crash session..."). **NOW recon-verified**:
  the claim is true for `_resume_loop`'s `_boot_teachers()` call (line
  906) but **does NOT apply** to Path A's `--serve` (which uses
  `_find_or_create_terminal` with title-dedup at lines 1527–1528 and
  only mints fresh terminals at the fallthrough lines 1290/1299). See
  F4 in **Recon findings** for the full scope analysis.

- **F1 asymmetry confirmed:** `_launch_serve` (lines 1467–1513 happy path)
  has **NO explicit try/except handling** in its function body; orca
  errors propagate to `main()` and crash, leaving `serve-state.json`
  partial or orca terminals orphaned. `_teardown_serve` (lines 1557–1640)
  has 3 try/except blocks (lines 1568, 1631, 1638) — best-effort cleanup.
  Path A's happy-path state correctness depends entirely on orca being
  reliable during launch. **Not a regression** — a pre-existing limitation
  that was untestable without a flaky-orca harness.
- **`_launch_serve` only mints `agent-school-*` terminals on fallthrough.**
  `_find_or_create_terminal` at lines 1279–1300 first scans for an existing
  terminal with the same title (title-dedup at line 1288-1289, returns the
  existing handle). Only the two fallthrough lines (1290, 1299) call
  `mgr.create_terminal(...)`. So a `--serve` re-run after a clean
  `--stop-serve` always lands at exactly 2 `agent-school-*` terminals,
  and a `--serve` re-run WITHOUT `--stop-serve` REUSES the existing
  2 terminals (no spray).
- **`TeacherWorktree.boot()`** at teacher.py:146 has the explicit
  rediscover-or-create semantic (docstring at lines 158–163). Worktree
  side is correctly handled; the prior suffix-spray concern was for
  legacy code paths only.

## Not yet specified

- What is the canonical observability surface for Path A? No metrics, no
  log aggregation, no heartbeat — **still open** (F3 only recon-confirmed
  the gap; not yet specified HOW to close it).
- Does `--doubt` end-to-end work with current OmniRoute builds? (No
  live test; only unit-test coverage in `_principal_dispatch`.)
- Should `--resume` mark itself deprecated for Path A users since
  `--stop-serve && --serve` is now the canonical refresh path? (Tied to
  F4 mitigation.)

*(F1 completeness and F4 duplicate-terminal scope were OPEN at end of
Cycle 1 and are RESOLVED in Cycle 2 — see below.)*

---

## Recon findings

Cumulative recon from **two cycles**:

### Cycle 1 (2026-07-31, prior session)

Recon agents: file_picker (errored — long prompt issue), code_searcher
(51 matches). Confirmed F2 (drift scaling — grep-anchor invariant,
~40–60s wall-clock estimate). Overturned F5 (orphaned flags are wired
or scalar — the gap is live test coverage). F1 was deferred, F4 was
partial (docstring claim only).

### Cycle 2 (2026-07-31, this session) — F1 + F4 recon

Recon agents: file_picker (errored AGAIN — same prompt-length class of
failure, see Pitfalls below), code_searcher (66 matches, broader scope),
basher (verbatim source reads for `_launch_serve`, `_teardown_serve`,
`_gc_terminals`, `_resume_loop`, `TeacherWorktree.boot()`).

**Cycle 2 file_picker gotcha**: the agent returned TypeScript paths from
a different repo (e.g. `packages/backend/src/path-a/index.ts`). This is
cross-repo contamination from a previous session's cache; **NOT** treated
as evidence. The `code_searcher` + `basher` recon covered the same surface
and is trustworthy.

#### Cycle 2 F1 verdict — Path A completeness

`_launch_serve` (lines 1467–1513 happy path, plus 1527–1560 orchestration):
**zero try/except in the function body.** Orca-call errors propagate
straight to `main()` and crash with traceback. The "fragility" is real
but pre-existing — Path A didn't introduce it; the assertion that
launch always succeeds has been an assumption since the `--serve` path
was added. Test coverage is happy-path only (TestLaunchServe: 4 tests).

`_teardown_serve` (lines 1557–1640): **3 try/except blocks** (lines 1568,
1631, 1638) wrapping handle-lookup, close loop, and the gc_run. Robust
best-effort cleanup. Test coverage is decent (TestTeardownServe: 4 tests,
TestTeardownWithGc: 2 tests).

`_resume_loop` has **ZERO direct unit tests.** This is the only blank
spot in the test map.

**Conclusion (F1)**: happy paths covered; asymmetry = launch-fragile /
teardown-robust. Not a Path A regression. **Document as a known fragility**
in the README, not fix-it-now territory.

#### Cycle 2 F4 verdict — Recovery scope clarification

`_resume_loop`'s NOTE at conductor.py:901–905 is recon-verified:
`_boot_teachers()` (called at line 906) does create new orca terminals
and new run_loop() processes even when `TeacherWorktree.boot()` would
rediscover the worktree. **This is intentional pre-Path-A behavior**, not
a bug; the function author documented it as such.

Path A's `--serve` does NOT call `_boot_teachers()`. It calls
`_find_or_create_terminal(mgr, "agent-school-principal")` and
`_find_or_create_terminal(mgr, "agent-school-teacher-both")` at lines
1527–1528. `_find_or_create_terminal`'s title-dedup (lines 1288-1289)
returns the existing handle on re-serve. Only the two fallthrough paths
(lines 1290, 1299) call `mgr.create_terminal(...)`. So a Path A
`--serve` re-run NEVER mints duplicate `agent-school-*` terminals.

`TeacherWorktree.boot()` at teacher.py:146 is the canonical rediscover-
or-create for the WORKTREE side. The Path A refactor also moved the
REVIEW-TERMINAL out of `boot()` per teacher.py:159–163 ("boot() NO LONGER
spawns a teacher-*-review terminal. The review loop is owned by an Orca
automation...").

**Conclusion (F4)**: the duplicate-terminal side effect is **scoped
to `--resume` on the legacy bookbag-recovery path.** Path A's
`agent-school-teacher-both` is owned by `_launch_serve`, NOT
`_boot_teachers()`, and is safe under re-serve. Mitigation: in README,
note that `--resume` is for pre-Path A use; Path A operators use
`--stop-serve && --serve` to refresh. **No code change needed.**

#### Pitfalls logged this cycle
- file_picker reliably fails on long instructional prompts (Cycle 1
  errored, Cycle 2 returned cross-repo contamination). Workaround:
  keep prompts to <=2 lines for file_picker; route all deep structure
  to code_searcher + basher which handle context better.
- The previous-cycle claim "50-line wait_for_verdicts timeout" was
  unverified; the actual `args.handoff_timeout` default is 120s per
  the argparse help at conductor.py:335–336. Cycle 1 missed that.

## Out of scope

- Replacing Path A with a different architecture (commit path stays).
- Auto-scaling daemon count based on bead volume (design is fixed at 2).
- Multi-instance serve across remotes (single-tenant only).
- Replacing `bd` with a different tracker (current policy use).
