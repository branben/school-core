# The two loops: inner & outer

Canonical overview of how school-core runs: the outer loop turns an epic into
tickets; the inner loop turns a ticket into a merged PR. Everything lands as
`bd` slices (one testable concern per slice), and every slice walks the inner
loop. This page is the map; `daily-loop.md` is the ritual a session actually
runs.

Source of truth: the SDLC plan
(`docs/plans/2026-09-13-001-feat-effective-html-sdlc-plan.md`). Phase mapping:
`docs/templates/phase-map.md`. Context/skills per phase:
`docs/sdlc/context-skills.md`.

---

## Outer loop — epic → PRD → SPEC → tickets

```text
epic decides GF/BF
  ├─ PRD    → Tier-3 .md (source) + pitch-doc .html render (tot living/@hash)
  ├─ SPEC   → Tier-3 .md (source) + impl-plan .html render (html-plan discipline)
  └─ TICKET → beads (one testable concern) + risk-map .html view
```

The outer loop is **human-driven** (an epic arrives; a PRD/SPEC is written and
reviewed); the plan deliberately ships the *doc layer* for it before any
automation ("inform → PRD → SPEC → ticket" fan-out is deferred). Tickets are
beads; moving them is `bd` (write path), never the board HTML.

## Inner loop — Prime → Plan → Implement → Validate → Review → PR

```text
Prime    → Tier-2 module-map .html (html-diagram), regenerated from repo data
Plan     → Tier-1/3 tweakable-plan .html (html-plan; decisions first)
Implement→ markdown deviation log (unknowns-09 pattern)
Validate → Tier-1 prototype/wireframe .html (html-prototype; states + boundary + export)
Review   → Tier-3 guided review (plannotator-guide; guide.json validated)
PR       → Tier-3 PR-writeup .html published via tot (living + @hash URLs)
```

Every slice runs Prime → … → PR as its own CE/TDD loop; a failed sub-step
loops back to **that** step, not the whole slice. The daily ritual
(`docs/templates/daily-loop.md`) enforces it in the morning: `bd ready` →
board refresh → pick one Next slice → state "done means" → work → validate →
review → bead write.

## Inner-loop source block (chain of custody)

Every inner-loop artifact carries a mandatory `source` block that ties it back
to the ticket and the SPEC. An empty or missing required field causes the
artifact's render to fail-closed (exit non-zero), so the inner loop cannot run
on an orphaned slice.

**Schema** (YAML front-matter in `.md` artifacts, HTML comment block in `.html`):

```yaml
---
bead:       <bd id, e.g. school-core-fip>
spec:       <spec file path> · <milestone id>
decisions:  <comma-separated SPEC accepted-decision ids>
prev:       <prior inner artifact path or "none">
---
```

- `bead`, `spec`, and `decisions` are required on every inner artifact.
- `prev` is required from PLAN onward (PRIME = `none`).
- REVIEW's `guide.json` adds `artifacts` (list of the four prior artifacts)
  and `spec_acceptance` (verbatim acceptance criteria from the SPEC).
- The review gate (`scripts/review_gate.py`, check #5) verifies the chain:
  `guide.json` must carry all five required fields, and every cited artifact
  must exist in the PR diff or base tree. Exit non-zero with
  `SOURCE-BLOCK MISSING — <which field>` on any gap.

**Artifact chain:**

| Phase | Artifact | Sources (reads) | Forward (feeds) |
|---|---|---|---|
| PRIME | `prime-module-map.html` | bead · spec · decisions | PLAN |
| PLAN | `plan-tweakable.html` | bead · spec · decisions · prev: prime | BUILD |
| BUILD | `dev-log.md` | bead · prev: plan | TEST |
| TEST | `validate-prototype.html` | bead · acceptance-crit · prev: dev-log | REVIEW |
| REVIEW | `guide.json` | bead · spec · acceptance · [module-map, plan, dev-log, prototype] | PR |
| PR | `pr-writeup.html` | bead · canonical = spec · prev: review.verdict | merged |

The seam (outer → inner) is the first link: a bead **is** a SPEC milestone,
handed off at the gate. PRIME's source block is the first citation — so the
moment the inner loop starts, it is already bound to the ticket. If a ticket
has no bead, or the bead's spec-milestone is missing, PRIME's render fails
before any code runs.

## Phase → documentation form → tool (canonical)

See [`docs/templates/phase-map.md`](../templates/phase-map.md) for the full
table (proper doc type, authoring skill, canonical source, transport, build +
verify contract, template map). The one line: HTML where a human must
**react/compare/decide/feel**; markdown canonical where the artifact is
**data** (tickets, verify results, scores).

| Phase | Form (tier) | Authoring skill | Canonical source | Template |
|---|---|---|---|---|
| Pre-PRD exploration | Tier-1 exploration / directions | `html-wireframe` | none | `exploration-wireframe.html` |
| PRD | Tier-3 pitch-doc render | `html` + `design-artifact` | `.md` in git | `prd-pitch.html` |
| PRD alternatives (GF/BF) | Tier-1 side-by-side compare | `html-wireframe` | none | `prd-alternatives.html` |
| SPEC | Tier-3 impl-plan render | `html-plan` | `.md` in git | `spec-implementation-plan.html` |
| Ticket | markdown→beads; risk-map view | `html` + risk-map callouts | beads | `ticket-risk-map.html` |
| Prime | Tier-2 module map | `html-diagram` | repo data (generator) | `prime-module-map.html` |
| Plan | Tier-1/3 tweakable-plan | `html-plan` | `.md` (or ephemeral) | `plan-tweakable.html` |
| Implement | markdown deviation log | — | `.md` in git | — |
| Validate | Tier-1 prototype / wireframe | `html-prototype` | none | `validate-prototype.html` |
| Review | Tier-3 guided review | `plannotator-guide` | `guide.json` (validated) | `guided-review-guide.schema.json` |
| PR → human | Tier-3 PR-writeup render | `html` (report register) | `.md` in git | `pr-writeup.html` |

## Where the pieces live

- **Context + skills per phase** — `docs/sdlc/context-skills.md` (ripwire
  verbs, plannotator authoring skills, the three-layer model).
- **Tier model + build/verify contract** — `docs/templates/TIER.md`.
- **Templates for every phase** — `docs/templates/` (see table above).
- **Tracking contract (board = view, `bd` = write)** —
  `docs/templates/slice-tracking-contract.md`; the board itself is
  `docs/templates/triage-board.html` (reads `data/board.json`, built by
  `scripts/build_board_json.py`).
- **Daily ritual** — `docs/templates/daily-loop.md` (agent reads it first in a
  fresh session).
- **Engines/scripts** — `scripts/html_module_map.py` (U2), `html_render_md.py`
  (U3), `check_html_artifact.py` (U4), `build_board_json.py` (U5),
  `dispatch_cloud_slice.py` + `trust_envelope.py` (U7).
- **Cloud lane (U7)** — `.github/workflows/cloud-lane.yml`: label → bead →
  OpenHands Cloud conversation → PR; trust envelope in
  `scripts/trust_envelope.py`.

## Session flow (how the two loops meet)

1. **Start (outer + inner):** `bd ready` → refresh board → pick ONE slice
   (Next → Now, single-concern) → write "done means" aloud.
2. **Work (inner):** Prime (module map) → Plan (tweakable-plan, decisions
   first) → Implement (dev log) → Validate (prototype where it helps; run the
   gates) → Review (guided review) → PR (+ PR-writeup).
3. **End (cycle rule):** bead write for the worked slice (`bd close <id>` /
   `bd update <id> --status open`) **before** starting the next slice; refresh
   the board.

There is no step that mutates the tracking state outside `bd` + git. The board
is a view; HTML artifacts are documents; the loop is measured by bead writes.