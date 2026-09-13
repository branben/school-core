# Slice-tracking contract

Shared rule for how every participant (human, Hermes, OpenHands) tracks slices
on the triage board without forking the source of truth.

## The one rule

> **The board HTML is a read-only *view*. The write path is `bd` (beads) + git.
> No agent ever edits the board file to move work.**

The durable state that everyone agrees on lives in:

- `.beads/` — issue state, claims, comments (the task DB)
- `data/last_run.json` — last loop outcome per issue (the pipeline truth)
- `git` — the actual slice contents (code, docs)

## How the board maps pipeline state to lanes

| Lane | Meaning | Source statuses (`last_run.json`) |
|---|---|---|
| **Now** | actively worked this cycle | `in_progress`, `crew_in_flight` |
| **Next** | queued behind the current slice | `in_review`, `retry`, `blocked` |
| **Later** | not yet touched by the loop | open, unprocessed (default) |
| **Cut** | done or not actionable | `success`, `done`, `error`, `school-failed` |

Every card keeps its **original status as the reason**, so “cut” never loses
*why* it was cut.

## How to move work (humans AND agents)

1. **View** the board: `python3 scripts/build_board_json.py` then open
   `docs/templates/triage-board.html` (or the hosted copy under `docs/site/`).
2. **Plan**: drag cards between lanes. Nothing is persisted by the drag.
3. **Apply** (`bd` is the write path):

```bash
# a slice moves to Now — claim it
bd update <id> --claim

# a slice is queued / deprioritized
bd update <id> --status open

# a slice is cut with the reason preserved
bd close <id> --reason="Triage cut"
```

The board’s **“Copy as bd commands”** button prints exactly these commands for
the cards you moved (idempotent — unchanged cards produce nothing). Paste them
into a terminal, or hand them to an agent to run via its `bd` tool.

## What agents must NOT do

- Do not edit `triage-board.html`, `board.json`, or the board’s JS to move work.
- Do not treat the exported commands as already-applied. They are a proposal
  until a human or an agent runs `bd`.
- Do not create a parallel task list (TodoWrite / markdown TODO) that holds
  shared project state. Beads is the shared store.
  (See `.agents/skills/beads/SKILL.md`.)

## Why this shape (see `docs/plans/2026-09-13-001-feat-effective-html-sdlc-plan.md`, U5 / ADR 0005)

- **No second store** → no drift surface. Cards can be wrong-ordered locally
  without corrupting the pipeline; `bd` is the only authority.
- **Realtime = delta polling** of `board.json` (as the live dashboards already do);
  there is no SSE webpush here, by design (ADR 0005: CI runs every 5 min,
  JS polls every 15 s).
- **The board is an input, not a log.** Dragging is a *proposal*; the export is
  the handoff that turns a proposal into commands.

## Diagnostic: stale or missing data

```bash
python3 scripts/build_board_json.py          # rebuild from last_run.json + cache
python3 -m pytest tests/test_slice_tracking.py -q   # ensure mapping/export still correct
```