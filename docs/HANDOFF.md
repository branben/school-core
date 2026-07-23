# Handoff Contract — Bookbag State Machine, Signal Protocol, File-Lock Rules & Lifecycle Contract

> The formal handoff contract between Principal, Student Leaves, and CTO/COO
> Teachers. Defines how work moves between participants, who owns what state,
> and the guarantees each participant makes.

---

## Table of Contents

1. [Bookbag State Machine](#1-bookbag-state-machine)
2. [Signal Protocol](#2-signal-protocol)
3. [File-Lock Rules](#3-file-lock-rules)
4. [Teacher Lifecycle Contract](#4-teacher-lifecycle-contract)
5. [Leaf Lifecycle Contract](#5-leaf-lifecycle-contract)
6. [Timeout Configuration Reference](#6-timeout-configuration-reference)
7. [Error Recovery](#7-error-recovery)

---

## 1. Bookbag State Machine

The bookbag (`~/.hermes/bookbag/<bead>.json`) is the **ground-truth
artifact** that flows through the entire pipeline. Every participant reads
and writes the bookbag — nobody trusts in-memory reports.

### States

> **Phase 1 (synchronous) shortcut:** In the current Phase 1 implementation,
> both verdicts are filled inline inside `run_task()` in the principal process
> (via `_run_two_judge_review()`). The bookbag transitions directly from
> **WRITTEN → REVIEWED** — the CTO_REVIEWED and COO_REVIEWED intermediate
> states are skipped. The async path (Phase 2, future) will use the full
> sequential state machine below.

```
    ┌────────────┐
    │  WRITTEN   │  Leaf writes bookbag with empty verdicts
    └─────┬──────┘
          │
    ┌─────▼──────┐      ◄── Phase 1 shortcut: WRITTEN → REVIEWED
    │  CTO_REV'D │  Teacher CTO fills cto_verdict (PASS|FAIL)        ──── Phase 2 only
    └─────┬──────┘
          │
    ┌─────▼──────┐
    │  COO_REV'D │  Teacher COO fills coo_verdict (PASS|FAIL)        ──── Phase 2 only
    └─────┬──────┘
          │
     ┌────▼────┐
     │ REVIEWED│  Both verdicts present and accepted is computed
     └────┬────┘
          │
     ┌────▼──────┐
     │ SCORED    │  Principal calls evaluate_and_update() — in Phase 1 this
     └────┬──────┘  is done inside run_task() before signal_ready()
          │
     ┌────▼──────┐
     │ DISPOSED  │  Leaf worktree removed; bookbag archived
     └───────────┘
```

### State table

| State | `cto_verdict` | `coo_verdict` | `accepted` | Who transitions |
|---|---|---|---|---|
| **WRITTEN** | `""` | `""` | `false` | Leaf via `write_bookbag()` |
| **CTO_REVIEWED** | `"PASS"` / `"FAIL"` | `""` | `false` | CTO via `locked_update_bookbag()` |
| **COO_REVIEWED** | `""` | `"PASS"` / `"FAIL"` | `false` | COO via `locked_update_bookbag()` |
| **REVIEWED** | `"PASS"` / `"FAIL"` | `"PASS"` / `"FAIL"` | `true` / `false` | Second teacher or Principal via `write_bookgap()` |
| **SCORED** | same | same | same | Principal via `evaluate_and_update()` |
| **DISPOSED** | archived | archived | archived | Principal via `leaf.dispose()` |

### State transition rules

1. **WRITTEN → REVIEWED**: Both verdicts must be non-empty. If CTO and COO
   both write `"PASS"`, `accepted` is set to `true`. If either writes `"FAIL"`,
   `accepted` is set to `false`.

2. **CTO_REVIEWED + COO_REVIEWED race**: Since CTO and COO poll independently
   and update *different* fields (`cto_verdict` vs `coo_verdict`), the two
   writers can safely run concurrently **as long as** each only writes its own
   field. The file-lock protocol (Section 3) prevents the second writer from
   overwriting the first's field via the read-modify-write race.

3. **Validation guard**: `validate_bookbag()` enforces that `accepted=true`
   implies both verdicts are `"PASS"`. Any bookbag violating this invariant
   is flagged as inconsistent.

### Schema (from `bookbag.py`)

```python
{
    "bead": str,           # unique task ID, e.g. "coder-python-coding-a1b2c3d4"
    "task": str | None,    # task description
    "student": str,        # role that handled it (coder, searcher, etc.)
    "domain": str,         # task domain
    "difficulty": str,     # easy / medium / hard / blocker
    "output": str,         # student's raw LLM response
    "lens": str,           # review lens applied ("cto" / "coo")
    "cto_verdict": str,    # PASS or FAIL (technical correctness)
    "coo_verdict": str,    # PASS or FAIL (completeness / acceptance criteria)
    "findings": [          # combined review findings
        {"section": str, "severity": str, "description": str, ...}
    ],
    "ac_met": [str],       # acceptance criteria satisfied
    "files_changed": [str],
    "verification": str,   # build/test output
    "summary": str,        # human-readable summary
    "blockers": [str],     # anything blocking completion
    "accepted": bool,      # true if both CTO and COO passed
    "timestamp": str       # ISO-8601 UTC
}
```

---

## 2. Signal Protocol

The signal protocol provides **fast flag-file signaling** so teachers can
detect ready bookbags without parsing JSON on every poll.

### Data flow

```
Leaf                                           Teacher
 │                                               │
 ├── write_bookbag(bead, ...)                    │
 │     → ~/.hermes/bookbag/<bead>.json           │
 │                                               │
 ├── BookbagSignal(bead).ready()                 │
 │     → ~/.hermes/signals/<bead>.ready  ────────┤
 │                                               ├── poll: signal.check()?
 │                                               │     → reads .ready file
 │                                               │
 │                                               ├── if ready:
 │                                               │     read_bookbag(bead)
 │                                               │     → ~/.hermes/bookbag/<bead>.json
 │                                               │
 │                                               ├── locked_update_bookbag(...)
 │                                               │
 │                                               └── BookbagSignal(bead).clear()
 │                                                     → removes .ready file
```

### BookbagSignal API

```python
class BookbagSignal:
    """Fast flag-file signaling for bookbag handoff.

    Instead of polling the bookbag JSON file (which requires parsing JSON
    on every poll), use a lightweight `.ready` flag file.
    """

    def ready(self) -> None:
        """Signal that this bead's bookbag is ready for review.
        Writes ~/.hermes/signals/<bead>.ready
        """
        ...

    def check(self) -> bool:
        """Check if the ready signal exists."""
        return self._ready_path.exists()

    def clear(self) -> None:
        """Clear the ready signal after handoff is complete.
        Removes ~/.hermes/signals/<bead>.ready
        """
        ...
```

### Why flag files instead of bookbag polling?

| Approach | Cost per poll | False positives | Notes |
|---|---|---|---|
| Parse bookbag JSON | 1 read + 1 JSON parse | None | Expensive at 5s intervals |
| Check `.ready` file | 1 `os.stat()` | Stale signals not cleared | Fast, lightweight |
| Combined | 1 `os.stat()` + 1 read when ready | None | Default strategy |

The **combined** strategy is used: first check the flag file (fast path),
then parse the bookbag only when the flag is present.

### Signal lifecycle

1. **Leaf** writes bookbag → writes `.ready` flag
2. **Teacher** polls → sees `.ready` flag → reads bookbag → reviews → clears flag
3. If teacher crashes mid-review, the `.ready` flag remains — another poll
   picks it up. No data loss.

---

## 3. File-Lock Rules

The file-lock protocol prevents write contention when CTO and COO update
the same bookbag file concurrently.

### Why needed

`update_bookbag()` reads a JSON file, modifies it in memory, and writes it
back. If CTO and COO both update the same bookbag file within the same
~100ms window, the second writer would overwrite the first's changes
(lost-update problem).

### Lock acquisition

```python
def acquire_lock(bead: str, timeout: float = 10.0) -> bool:
    """Acquire a file lock for a bead.

    Uses filesystem atomicity: O_EXCL on open() fails if the lock file
    already exists. Polls every 0.5s until acquired or timeout.

    Writes PID to the lock file for stale-lock detection.
    """
```

Lock location: `~/.hermes/locks/<bead>.lock`

**Algorithm:**

1. `os.open(lock_path, O_CREAT | O_EXCL | O_WRONLY)` — atomically create
   lock file (fails with `FileExistsError` if already held)
2. Write PID to lock file (`os.write(fd, f"{os.getpid()}\n")`)
3. If `FileExistsError`: read the lock file, check if the PID is still alive
   (`os.kill(pid, 0)`). If `ESRCH` — process is gone, steal the lock.
   Otherwise, wait 0.5s and retry.
4. Return `True` on success, `False` on timeout.

### Lock release

```python
def release_lock(bead: str) -> None:
    """Release a file lock for a bead. Best-effort."""
    lock_path = LOCK_DIR / f"{bead}.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass
```

### Atomic update with lock

```python
def locked_update_bookbag(bead: str, lock_timeout: float = 10.0, **kwargs) -> dict | None:
    """Update a bookbag with lock protection.

    1. acquire_lock(bead, lock_timeout)  — blocks until lock or timeout
    2. read_bookbag(bead)                — read current state
    3. bag.update(kwargs)                — apply changes in memory
    4. write_bookbag(bead, **bag)        — write back
    5. release_lock(bead)                — always in finally block

    Returns updated dict, or None if lock could not be acquired
    or bookbag doesn't exist.
    """
```

### Locking guarantees

| Scenario | Outcome |
|---|---|
| CTO and COO update same bead within 100ms | One acquires lock, writes, releases. Other retries after 0.5s. Both writes preserved. |
| Teacher crashes while holding lock | Stale-lock detection: next acquirer reads PID → `ESRCH` → steals lock |
| Lock file left on disk (no crash) | PID check passes → lock is considered alive. After `lock_timeout` (10s), `acquire_lock()` returns `False`, caller skips this bead. |
| Both teachers try to lock same bead | One wins, the other skips. Next poll cycle (5s later) the other teacher picks up the bead. |

---

## 4. Teacher Lifecycle Contract

Each teacher (CTO, COO) runs in a **persistent Orca child worktree**
(`teacher-cto` / `teacher-coo`) with its own terminal. The teacher enters
an infinite sleep/wake/review loop.

### Lifecycle diagram

```
                 ┌─────────┐
                 │  BOOT   │  Create or rediscover worktree
                 └────┬────┘
                      │
                 ┌────▼────┐
          ┌──────┤  SLEEP  │  Persist state, consolidate, log
          │      └────┬────┘
          │           │
          │      ┌────▼────┐
          │      │  WAKE   │  Restore state from last sleep
          │      └────┬────┘
          │           │
          │      ┌────▼────────┐
          │      │  REVIEW     │  Poll for un-reviewed bookbags
          │      │              │  that match this teacher's lens
          │      └────┬────────┘
          │           │
          └───────────┘
```

### TeacherWorktree API

```python
class TeacherWorktree:
    def boot(self) -> str:
        """Create or rediscover persistent teacher worktree.

        Tries Orca worktree creation first. If the worktree already
        exists (restart scenario), scans Orca's worktree list for a
        matching name and redisovers it.

        Returns: Absolute path to the teacher's worktree.
        """

    def sleep(self, duration_minutes: float = 0.0) -> dict:
        """Execute sleep sequence:
        1. Freeze: stop accepting new review tasks
        2. Consolidate: compress episodic history → YAML summary
        3. Persist: save SleepState to data/sessions/<session_id>.json
        4. Log: record sleep event to Library Log
        """

    def wake(self) -> dict:
        """Execute wake sequence:
        1. Load: read persisted SleepState from disk
        2. Hydrate: load ConsolidationArtifact for context
        3. Resume: log wake event to Library Log

        Graceful on first boot: if no session exists, returns empty
        state instead of crashing.
        """

    def review_cycle(self) -> int:
        """Poll for un-reviewed bookbags and review one if found.

        1. list_bookbags() — all beads on disk
        2. For each bead: read bookbag, check if this teacher's
           verdict field is empty
        3. If found: build task dict, run AdversarialReviewer
        4. locked_update_bookbag(bead, cto_verdict/cod_verdict=...)
        5. Record in episodic history

        Returns: 1 if reviewed, 0 if none found.
        """

    def run_loop(self) -> None:
        """Infinite sleep/wake/review cycle.

        Designed to run inside the teacher's worktree terminal:
            python -c "from teacher import TeacherWorktree; 
                        TeacherWorktree('cto').run_loop()"

        Every 10 cycles, prunes old session data.
        """

    def prune_sessions(self, max_cycles: int = 10) -> int:
        """Remove old session files beyond retention limit.

        Keeps only the most recent `max_cycles` session JSON and
        consolidation YAML files. Prevents unbounded data growth.
        """
```

### Lens assignment

| Teacher | Lenses | What they check |
|---|---|---|
| **CTO** | `CORRECTNESS`, `SECURITY` | Technical correctness, security vulnerabilities, edge cases |
| **COO** | `COMPLETENESS` | Acceptance criteria satisfaction, output completeness, task coverage |

### Contract guarantees

| Guarantee | Description |
|---|---|
| **Idempotent boot** | Creating or rediscovering the worktree produces the same state. Safe to call multiple times. |
| **Sleep preserves state** | All episodic history, scores, and queued tasks are persisted to disk before sleep. |
| **Wake restores context** | Teacher resumes review with full episodic context from consolidation artifact. |
| **Each bookbag reviewed once per teacher** | `review_cycle()` skips bookbags where `cto_verdict` (for CTO) or `coo_verdict` (for COO) is already non-empty. |
| **Lock-protected writes** | `locked_update_bookbag()` prevents CTO/COO write contention. |
| **Bounded data growth** | `prune_sessions()` keeps only the most recent 10 session files. |

---

## 5. Leaf Lifecycle Contract

Each leaf is a **disposable Orca child worktree** (`study-{role}-{rand8}`)
created for one task and destroyed when handoff is complete.

### Lifecycle diagram

```
    ┌──────────┐
    │  BOOT    │  Create Orca child worktree
    └────┬─────┘
         │
    ┌────▼──────┐
    │  BRIEF    │  Write StudentBrief to .hermes/briefs/<bead>.json
    └────┬──────┘
         │
    ┌────▼────────┐
    │  RUN_TASK   │  Execute director.run_task() (LLM + bookbag + review)
    └────┬────────┘
         │
    ┌────▼────────┐
    │  OUTPUT     │  Write audit trail to .hermes/outputs/<bead>.json
    └────┬────────┘
         │
    ┌────▼────────┐
    │  SIGNAL     │  Write .hermes/signals/<bead>.ready flag
    └────┬────────┘
         │
    ┌────▼────────────┐
    │  WAIT_HANDOFF * │  Phase 2 only: poll for teacher verdicts
    └────┬────────────┘
         │
    ┌────▼─────────┐
    │  DISPOSE     │  Remove Orca worktree, idempotent
    └──────────────┘
```

\* In Phase 1 (synchronous), CTO/COO review happens inside `run_task()`
in the principal process, so `wait_for_handoff()` is informational.
Verdicts are already filled when `signal_ready()` is called.

### StudentLeaf API

```python
class StudentLeaf:
    def boot(self) -> str:
        """Create the disposable student worktree.

        Creates a proper Orca child worktree named `study-{role}-{rand8}`.
        Wraps OrcaUnavailableError in LeafError.
        """

    def write_brief(self, task_prompt: str) -> Path:
        """Write a StudentBrief to .hermes/briefs/<bead>.json.

        The brief is the task contract: bead, role, domain, task, difficulty.
        """

    def run_task(self, task_prompt: str) -> dict:
        """Execute the task via director.run_task().

        Phase 1: synchronous — LLM call + bookbag write + CTO/COO review
        all happen in the principal process. The result dict contains
        response, review, scores.

        Phase 2: async — only the LLM call. Bookbag written with empty
        verdicts. Teachers fill verdicts asynchronously.
        """

    def write_output(self, data: dict) -> Path:
        """Write audit trail to .hermes/outputs/<bead>.json.

        Full record: response, review, findings, scores, all structured.
        """

    def signal_ready(self) -> None:
        """Signal teachers bookbag is ready.

        Creates BookbagSignal(bead).ready() — writes
        ~/.hermes/signals/<bead>.ready flag file.
        """

    def wait_for_handoff(self, timeout: float | None = None) -> tuple[str, str]:
        """Wait for CTO and COO verdicts.

        Delegates to wait_for_verdicts(). In Phase 1 this is a no-op
        (verdicts already filled). In Phase 2 this polls until both
        verdicts appear.

        Returns: (cto_verdict, coo_verdict)
        """

    def dispose(self) -> None:
        """Remove the worktree. Idempotent.

        Calls close_worktree() on OrcaExecutionManager, clears all
        references. Safe to call multiple times.
        """
```

### File layout inside a leaf worktree

```
study-coder-a1b2c3d4/
  └── .hermes/
      ├── briefs/
      │   └── coder-python-coding-a1b2c3d4.json   # StudentBrief
      └── outputs/
          └── coder-python-coding-a1b2c3d4.json    # Audit trail
```

Shared (outside the worktree):
```
~/.hermes/
  ├── bookbag/
  │   └── coder-python-coding-a1b2c3d4.json        # Ground-truth bookbag
  ├── signals/
  │   └── coder-python-coding-a1b2c3d4.ready       # Ready flag
  └── locks/
      └── coder-python-coding-a1b2c3d4.lock         # Write lock
```

> **Paths are configurable.** Set `BOOKBAG_DIR`, `SIGNAL_DIR`, or `LOCK_DIR`
> environment variables to override the default `~/.hermes/` paths. This is
> useful when running multiple school-core instances or testing custom paths.

### Convenience wrapper

`run_leaf()` wraps the full lifecycle:

```python
def run_leaf(task_prompt, role, domain, difficulty, store) -> dict:
    leaf = StudentLeaf(role, domain, difficulty, store)
    try:
        leaf.boot()
        leaf.write_brief(task_prompt)
        result = leaf.run_task(task_prompt)
        if result.get("status") == "success":
            leaf.write_output(output_data)
            leaf.signal_ready()
            # Phase 2: cto, coo = leaf.wait_for_handoff()
        return result
    finally:
        leaf.dispose()  # always runs
```

### Contract guarantees

| Guarantee | Description |
|---|---|
| **Bead uniqueness** | Random 8-hex-char suffix ensures unique beads across runs. Pattern: `{role}-{domain}-{rand8}`. |
| **No orphan worktrees** | `dispose()` is always called, either via explicit code or `finally` block in `run_leaf()`. |
| **Idempotent dispose** | Calling `dispose()` multiple times is safe — second call is a no-op. |
| **Boot before use** | `write_brief()`, `run_task()`, `write_output()` all raise `LeafNotBootedError` if `boot()` hasn't been called. |
| **Signal after write** | `signal_ready()` is always called **after** the bookbag is fully written. |
| **Phase 1: verdicts inline** | In synchronous mode, verdicts are filled before `signal_ready()`. The teacher sees a completed bookbag immediately. |

---

## 6. Timeout Configuration Reference

All timeouts are configurable via environment variables, with sensible
defaults for personal dev use.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `HANDOFF_TIMEOUT` | `120` (s) | `wait_for_bookbag()`, `wait_for_verdicts()` | Max time to wait for both teacher verdicts |
| `HANDOFF_POLL_INTERVAL` | `5.0` (s) | `wait_for_bookbag()`, `wait_for_verdicts()` | Seconds between poll attempts |
| `DEFAULT_POLL_INTERVAL` | `5.0` (s) | `TeacherWorktree.poll_interval` | Teacher's bookbag poll frequency |
| `DEFAULT_REVIEW_TIMEOUT` | `90` (s) | `TeacherWorktree._call_review_model()` | Max time for the LLM review call |
| `acquire_lock timeout` | `10.0` (s) | `locked_update_bookbag()` | Max time to wait for file lock |
| `lock_timeout` | `10.0` (s) | `acquire_lock()` | Max time for O_EXCL lock acquisition |
| `HANDOFF_TIMEOUT` (leaf) | `120` (s) | `StudentLeaf.wait_for_handoff()` | Max time to wait for async verdicts |
| `MAX_SESSION_CYCLES` | `10` | `TeacherWorktree.prune_sessions()` | Keep N most recent session files |

### Setting custom timeouts

```bash
# Quick handoff (fast LLMs):
HANDOFF_TIMEOUT=60 HANDOFF_POLL_INTERVAL=2 python conductor.py --loop --rounds 10

# Slow handoff (complex reviews, slow models):
HANDOFF_TIMEOUT=300 HANDOFF_POLL_INTERVAL=10 python conductor.py --loop --rounds 5

# Teacher with custom poll rate:
python teacher.py --role cto --poll-interval 2.0

# Cross-project: set in .env or shell profile:
export HANDOFF_TIMEOUT=120
export HANDOFF_POLL_INTERVAL=5
```

---

## 7. Error Recovery

### Teacher crash recovery

| Failure | Detection | Recovery |
|---|---|---|
| Teacher worktree deleted | Principal calls `boot()` (rediscovery) → `worktree list` returns no match | Principal recreates via `create_worktree()` |
| Teacher process crashed mid-review | Teacher terminal session ends; bookbag has partial verdict | Next poll cycle: another teacher instance picks up the bookbag |
| Teacher session data corrupted | `execute_wake()` raises `SessionCorruptedError` | Teacher falls back to fresh state (no prior context) |
| Lock held by dead process | `acquire_lock()` reads PID → `os.kill(pid, 0)` → `ESRCH` → steals lock | Transparent — next acquirer recovers automatically |

### Leaf crash recovery

| Failure | Detection | Recovery |
|---|---|---|
| Leaf worktree creation fails | `OrcaUnavailableError` → `LeafError` | `run_leaf()` raises exception, caller handles |
| Leaf task fails (LLM error) | `result["status"] != "success"` | Leaf skips `write_output()` and `signal_ready()`, disposes without handoff |
| Principal crashes mid-handoff | On restart: `--resume` flag scans for in-progress bookbags | Principal decides per-bead: continue waiting or clean up |
| Leaf dispose fails (Orca error) | `close_worktree()` exception caught in `finally` block | Logged as warning; worktree may be orphaned. Clean up via `conductor.py --clean-worktrees` |

### Principal crash recovery (Phase 2)

The `--resume` flag (planned, not yet implemented) will:

1. Scan `~/.hermes/bookbag/` for bookbags with partial verdicts
2. Rediscover existing teacher worktrees (or recreate if missing)
3. For each in-progress bookbag: check if both verdicts are filled
4. If complete: run `evaluate_and_update()` and dispose the leaf
5. If incomplete: optionally wait for remaining verdict or abort

### Stale lock recovery

Lock files (`~/.hermes/locks/<bead>.lock`) contain the PID of the process
that created them. If the process crashes, the next `acquire_lock()` call:

1. Reads the PID from the lock file
2. Calls `os.kill(pid, 0)` — returns `ESRCH` if the process is gone
3. Unlinks the stale lock and steals it

This is safe because:
- `os.kill(pid, 0)` is atomic (no race with PID reuse on modern OS)
- The stale lock's write-critical section is bounded (≤ 100ms read-modify-write)
- No lock lasts longer than `lock_timeout` (10s) during normal use

---

## Appendix: End-to-End Flow

```
Principal                          Leaf                    Teacher CTO          Teacher COO
─────────                          ────                    ───────────          ───────────
                                                                                      
  │                                │                       │                    │     
  ├── create worktree (teacher-cto)──┐                    │                    │     
  │                                │                       │                    │     
  ├── create worktree (teacher-coo)─────────────────────────┼────────────────────┤     
  │                                │                       │                    │     
  ├── start teacher loop (cto) ──────────────────────────────►  run_loop()      │     
  │                                │                       │  sleep()           │     
  ├── start teacher loop (coo) ────────────────────────────────┼────────────────►     
  │                                │                       │                    │  sleep()
  │                                │                       │                    │     
  ├── run_leaf(task) ──────────────► boot()                │                    │     
  │                                ├── write_brief()       │                    │     
  │                                ├── run_task()          │                    │     
  │                                │   ├── LLM call        │                    │     
  │                                │   ├── write_bookbag   │                    │     
  │                                │   ├── CTO review      ► wake() + review()  │     
  │                                │   ├── COO review      ─────────────────────► wake() + review()
  │                                │   └── score           │                    │     
  │◄───────────────────────────────┤   return result       │                    │     
  │  evaluate_and_update(result)   │                       │                    │     
  │                                ├── write_output()      │                    │     
  │                                ├── signal_ready()      │                    │     
  │                                ├── dispose()           │                    │     
  │                                │                       │                    │     
  │◄── result with scores ─────────┤                       │                    │     
```

---

*Last updated: 2026-07-22*
*Corresponding code: `bookbag.py`, `teacher.py`, `leaf.py`, `conductor.py`, `sleep_state.py`*
