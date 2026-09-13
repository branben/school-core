# Daily loop — start-of-day ritual

Read this first in a fresh session. Execute top to bottom:

**Prime → Plan → Implement → Validate → Review → PR.**

This is a ritual, not a contract. The contract lives in
[`slice-tracking-contract.md`](slice-tracking-contract.md); the view is
[`triage-board.html`](triage-board.html). Reference both, duplicate neither.

## Prime

Exactly three commands, no new tooling:

```bash
bd ready                       # pull open slices
python3 scripts/build_board_json.py   # refresh the projection (writes data/board.json)
# open docs/templates/triage-board.html
```

> If `bd` is unavailable in a container: read `.beads/interactions.jsonl` (passive export).

## Plan

Enforce the repo's single-concern rule before anything else: pick **ONE** slice from
`next` / `later` → `now`. State its exit checks **always before implementing**:

> **Done means:** _write the "done means" line here, out loud, first._

If no `next` slice exists: create (or pull) a bead for the oldest open item, or surface the gap
to the principal — don't invent tooling.

Lane semantics, as defined by `slice-tracking-contract.md` (board is a read-only view; `bd` is the
write path):

| Lane | Statuses |
|---|---|
| Now | in_progress, crew_in_flight |
| Next | in_review, retry, blocked |
| Later | open, unprocessed |
| Cut | done, success, error, school-failed |

## Implement

Work the one slice. One concern per cycle. Follow `AGENTS.md` conventions.

## Validate

Run the quality gates that apply (tests, linters, build). The slice is done when its exit checks pass.

## Review

Re-read the slice against the "done means" line written in Plan.

## PR

Open the PR per repo process. If no PR applies, the cycle still ends with the bead write below.

## Cycle end (unconditional)

Every cycle ends with a bead write for the worked slice — `bd close <id>` or
`bd update <id> --status open` — **before the next cycle starts**. Never start a new slice
with the previous one unwritten.

## Define done here — did today's run follow the loop?

- [ ] Ran the three Prime commands (`bd ready`, board build, opened the board)
- [ ] Picked ONE slice from `next`/`later` → `now`
- [ ] Stated its exit checks before implementing ("done means" written first)
- [ ] Ended the cycle with a bead write (`bd close <id>` / `bd update <id> --status open`) for the worked slice
- [ ] Refreshed the board projection
- [ ] Surfaced blockers to the principal