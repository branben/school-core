# Session walkthrough: parallel-students investigation (2026-08-16)

Archive of the investigation + fixes landed this session. Read as the
intellectual product, not as instructions.

## Goal
Run multiple student AIs in parallel, with inspectable work — without
breaking the verify contract (Orca worktree isolation, git isolation,
teardown, grading, two-judge review).

## What we actually did

- Audited the admission/fleet/admission-layer code (crew_admission.py,
  resilience.py, crew_runs registry, ship loop).
- Confirmed the "over-admission on empty registry" bug is ALREADY fixed
  (fail-closed: corrupt crew_runs.json → sys.maxsize → deny).
- Discovered the REAL parallel blocker was the dropped-worktree-id WAITING
  path: dispatch() resolved a fleet worktree id, leased it, then called
  dispatch_crew WITHOUT passing that id, and dispatch_crew itself already
  creates its own Orca worktree via fm-spawn.sh (the fleet id is a logical
  lease key, not the real Orca id — by design). The net effect: 4 admitted
  crews all got assigned the SAME logical slot (wt-1) because the lease is
  released right after spawn, leaving no durable view of occupied slots.
- Fixed it (commit a01f1ba): dispatch_crew now accepts + records
  fleet_worktree_id into the durable crew_runs.json running record, and
  FleetRegistry.assign_worktree now (a) consults that durable record so a
  slot stays occupied for the crew's async lifecycle, and (b) actually
  returns the FIRST FREE worktree instead of always worktrees[0]. Tested
  with a new test_parallel_fleet test (4-worktree fleet + cap=4 + in-flight
  running records → 4 distinct slots). Full suite: 1313 passed, 16 skipped.
- Found the per-student turn budget was ALSO broken (secretschool-core-hll):
  orca_executor._TURNS was {easy:1, medium:16, hard:16, diploma:20} with
  HERMES_TIMEOUT_PER_TURN_MS=120000, so a medium task booked 16*120=1920s —
  exceeding the 900s crew default and the 1800s CI job cap, guaranteeing
  timeout. The docstring (orcadorch line ~1002) said medium=3, hard=5,
  diploma=8 — the code contradicted itself.
- Fixed it (commit 4092a69): HERMES_TIMEOUT_PER_TURN_MS 120000→90000 and
  _TURNS aligned to {easy:1, medium:3, hard:5, diploma:8}. Added a new test
  (test_orca_timeout_budget) asserting per_turn=90s, _TURNS matches docstring,
  and every difficulty's total fits under BOTH the crew cap (900s) and job cap
  (1800s). Full suite: 1332 passed, 16 skipped.
- Fixed the board-bot divergence loop (commit b7bd934): redirected all three
  bot git touchpoints to board-publish instead of main, so main no longer
  accumulates auto-commits that force rebasing.
- Created the 4-worktree fleet config (commit fe7dfab): data/fleet.json
  with 4 worktrees + capacity 4 (force-added past gitignore; it's runtime
  state, not source).
- Ran a real Bot Mode parallel demo (school_bot_execute against 3 bots):
  student-searcher returned "pong" in 22.5s with full Reasoning — proving
  the integration works end-to-end. BUT every call still died at the 60s
  hard transport ceiling (even a trivial task takes ~31s cold-start),
  so parallel Bot Mode CANNOT complete right now.

## What is MERGED to origin/main
- a01f1ba fix(dispatch): keep fleet worktree slot occupied for crew's async
  lifecycle (N6.2).
- fe7dfab chore(fleet): provision 4-worktree local fleet for parallel students.
- b7bd934 fix(ci): redirect board-bot commits to board-publish, off main.
- 4092a69 fix(orchestrator): align per-student turn budget with job/crew
  ceilings.

## What is NOT MERGED / NOT done
- Runner env vars: CREW_MAX_PER_CYCLE=4, CREW_RUNNER_SLOTS=4,
  CREW_CYCLE_BUDGET_SECONDS>=3630 must be set on the Mac runner (outside the
  repo). Without them, 4 crews get DENIED (the budget-aware admission proved
  this — at 1800s all 4 are denied).
- data/fleet.json is gitignored; it won't ride via git. It must be deployed
  to the runner checkout.
- Unverified infra: (a) fm-spawn/Orca must actually CREATE 4 concurrent
  worktrees; (b) the OmniRoute gateway (localhost:20128) must survive 4
  concurrent LLM streams. Neither measured.
- hermes-fm-wrapper FM_AGENT_MAX_TURNS default is still 16 (not a code change
  to school-core, but the wrapper's default). The Orca path now uses 90s/turn
  and aligned _TURNS; the wrapper default is separate.
- Bot Mode 60s transport ceiling (not a school-core code change; it's the
  agent-school MCP server wrapper config). Bot Mode also runs headless with
  web UI disabled — sessions are not inspectable.

## Beads (tracked)
- school-core-btm (open): Provision Mac runner for 4-parallel students.
- school-core-hll (open, BLOCKS btm + fc7.3.6): per-student turn budget.
- school-core-ex6 (open): Bot Mode 60s hard transport ceiling.
- school-core-x0b (open): Bot Mode: headless, sessions not inspectable.
- school-core-fc7.3.6 (open, PARENT: school-core-fc7.3, BLOCKED by
  school-core-hll): Run controlled Hermes 8-turn completion pilot.

## Dependency chain (the right sequence to actually fly)
1. Fix the turn budget (done — 4092a69). NEXT: also fix the hermes-fm-wrapper
   FM_AGENT_MAX_TURNS default OR set it explicitly in the pilot workflow.
2. Run the controlled 8-turn pilot (fc7.3.6) — now actually achievable because
   orca_executor uses 90s/turn and _TURNS=8 for diploma (720s < 900s crew cap).
3. Provision the runner env vars + deploy fleet.json (school-core-btm).
4. Verify fm-spawn/Orca can create 4 concurrent worktrees; verify gateway.
5. Then raise concurrency from 1.

## Key numbers (regenerated from code by regenerate_budgets.py, run 2026-08-16)
See regenerate_budgets.py for the live computation. Runtime output (2026-08-16):

  HERMES_TIMEOUT_PER_TURN_MS = 90000   (90 s/turn)
  _TURNS = {'easy': 1, 'medium': 3, 'hard': 5, 'diploma': 8}
  CREW_CAP_S = 900, JOB_CAP_S = 1800

  difficulty   turns   per-turn(s)   total(s)   total(min)   under_crew   under_job   ok?
  easy            1        90            90          1.50       True        True     1
  medium          3        90           270          4.50       True        True     1
  hard            5        90           450          7.50       True        True     1
  diploma         8        90           720         12.00       True        True     1

  ALL difficulties fit under BOTH crew cap AND job cap.

  NOTE: with the OLD buggy values (120s/turn, _TURNS=16), diploma would have
  been 16*120 = 1920s, and medium 16*120 = 1920s — both exceeding the 900s
  crew cap and the 1800s job cap. The old code structurally guaranteed timeout.

## Outstanding (not yet done)
- hermes-fm-wrapper default MAX_TURNS is still 16 (scripts/hermes-fm-wrapper:12:
  MAX_TURNS="${FM_AGENT_MAX_TURNS:-16}"). To actually run the controlled 8-turn
  pilot (fc7.3.6/fc7.6), EITHER set FM_AGENT_MAX_TURNS=8 explicitly in the
  pilot workflow/env, OR change the wrapper default from 16 to 8 (user-space
  script change, not a school-core code change). This is the ONLY remaining
  lane-specific gap after the Orca budget fix — the Orca path now uses 90s/turn
  and _TURNS=8; the wrapper must be addressed so the pilot's OWN turn cap is
  8, not 16.

## Dependencies (the right sequence)
1. Fix the turn budget (done — 4092a69). NEXT: address the wrapper default
   (set FM_AGENT_MAX_TURNS=8 in the pilot, OR patch scripts/hermes-fm-wrapper).
2. Run the controlled 8-turn pilot (fc7.3.6) — now actually achievable because
   orca_executor uses 90s/turn and _TURNS=8 for diploma (720s < 900s crew cap).
3. Provision the runner env vars + deploy fleet.json (school-core-btm).
4. Verify fm-spawn/Orca can create 4 concurrent worktrees; verify gateway.
5. Then raise concurrency from 1.
- The "dropped worktree_id" theory was WRONG. dispatch_crew makes its own
  Orca worktree; the fleet id is a logical lease key by design. The real bug
  was that the lease is released synchronously after spawn, leaving no durable
  slot occupancy for the crew's async lifecycle.
- Always make the test ROBUST and FAITHFUL before concluding a bug exists:
  the faithful test (mocking dispatch_crew to write a running record with
  fleet_worktree_id) DID reproduce the collision, which pointed at the real
  cause.
- Full suite runs (1332 tests) are worth it even when slow — they catch the
  regressions that the fast RED/GREEN would miss. The 6-minute suite is still
  cheaper than rediscovering a broken admission/grade path.
