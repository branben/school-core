# Bookbag vs bd (beads) — separation of concerns

Two stores, two concerns. The naming overlap is intentional: the **bead id** is
the shared key. But what each store owns is disjoint — never merge them.

## The boundary

| | `bookbag.py` (~/.hermes/bookbag/) | `bd` (beads, `.beads/dolt/`) |
|---|---|---|
| **It is** | The durable **judgment/result** record for one task | The **task tracker** / kanban / issue board |
| **What lives here** | student output, `cto_verdict`, `coo_verdict`, `cto_score`, `coo_score`, `findings`, `accepted` | status (open/claimed/closed), assignee, dependencies, slice metadata |
| **Who writes it** | student (writes the notebook), teachers (fill verdicts), principal (`accepted`) | agent + principal (`bd update/close`), scheduler |
| **Lifecycle signal** | "did the two judges accept this specific artifact?" | "what is the state of this slice of work?" |
| **Consumers** | teachers, director review, `validate_verdict_record`, conductor handoff | triage board, `bd ready`, cross-machine sync (`bd dolt push/pull`) |

## The shared key

The **bead** is the link. `bd` names the slice (e.g. `coder-python-coding-abc123`),
and the bookbag for that slice lives at `<bead>.json`. The bookbag id is the
bead id — bookbag never gets its own independent id.

## Why this matters

1. **One source of truth for task state.** If bookbag duplicated status/claim,
   two stores would drift (a `bd` row can be closed while a bookbag says pending
   or vice versa). `AGENTS.md` mandates `bd` for ALL task tracking — bookbag
   adding tracker fields contradicts that.
2. **The verdict stays auditable.** Acceptance must be reconstructable from the
   bookbag alone (verdicts + scores + findings + `accepted`). Task lifecycle is
   not needed to audit a judgment — and judgment is not needed to manage the
   board.
3. **Migration safety.** Slice re-opening, reassignment, or kanban moves change
   `bd` only; the judgment record is untouched.

## Rules of thumb

- Adding a **claim/status/board** field to bookbag → put it in `bd` instead.
- Adding a **quality/verdict/finding** field to `bd` → put it in bookbag instead.
- `generate-and-verify` (Slice) → the boundary is "generate the artifact in the
  worktree, verify in the verifyShell", not "move bookbag data into bd".

See `docs/school-core-architecture.md` (pedagogical rule 3: "`bd` tracks *task
state*; the bookbag tracks *judgment*. Two concerns, two stores.").