# Dispatch Bug Investigation: 75 "Extract sweep constants" Sessions Failed

**Purpose:** Track the investigation into why 75 FirstMate "Extract sweep constants" sessions failed, and what the fix should be.

**Status:** Root cause identified — path/worktree bug. Fix NOT yet implemented.

---

## 1. What Happened

75 FirstMate sessions, all titled "Extract sweep constants - sweep_rounds and sweep_vote_margin", were launched between 2026-08-20 and 2026-08-21. All 75 failed. Each one:

1. Launched a FirstMate crewmate in `/Users/brandonbennett` (NOT in a git worktree)
2. Ran `git worktree list` → found only the main checkout
3. Tried to locate `sound-royale-ny` → could not find it
4. Stopped after 3 tool calls and 5 messages with no work done

The crewmates were executing a real task from a real plan (`2026-07-19_churn-game-rule-constants.md`), but could not find the project to work on.

---

## 2. Root Cause

The crewmates were launched in the **home directory** (`/Users/brandonbennett`), not in a project worktree. The dispatch mechanism did not:

- Create a git worktree for the crew to work in
- Set the working directory to a location where `sound-royale-ny` is accessible
- Pass the project path to the crew's context

This is a **dispatch configuration bug**, not a project-finding bug. The project exists at `/Users/brandonbennett/sound-royale-ny` (and its Orca worktree copy at `/Users/brandonbennett/orca/sound-royale-ny/`), but the crewmates were dropped into a directory where the project is not the working tree and `git worktree list` shows no worktrees.

---

## 3. How Dispatch Works (Current Understanding)

### 3.1 Dispatch Entry Points

| Script/file | Role |
|---|---|
| `dispatch_plan_b.py` | Python dispatch script — calls `crew_dispatch.dispatch_crew()` with explicit `project_dir` argument |
| `crew_dispatch.py` | Core dispatch module — `dispatch_crew()` creates Orca worktree, launches Hermes crewmate via `hermes-fm-wrapper` |
| `hermes-fm-wrapper` (shell) | Bash wrapper that loads SOUL.md persona, runs `hermes -p "$PROFILE" --cli --query "$INPUT" ...`, captures exit code, writes FM_STATUS_FILE |
| `scripts/fm_doctor.sh` | Diagnostic script — checks FirstMate clone, Orca daemon, FM_HOME config, Hermes wrapper, model auth |
| `FM_HOME` = `~/.hermes/school-core-fm-config` | Persistent config + state for FirstMate/Orca integration |

### 3.2 The `dispatch_crew()` Function

`crew_dispatch.py:dispatch_crew()` takes a `project_dir` parameter and:

1. Creates a **disposable Orca git worktree** of the project
2. Launches a Hermes crewmate **inside that worktree** via `hermes-fm-wrapper`
3. The crewmate's prompt includes: "You are in a disposable Orca git worktree of the project. Verify `pwd` and `git rev-parse --show-toplevel` both resolve to this disposable worktree, not a primary checkout."
4. After the crew finishes, tears down the worktree (but the report.md survives)

### 3.3 The 75 Failed Sessions Did NOT Use This Path

The 75 "Extract sweep constants" sessions were **not** dispatched via `dispatch_plan_b.py` or `crew_dispatch.dispatch_crew()`. They appear to have been launched as standalone FirstMate sessions — likely from the Hermes GUI or a cron job — that:

- Did NOT use `dispatch_crew()`
- Did NOT create an Orca worktree
- Did NOT set `project_dir`
- Were launched with the default working directory (`/Users/brandonbennett`)
- Had no plan context pointing them at `sound-royale-ny/.hermes/plans/`

The crewmates expected to find the project but had no mechanism to locate it.

---

## 4. Why the Plan Reference Did Not Help

The plan (`2026-07-19_churn-game-rule-constants.md`) lives at:

```
sound-royale-ny/.hermes/plans/2026-07-19_churn-game-rule-constants.md
```

The crewmate was launched in `/Users/brandonbennett` and likely looked for the plan in:

```
~/.hermes/plans/2026-07-19_churn-game-rule-constants.md  # WRONG — outside the project
```

The project's `.hermes/` directory is **inside the project checkout**, not in the user's home `~/.hermes/`. A crewmate looking in `~/.hermes/plans/` would not find it.

The session exports confirm this: the crewmate's context included a reference to `.hermes/plans/2026-07-19_churn-game-rule-constants.md` (a relative path), but without being inside the project tree, the relative path resolves to the wrong location.

---

## 5. The Sweep Constants — Where They Actually Are

The "Extract sweep constants" task is real and the plan is sound. The constants to extract are:

| Constant | Value | Current location (raw literal) | Intended location |
|---|---|---|---|
| `SWEEP_ROUNDS` | `3` | `models.py:225` (`total_rounds == 3` in `check_sweeper_eligibility()`), `views.py:321` (`room.total_rounds != 3` in `has_ranked_three_round_sweep()`), `views.py:325` (`round_number__lte=3`), `views.py:329` (`len(resolved_rounds) == 3`), `game.ts:86` (`totalRounds?: number` — inferred), `useGame.ts` (if boards generated with literal 3) | `models.py` (named constant) + front-end `game.ts` |
| `SWEEP_VOTE_MARGIN` | `1` | `views.py:1268` (`if vote_margin == 1:` in `resolve_round()`) | `views.py` (named constant) |

**NOTE:** The plan file says `views.py:329` for the `== 3` reference and `:1268` for `vote_margin == 1`. The actual line numbers in the current codebase are:
- `== 3` in `has_ranked_three_round_sweep()`: `views.py:321` (and `325`, `329`)
- `vote_margin == 1`: `views.py:1268`

This line-number drift is expected — the plan was written at a specific point in time and the file has grown since.

**The actual sweeper logic (verified):**
- `has_ranked_three_round_sweep()` (views.py:320-331): checks `room.total_rounds != 3` AND `current_round.round_number != 3` — the room must have exactly 3 rounds AND we must be on round 3
- `resolve_round()` (views.py:1259-1290): computes `vote_margin = max_votes - (spectator_count - max_votes)`, then checks `if vote_margin == 1:` for the sweep multiplier
- `check_sweeper_eligibility()` (models.py:225): `if self.room.total_rounds == 3:` — called on the Room model

The value `3` appears in multiple places because the sweeper mechanic requires: (a) a 3-round room, (b) being on round 3, (c) all 3 rounds resolved. The value `1` appears once as the vote margin threshold.

---

## 6. What the Fix Should Be

The 75 sessions failed because of a **dispatch path bug**, not because the task is invalid. Two possible fixes:

### Option A: Use proper dispatch for all crew tasks

Ensure every FirstMate task goes through `crew_dispatch.dispatch_crew()` with an explicit `project_dir`, so each crewmate gets a proper Orca worktree with the project checkout. This is the "correct" fix — it aligns with how `dispatch_plan_b.py` works.

### Option B: Fix the plan context to point to the right path

If standalone FirstMate sessions are the intended mechanism (not Orca worktrees), the plan file needs to be accessible from the launch directory, OR the crewmate needs the full absolute path in its context.

### Option C: Both

Use proper dispatch AND ensure plan references are robust (absolute paths or project-relative with clear guidance).

---

## 7. BEND Relevance — Final Verdict

**BEND is NOT relevant to the 75 failed sessions.** The sessions were not a computational loop — they were a dispatch bug. BEND's value (formal proofs, parallel GPU compute) does not apply to:

- Fixing a path/worktree bug in FirstMate/Orca dispatch (Python/Shell orchestration)
- Extracting named constants from game engine code (behavior-preserving refactor)
- Running vitest unit tests for VotingPanel and RoundStage (JavaScript test runner)

**BEND IS relevant to the game engine's ELO/sweeper math IF it involved heavy simulation**, but the game engine uses simple arithmetic ( Elo gain = base_gain × multiplier, sweeper_penalty = 20, vote_margin = max_votes - (spectators - max_votes) ). There is no simulation, no optimization loop, no hyperparameter search. The constants `3` and `1` are game design choices, not computed optima.

**The one thing worth considering for BEND:** if the game ever adds a tuning layer that simulates thousands of matches to optimize `SWEEP_ROUNDS`, `SWEEP_VOTE_MARGIN`, ELO multipliers, etc. — that simulation loop would be a legitimate BEND candidate. But no such layer exists today.

---

## 8. What Was Found / Not Found

### 7.1 Found

- [x] Plan file at `sound-royale-ny/.hermes/plans/2026-07-19_churn-game-rule-constants.md` (93 lines, 5728 bytes)
- [x] 6 tasks in the plan: 4 constant extractions + 1 round-count bounds + 1 vitest tests
- [x] `SWEEP_ROUNDS = 3` equivalent literals in `models.py:225`, `views.py:320-331`, `views.py:1268`
- [x] `SWEEP_VOTE_MARGIN = 1` equivalent literal in `views.py:1268`
- [x] `MIN_ROUNDS = 1`, `MAX_ROUNDS = 10` already exist in `game.ts:105-106`
- [x] `dispatch_crew()` in `crew_dispatch.py` creates Orca worktrees properly
- [x] `hermes-fm-wrapper` is the launch wrapper that loads persona + runs Hermes CLI
- [x] `fm_doctor.sh` is the diagnostic script for the FM→Orca→Hermes chain
- [x] `FM_HOME` = `~/.hermes/school-core-fm-config` is the persistent config dir
- [x] `dispatch_plan_b.py` uses `crew_dispatch.dispatch_crew()` with explicit `project_dir`

### 7.2 Not Found

- [ ] The actual dispatch mechanism that launched the 75 sessions (not `dispatch_plan_b.py` / `crew_dispatch.py`)
- [ ] Any cron job or automation that triggered the 75-session batch
- [ ] A "sweep tuning" computation layer — the constants are game design values, not computed
- [ ] `SWEEP_ROUNDS` or `SWEEP_VOTE_MARGIN` as named constants anywhere in the codebase (they don't exist yet — that's what the task was supposed to create)

---

## 9. Next Steps

1. **Find how the 75 sessions were launched** — check cron jobs, Hermes GUI dispatch, any automation that batch-launched FirstMate tasks. The session exports show they existed; the launch mechanism is the missing piece.
2. **Decide on fix approach** — Option A (proper dispatch), Option B (fix plan paths), or Option C (both).
3. **If the task is worth re-running** — extract `SWEEP_ROUNDS = 3` and `SWEEP_VOTE_MARGIN = 1` as named constants per the plan.

---

*End of tracking file.*
