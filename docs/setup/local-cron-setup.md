# Local Cron Setup — Autonomous Runtime for school-core

> **For a fresh Hermes agent:** clone this repo → run `setup.sh` → set env vars → optionally create the 2 local cron entries below → the framework runs autonomously.

## What's in the repo vs. what's per-machine

| Layer | Where | Who sets up | What it does |
|---|---|---|---|
| **Cloud heartbeat** | `school-loop.yml` (committed) | Clone + secrets | Runs the full pipeline every 5 min on the self-hosted Mac runner: fetch issues → classify → dispatch crews → CTO/COO review → PR → checkpoint state. No local cron needed for this. |
| **Local digest** | `scripts/school_daily_digest.py` (committed) + local crontab | User creates 1 crontab entry | Sends a daily Telegram digest of board state (NOW/NEXT/CUT lanes) at 9am weekdays. |
| **Local health doc** | `scripts/school_health_doc.py` (committed) + local crontab | User creates 1 crontab entry | Generates an interactive HTML health page at 8am weekdays with board state, failure classes, score trends, and clickable triggers. |
| **Secrets + env** | Local `.env` + credentials | User | OMNIROUTE_API_KEY, GH_TOKEN, AGENTMAIL_API_KEY, Telegram bot token. |
| **Cron config** | `~/.hermes/cron/jobs.json` (local-only, not in repo) | User | Hermes cron daemon config. The 2 entries below are examples. |

## Why only 2 local crons (not 5)

The 3 deleted entries (`school-core (unified)`, `agent-school-teacher-cto`, `agent-school-teacher-coo`) were **redundant** with `school-loop.yml`:

- `school-loop.yml` already runs `issue_bridge --once` every 5 min on the Mac self-hosted runner — the full pipeline including CTO/COO review.
- Restoring `school_cron.sh` locally would **double-execute** the bridge every 5 min.
- `teacher_tick_cto.sh` / `teacher_tick_coo.sh` ticked review separately, but the bridge handles CTO/COO review internally (reads `cto_verdict`/`coo_verdict` from task results).

Keep the 2 that do something the GitHub Action doesn't:
- **Daily digest** — different channel (Telegram), different time (9am), different output (text summary vs HTML board).
- **Health doc** — different output (interactive HTML with clickable triggers), different time (8am).

## Setup steps

### 1. Clone + bootstrap

```bash
git clone https://github.com/branben/school-core.git
cd school-core
./setup.sh
```

### 2. Set env vars

```bash
# Required for the pipeline
export OMNIROUTE_API_KEY=<your-key>
export GH_TOKEN=<your-github-token>        # or set in .env
export AGENTMAIL_API_KEY=<your-key>        # optional, for alerts

# For the daily digest (Telegram)
export TELEGRAM_BOT_TOKEN=<your-bot-token>
export TELEGRAM_CHAT_ID=<your-chat-id>
```

See `.env.example` for the full contract.

### 3. Create the 2 local cron entries

The cron daemon is Hermes's built-in scheduler. Create entries via `hermes cron`:

```bash
# Daily digest — 9am weekdays, sends board summary to Telegram
hermes cron add \
  --name "school-daily-digest" \
  --schedule "0 9 * * 1-5" \
  --script "python3 /path/to/school-core/scripts/school_daily_digest.py" \
  --workdir "/path/to/school-core" \
  --no-agent

# Health doc — 8am weekdays, generates interactive HTML
hermes cron add \
  --name "school-health-doc" \
  --schedule "0 8 * * 1-5" \
  --script "python3 /path/to/school-core/scripts/school_health_doc.py" \
  --workdir "/path/to/school-core" \
  --no-agent
```

Or edit `~/.hermes/cron/jobs.json` directly — the schema is visible in the existing entries.

### 4. Verify the pipeline works

```bash
cd school-core
nix develop .#verifyShell   # Tier A required
python issue_bridge.py --repo branben/sound-royale-ny --once
```

### 5. (Optional) Point at a different repo

The `SCHOOL_REPO` env var in `school-loop.yml` controls the target. Change it to point the framework at any repo:

```yaml
env:
  SCHOOL_REPO: "owner/your-repo"
```

The local digest and health doc scripts read `data/board.json` — they're repo-agnostic as long as the board is populated.

## What a fresh agent user gets from the repo

- `school-loop.yml` — the 5-min heartbeat (no setup beyond secrets)
- `scripts/school_daily_digest.py` — the digest script (crontab entry is per-machine)
- `scripts/school_health_doc.py` — the health doc script (crontab entry is per-machine)
- `docs/setup.md` — Tier A/B setup (Nix, verify shell, pipeline run)
- `docs/sdlc/` — the full SDLC framework (loops, phase-map, context-skills)

What a fresh agent user must set up per-machine:
- Secrets (`.env` + credentials)
- Local cron entries for digest + health doc (if they want them)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for the digest

## Deleting the redundant crons

If you have the 3 old entries still in `~/.hermes/cron/jobs.json`, remove them:

```bash
hermes cron remove --id <school-core-unified-id>
hermes cron remove --id <teacher-cto-id>
hermes cron remove --id <teacher-coo-id>
```

Or edit `jobs.json` directly and remove the entries with those IDs.

## Design rationale

The framework is **repo-agnostic**: the pipeline runs against whatever `SCHOOL_REPO` points at. The local crons are **delivery-specific**: they send digests and generate health docs for the operator's machine. Separating them means:

- A new user clones the repo and gets the pipeline + scripts immediately.
- They opt into the local delivery layer (digest + health doc) with 2 crontab entries.
- The cloud heartbeat (`school-loop.yml`) works without any local cron.

This is the split recommended by `omh-automation-blueprint`: host automation (crontabs) stays on the operator's machine; the framework ships the scripts + a setup note.
