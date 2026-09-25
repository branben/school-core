# Cron Audit — Dead Scanner Cleanup (2026-09-20)

> **For a fresh Hermes agent:** this is a record of which cron entries were audited and removed. Not a setup guide — see `local-cron-setup.md` for that.

## What was audited

All 26 cron entries in `~/.hermes/cron/jobs.json` were audited on 2026-09-20. The school-core framework crons were analyzed for redundancy with `school-loop.yml`; the daily scanners were analyzed for ongoing value.

## What was removed

### Redundant school-core crons (3 entries)

| Entry | Why removed |
|---|---|
| `school-core (unified)` every 5m | Redundant with `school-loop.yml` execute job — both run `issue_bridge --once` every 5 min on the same Mac. Would double-execute. |
| `agent-school-teacher-cto-branben__sound-royale-ny` every 5m | Redundant — `issue_bridge.py` handles CTO review internally (reads `cto_verdict` from task results). The missing `teacher_tick_cto.sh` script was never restored. |
| `agent-school-teacher-coo-branben__sound-royale-ny` every 5m | Redundant — same as above for COO. The missing `teacher_tick_coo.sh` script was never restored. |

### Dead daily scanners (2 entries)

| Entry | Why removed |
|---|---|
| `V14 Daily Signal Scanner` 30 16 * * 1-5 | Dead — script `v14_scanner.sh` missing, purpose no longer active. |
| `Tasty-Papers Concierge (memory graph index)` 30 7 * * 1-5 | Not read — script missing, failure streak 17, last error "Script not found". No longer serves a purpose. |

## What was kept

### School-core framework crons (2 entries — both working)

| Entry | Purpose | Script |
|---|---|---|
| `school-daily-digest` 0 9 * * 1-5 | Daily Telegram digest of board state (NOW/NEXT/CUT lanes) | `scripts/school_daily_digest.py` (in repo) |
| `school-health-doc` 0 8 * * 1-5 | Interactive HTML health page with board state, failure classes, score trends, clickable triggers | `scripts/school_health_doc.py` (in repo) |

Both scripts are committed in `scripts/` and work. The cron entries reference them without the `scripts/` prefix — the cron daemon resolves them relative to the Hermes scripts dir.

### Daily AI Research Scan (kept — re-scope needed)

| Entry | Status |
|---|---|
| `Daily AI Research Scan` 0 7 * * 1-5 | **KEEP** — but re-scope from ARCSIF papers to a working source. Last error: HTTP 429 rate limit. Purpose: find papers → import to KnowledgeCore → improve school-core. Still valid if the source works. |

### Personal/trading crons (11 entries — all kept)

These serve your life, not the SDLC framework. All kept: vault-refresh, inbox-sweep, Pre-London Open Scanner, Killzone Orchestrator, Weekly Vault Consolidation, Same-Day Exit, Daily Capitol Trades Pre-fetch, Aquarium Care, NYC job-market weekly scan, Career Compass weekly OSS scan, daily-twitter-digest.

### Infrastructure crons (2 entries — all kept)

`daily-storage-clean` and `quarterly-prune` — hygiene, support everything.

## Final count

26 → 21 cron entries. 5 removed (3 redundant school-core + 2 dead scanners).

## Framework takeaway

The school-core framework's cron value is in **2 delivery-layer crons** (digest + health doc), not in the pipeline heartbeat — that's `school-loop.yml` (committed, no local cron needed). A fresh agent user clones the repo, gets the pipeline + scripts, and optionally creates 2 crontab entries for daily visibility. See `docs/setup/local-cron-setup.md`.
