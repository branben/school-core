OPEN ITEMS — Parallel-students investigation (2026-08-16)
=========================================================

NOTE ON REDACTIONS
------------------
Any credential/API key/token values here are replaced with [REDACTED].
The OMNIROUTE_API_KEY starts `sk-1…` → [REDACTED]; endpoint is
http://localhost:20128/v1; it lives in school-core/.env which is now
gitignored (commit b7bd934 region / .gitignore committed). Never commit
.env — only .gitignore and non-secret config.


CURRENT LEDGER (committed to origin/main, pushed)
-------------------------------------------------
• 4092a69 fix(orchestrator): align per-student turn budget with job/crew ceilings
      orca_executor.py:
        HERMES_TIMEOUT_PER_TURN_MS  120000 -> 90000 (90 s/turn)
        _TURNS {easy:1,medium:16,hard:16,diploma:20} -> {easy:1,medium:3,hard:5,diploma:8}
      new: tests/test_orca_timeout_budget.py (RED->GREEN; proves every difficulty
      fits crew cap 900s AND job cap 1800s; full suite 1332 passed, 16 skipped, 0 failures)
      closes school-core-hll (turn budget blocker)

• b7bd934 fix(ci): redirect board-bot commits to board-publish, off main
      .github/workflows/school-loop.yml: 3 commit targets now board-publish instead of main
      NOTE: bot committed 79d0180 chore: publish board [skip ci] to main ONCE
      BEFORE the redirect took effect. After b7bd934: no further bot commits to
      main on the observed window. Confirm long-term by watching for any new
      chore: publish board [skip ci] appearing on main.

• fe7dfab chore(fleet): provision 4-worktree local fleet for parallel students
      data/fleet.json:
        {daemons:{local:{endpoint:local, worktrees:[wt-1,wt-2,wt-3,wt-4], capacity:4}}}
      gitignored runtime state; force-added via git add -f; deploy to runner separately.

• a01f1ba fix(dispatch): keep fleet worktree slot occupied for the crew's full async
      lifecycle (school-core N6.2). Previously the office's lease was released right
      after the synchronous spawn, so assign_worktree could hand the same logical
      slot (wt-1) to multiple crews. Now dispatch_crew records the fleet
      worktree_id into the durable crew_runs.json running record; assign_worktree
      skips any slot with an in-flight crew on it; and the free-slot picker no
      longer defaults to worktrees[0]. Verified: 4 dispatches with 4 in-flight
      running records -> 4 distinct slots (wt-1..wt-4). New test:
      tests/test_parallel_fleet.py.


WHY YOU CAN'T CLICK ROBOT SESSIONS (two facts, not a bug)
---------------------------------------------------------
1. school_bot_execute launches each bot as a headless Hermes backend with
   "web UI disabled". The desktop log says:
   404: {"error":"Headless backend (hermes serve): web UI disabled — use
        `hermes dashboard` for the browser UI."}
   There is no desktop tab/pane/conversation to click into. Result comes back
   to the MCP caller, then the backend is reaped (idle > 600 s, SIGTERM).
2. No transcript file persists in the bot's profile dir. A content search of
   ~/.hermes for the bot name finds nothing durable; the bot's output is the
   live tool response, then the process dies.
   Attempted `conversation: "name"` param → exit 1 (that param is not a session
   opener in this integration).
   BOTTOM LINE: Bot Mode is fire-and-forget (result -> caller). It is NOT a
   UI-visible multi-agent session. Consistent with scale-architecture.md: Bot
   Mode is reserved for non-coding roles that "don't need a worktree." Coding
   students (the verify contract) are supposed to run in Orca worktrees via
   crew_dispatch, where artifacts (report.md, worktree diffs, bookbag) ARE
   inspectable. So you don't "click on the coding students" either — you inspect
   the artifacts they produce, not the chat.


PROOF THAT BOT MODE PARALLELISM IS BLOCKED BY A TRANSPORT CEILING, NOT BY LOGIC
-------------------------------------------------------------------------------
• Per-call `timeout: 90` was IGNORED. Every parallel call in the batch died with:
   "MCP call timed out after 60.0s (configured timeout: 60.0s)"
   The 60s is a hard transport cap in the tool/wrapper, not my per-call timeout.
• A trivial one-word `pong` task returned OK in 22.49 s with has_error=false,
   so the integration works end-to-end and each bot boots (~31 s observed for a
   one-word answer; a real task exceeds 60 s and dies).
• Therefore: NO non-trivial task can complete in parallel through this tool today.
   Bottleneck = 60s transport ceiling + ~31s cold start, NOT the OmniRoute
   gateway (we never got to measure gateway saturation because calls died at the
   transport layer first), and NOT the worktree wiring (that was the Orca path's
   problem; this is the Bot Mode path, which is a separate, isolated topology).
   BOT MODE IS RESERVED FOR NON-CODING ROLES per scale-architecture.md. The
   coding students go through crew_dispatch -> Orca worktrees; that's where the
   investigate/fix/wire happened (a01f1ba, fe7dfab, the turn-budget fix 4092a69).


WHERE EACH OF THE 5 FINDINGS STANDS
-----------------------------------
1. Turns (_TURNS map = 16, FM_AGENT_MAX_TURNS default 16)   -> FIXED (4092a69).
   Live budget table (regenerate_budgets.py): per_turn 90s, _TURNS matches
   docstring (easy 1 / medium 3 / hard 5 / diploma 8), every difficulty fits
   crew cap (900s) and job cap (1800s). That's 4092a69 + test_orca_timeout_budget.

2. Admission/fleet logic                                  -> AUDITED; part of it
   is already in code and the missing piece is provisioned+verified (a01f1ba +
   fe7dfab). admit logic reserves cap*timeout+reserve; effective_cap =
   min(configured_cap, runner_slots); deny when active_claims >= runner_slots.
   fail-closed on corruption/empty (sys.maxsize) YES; cross-daemon stale-registry
   guard NO (still an open watch-item); fleet now 4 (not 1) after fe7dfab, but
   only the LOCAL daemon slot count changed; CREW_RUNNER_SLOTS / CREW_MAX_PER_CYCLE
   env vars still default to 1 on the runner (must be set to 4).

3. Dispatch office + fleet registry                    -> FIXED (a01f1ba) +
   PROVISIONED (fe7dfab). 4 crews -> 4 distinct slots (verified). This is the
   "in-flight slot occupancy" fix: the durable running record carries the fleet
   worktree_id; assign_worktree consults it; free-slot picker no longer always
   picks worktrees[0].

4. Spawn seam (worktree_id)                            -> CORRECTED (was NOT a
   "dropped id" bug). dispatch_crew creates its OWN Orca worktree via fm-spawn.sh
   and tears it down; the fleet worktree_id is a LOGICAL LEASE KEY by design.
   The actual bug was the transient-lease-release problem (N6.2), now fixed.
   The fleet id is decoupled from the real Orca worktree by intent
   (crew_dispatch.py:7-11: registry deliberately omits Orca's path-bearing
   worktree ID).

5. Bot Mode                      -> PROBED, NOT FIXED, and correctly scoped.
   school_bot_execute is live + isolated, but (a) headless/no-click (fact), and
   (b) 60s transport ceiling kills non-trivial parallel tasks (fact). It is a
   separate, isolated topology from the Orca/fleet path, so it does NOT bypass
   the Orca worktree wiring bug (there was no such bug — that was the N6.2
   transient-lease issue, now fixed). Bot Mode can't be "the parallel students"
   path for coding work because it has no worktree/verify contract. For non-coding
   roles (research/triage/docs) it is a live parallel-safe path today — limited
   by the 60s ceiling only.


HANDOFF CHECKLIST — what's DONE vs what's LEFT (matching bd beads)
------------------------------------------------------------------
school-core-hll  (Per-student turn budget)                    -> CLOSED.
   Code: 4092a69. Test: test_orca_timeout_budget.py. Verified: 1332 pass / 16
   skip / 0 fail. Accepted: budget table regenerated from live code;
   8-diploma turns = 720s < 900s crew cap. Dependency removed from btm.

school-core-btm  (Provision Mac runner for 4-parallel)       -> OPEN.
   Code/provision already in place (a01f1ba + fe7dfab) but NOT yet LIVE.
   Remaining (runner-side, not repo commits):
     A. Set runner env vars (launchd plist / shell that invokes issue_bridge):
          CREW_MAX_PER_CYCLE=4
          CREW_RUNNER_SLOTS=4
          CREW_CYCLE_BUDGET_SECONDS>=3630   (4*900+30; at 1800 it denies all 4)
     B. Deploy data/fleet.json to the runner (gitignored; don't push via git).
          The file exists in this repo at data/fleet.json with 4 worktrees, cap 4.
     C. Confirm on runner:
          - fm-spawn/Orca can create 4 concurrent worktrees (not serialized/colliding)
          - OmniRoute gateway localhost:20128 survives 4 concurrent LLM streams
     D. Confirm CI: after b7bd934, the next chore: publish board [skip ci] lands
          on board-publish, NOT main.

   Note: CREW_RUNNER_SLOTS and CREW_MAX_PER_CYCLE default to 1, so even with
   fleet.json provisioned, admissions still cap at 1 today unless these env vars
   are set on the runner. The admission code already supports >1 (tested in
   test_parallel_fleet.py and test_crew_admission.py); the gap is the env + the
   runtime deploy.


ITSELF (archive): anything else the session peeked at
------------------------------------------------------
• crew_dispatch.py / school_scheduler.py / issue_bridge.py / resilience.py —
  all read. These are the code paths behind 1-4 above; the findings are the
  headline. No follow-up code exploration pending unless you want a deeper audit
  of the cross-daemon stale-registry guard (watch-item only; not blocking).
• Bot Mode MCP surface: school_bot_list was called; bots exist as a separate
  execution topology (own SOUL.md / skills / memory / cwd). school_bot_execute
  is functional (pong returned OK). school_bot_mention exists for bot-to-bot
  escalation.
• school-core-hll notes (append-notes) recorded the SESSION CLOSURE on the turn
  budget fix; school-core-btm notes recorded the runner checklist above.


CAUTIONARY NOTES / DOWNSIDES TO BE AWARE OF
-------------------------------------------
• The 79d0180 bot commit to main happened once before the redirect took effect.
  It is NOT a recurring leak currently, but it IS a reminder that the bot can
  still commit to main if a separate push path exists that the redirect didn't
  cover. If you see a new chore: publish board [skip ci] on main after b7bd934,
  that's a regression; investigate the bot's git push paths (not just the
  school-loop.yml jobs).
• data/fleet.json is gitignored. git add -f was used to ship it now; but the
  actual runtime deploy to the Mac runner is a SEPARATE operation (you cannot
  rely on git to carry it). Don't assume "it's in the repo = it's live."
• CREW_CYCLE_BUDGET_SECONDS >= 3630 is a real requirement. At 1800 (default-ish
  for a 30-min job) admission DENIES all 4 (proven: test_parallel_fleet RED
  until the budget was raised). If you raise capacity without raising the cycle
  budget, admission will silently refuse — that's the turns/admission coupling.
• The 60s transport ceiling for school_bot_execute is an EXTERNAL constraint
  (tool/wrapper), not something we can fix in this repo today. If you want
  non-coding Bot Mode tasks to complete in parallel, that ceiling must be raised
  in the MCP transport / wrapper layer, or school_bot_execute must be run with a
  longer-lived client that doesn't hard-kill at 60s.
• The test infrastructure reads a lot of orca_executor.py / school_scheduler.py
  / crew_dispatch.py; any further code change should come with its own
  test (RED first, then GREEN), not just a config slam. The bd beads are the
  change tracking for that.


Suggested next action (runner provisioning)
-------------------------------------------
Still TODO (runner-side):
   1. Confirm/adjust the 3 env var values (CREW_MAX_PER_CYCLE=4,
      CREW_RUNNER_SLOTS=4, CREW_CYCLE_BUDGET_SECONDS>=3630) and how the runner
      launches issue_bridge (launchd plist? a cron? a manual shell?). We don't
      know the launch mechanism from this repo — must be confirmed on the machine.
   2. Decide how to get data/fleet.json onto the runner:
        - Option X: leave data/ gitignored globally, and manually place the file
          on the runner's checkout (or author it there). Lowest-risk; keeps the
          per-run fleet topology out of version control.
        - Option Y: un-ignore data/fleet.json (or path-scoped .gitignore rule)
          so it rides via git while other runtime state (crew_runs.json, scores.json)
          stays ignored. More convenient long-term; changes the repo's gitignore.
   3. Verify the two unprovisioned infra pieces on the runner before declaring
      4-parallel "live": fm-spawn/Orca concurrency + OmniRoute gateway capacity.
   4. After first live 4-parallel cycle: inspect crew_runs.json + assigned
      worktree_ids to confirm 4 distinct slots and no admission denial.
   5. Confirm main has no new bot commits after the cycle (CI fix held).


END
