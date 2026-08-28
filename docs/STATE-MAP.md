# School-Core State Map

> Where truth lives. Read this before your first reading.

## Repository Layout

```
school-core/                      ← The repo (git)
├── .beads/                       ← Issue tracker (Dolt DB)
│   ├── dolt/                     ← Beads issue data
│   └── issues.jsonl              ← Passive export (do NOT edit)
├── .entire/logs/                 ← Entire CLI session logs
├── .hermes/                      ← Hermes-specific config ( Serena, etc.)
├── .venv*/                       ← Python virtualenvs (gitignore these)
├── data/                         ← Consumer-side ledger
│   ├── crew_runs.json            ← Bridge's durable registry (8 records)
│   ├── last_run_batch.json       ← Atomic-write journal
│   ├── processed_issues.json     ← Issue completion tracker
│   ├── retry_issues.json         ← Retry attempt counts
│   └── scores.json               ← Agent scoring data
├── docs/                         ← Generated documentation
├── src/                          ← Python source
│   └── entire_review.py          ← Entire CLI pre-merge sensor
├── tests/                        ← Test suite
│   ├── conftest.py               ← Shared fixtures (CRITICAL: read this first)
│   └── test_*.py                 ← Test modules
├── .github/workflows/            ← CI definitions
│   └── ci.yml                    ← CI pipeline (py3.9 + py3.12 matrix)
├── pr_creator.py                 ← PR creation
├── issue_bridge.py               ← Issue dispatch + grading
├── crew_dispatch.py              ← Crew lifecycle (spawn → teardown)
├── crew_ledger_reconcile.py      ← Producer/consumer audit
├── conductor.py                  ← Principal orchestrator
├── verify_gate.py                ← Pre-merge build gate
├── bookbag.py                    ← Verdict persistence
└── school_scheduler.py           ← Admission + cap logic
```

## State Locations (the trap)

| State | Location | In repo? |
|---|---|---|
| Producer status files | `~/.hermes/school-core-fm-config/state/*.status` | ❌ Outside |
| Consumer ledger | `school-core/data/crew_runs.json` | ✅ Inside |
| Issue tracker | `school-core/.beads/dolt/` (Dolt DB) | ✅ Inside |
| Verdicts/scores | `bookbag` (in-memory + `data/scores.json`) | ✅ Inside |
| Entire CLI logs | `school-core/.entire/logs/entire.log` | ✅ Inside |
| Orca worktrees | `~/orca/workspaces/` | ❌ Outside |
| Agent profiles | `~/.hermes/profiles/` | ❌ Outside |
| Cached clones | `~/.cache/branben__*/` | ❌ Outside |

## The Conventions

### Git diff (NEVER use moving tip)
```bash
# WRONG — renders main's gains as branch deletions
git diff main..HEAD

# CORRECT — diff against the fork point
git diff $(git merge-base main HEAD)..HEAD
```

### Python version
- **CI**: py3.9 (`compileall` + `pytest`) AND py3.12 (pytest matrix)
- **Hermes venv**: py3.11 (where most devs run tests)
- **Runtime**: Orca worktrees use whatever the agent profile pins

### Python syntax floor
- **Annotations**: `str | None` is OK if file has `from __future__ import annotations`
- **Runtime**: `str | None` is NOT OK on py3.9 (e.g., `isinstance(x, str | None)`)
- **Test**: `Optional[str]` is the safe choice for test files (no future import guaranteed)

### Directory naming
Tests assert `REPO_PATH.endswith("school-core")`. Cloning to `/tmp/wk-ci` fails.
```bash
# WRONG
git clone repo /tmp/wk-ci

# CORRECT
git clone repo /tmp/school-core
# OR: relax the test assertion (preferred)
```

### Pipe discipline
```bash
# WRONG — pipe reports the LAST element's exit code
python -m compileall -q . | head

# CORRECT — run bare, read the real exit code
python -m compileall -q .
echo "exit=$?"
```

## Test Isolation (conftest.py)

`tests/conftest.py` monkeypatches:
- `crew_dispatch.FM_HOME`, `STATE_DIR`, `DATA_DIR`
- `crew_dispatch.CREW_RUNS_FILE`
- `issue_bridge.CREW_RUNS_FILE`
- `activity_log.ACTIVITY_LOG_PATH`
- `decision_log.DECISION_LOG_PATH`
- `escalation_log.LOG_PATH`
- `sleep_state.SCORES_PATH`
- `trajectory.TRAJECTORY_DIR`
- `bookbag.BOOKBAG_DIR`
- `scoring.ScoreStore.__init__` (redirects to tmp)

**A test that creates its own `.venv*` in the workspace will break `compileall`.**

## Producer/Consumer Invariant

Every status file with a terminal verb (`done`/`failed`) MUST have a ledger record in `data/crew_runs.json`.

Audit: `python crew_ledger_reconcile.py`

## Key File:line Map

| What | Where |
|---|---|
| Evidence comparator (SHA validation) | `crew_dispatch.py:1268-1300` |
| Repo attribution join | `crew_dispatch.py:1271-1280` |
| Retry-budget gate (qb4 fix) | `issue_bridge.py:2146-2157` |
| Entire gate (CRITICAL→veto) | `conductor.py:232-240` |
| Entire sensor | `issue_bridge.py:834-850` |
| Per-cycle cap | `school_scheduler.py:234-236` |
| Admission policy | `crew_admission.py:28-42` |
| Processed gate (qb4) | `issue_bridge.py:2146` |

## Before You Read Anything

1. Run `bd prime` — loads persistent memories
2. Run `python crew_ledger_reconcile.py` — know your baseline
3. Check `~/.hermes/school-core-fm-config/state/` — the producer
4. Check `data/crew_runs.json` — the consumer
5. Know which Python floor you're on
