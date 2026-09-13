---
title: "feat: Effective HTML as the SDLC documentation layer"
created: 2026-09-13
status: proposed
author: OpenHands (on behalf of the principal)
project: agent-school
tags: [html, sdlc, prd, spec, review, prototyping, documentation, effective-html, plannotator-guide]
origin: discussion with the principal on loop-level SDLC shape
---

# Effective HTML as the SDLC documentation layer

## Summary

Adopt the "unreasonable effectiveness of HTML" pattern (thariqs.github.io/html-effectiveness) as the
documentation layer for both loops, backed by two companion tools: **Plannotator's `effective-html`
skill collection** (authoring contract) and **`plannotator/guides` + `guides.show`** (review-phase
artifact format), with **`tot.page`** (git-backed publishing) as the transport for every
human-facing doc. The core claim of the movement is: a self-contained HTML artifact that a human
*reacts to, edits, or feels* beats a wall of markdown for decision, review, and handoff purposes.

This plan lands the *policy and scaffolding* — the three-tier documentation model, the phase-to-form
mapping, the minimal authoring/routing conventions, a **slice-tracking contract** (a
multiplayer-style triage board that stays a view over beads + git), a **daily workflow doc**
(the start-of-day ritual that makes the board + `bd` the operating rhythm), and a **cloud-lane
dispatch** (event → slice → PR without the Mac, with a trust envelope for auto-apply) — before any
phase is rewritten. It does not rewrite existing markdown plans, ADRs, or HANDOFF docs. It is
deliberately documentation-policy first, engine-optional later.

## Problem frame

The school's documentation today is uniformly markdown, in one shape: prose-heavy decision docs
(PRD/SPEC/ADR/plan) read top-to-bottom. The loop phases that benefit most from HTML are the ones
where a human must *react, compare, decide, or feel*:

| Phase | Current pain | HTML fix |
|---|---|---|
| Pre-PRD exploration | Three sequential walls of trade-off prose can't be "pointed at" | Side-by-side exploration/directions HTML |
| PRD (greenfield/brownfield decision) | "Lead with the demo" is impossible in .md | Pitch-doc HTML (demo → pitch → objections pre-answered) |
| SPEC | Milestones/risks/mockups flattened to a document you skim | Implementation-plan HTML (mockups, data-flow, risk table) |
| Ticket | One testable concern has no *visible* boundary | Annotated-diff HTML with a risk map; the risk map IS the slice test |
| Prime | 4-layer context is text; module shape is spatial | Module-map/request-path HTML rendered from repo data |
| Validate | Dogfooding interaction can't happen in a file you read | Prototype/wireframe HTML (states, boundary, export) |
| Review | Reviewer reads a flat diff, not a reading order | Chaptered guided-review HTML (guide.json, portable) |
| PR → human | "Here's a PR" has no living artifact to point at | PR-writeup HTML published as a living/frozen tot URL |

### Sources studied (2026-09-13)

- **thariqs.github.io/html-effectiveness** — 20 main demos + 11 "Know your unknowns" demos. The
  second set is organized as pre/during/post-implementation (8/1/2). 31 artifacts total. The lesson
  extracted is the *density axis* (read → react → edit) and the *tier axis* (disposable viewport →
  canonical-HTML → canonical-markdown-render-on-share).
- **github.com/plannotator/effective-html** — six authoring skills: `html` (router), `design-artifact`
  (creative direction), `html-wireframe` (structural, deliberately low-fi), `html-prototype`
  (states/boundary/export), `html-plan` (source-commitment-preserving plans), `html-diagram`
  (model + rendering method chosen to fit the relationship). Each ends with a build+verify contract.
- **guides.show + github.com/plannotator/guides** — "Guided Review": a chaptered walkthrough of a
  diff exported as ONE portable HTML file (validated `guide.json` shape). Can be shared as an
  encrypted link where the host never sees the bytes.
- **github.com/plannotator/tot** — `tot page.html` publishes to a living URL (`index.html`-style:
  version-less = branch tip, `@hash` = commit snapshot). Uploads direct browser deps; no config.

### The three tiers (the "proper documentation type" answer)

| Tier | Canonical source | Purpose | Regenerate policy | Tools |
|---|---|---|---|---|
| **Tier 1 — Disposable viewport** | None (throwaway `.html`) | A single decision: explore directions, interview, prototype feel, triage ordering, change-quiz | Render-and-discard; the prompt that made it is the seed, not the file | `html-wireframe`, `html-prototype`, exploration directions |
| **Tier 2 — Canonical HTML** | The `.html` itself (generated from repo data) | Living reference: design system, module map, feature explainer, verify/status dashboards | Regenerate from real tokens/symbols/state; keep generator + artifact in git | `html-diagram`, chart/status dashboards, design-system sheets |
| **Tier 3 — Canonical markdown, HTML render** | Markdown in git (diffable, reviewable, grep-able) | Long-lived institutional docs: PRD, SPEC, PR writeup, status, post-mortem, ADR | Keep .md authoritative; generate .html as a *view*; publish via tot | `html-plan` (source-preserving), pitch-doc, report, PR-writeup patterns |

**Mapping rule of thumb:** HTML is *right* where the artifact's job is to help a human react, decide,
or feel. Markdown stays canonical where the artifact's job is to be *data* — tickets (beads), verify
results, scores — because those must round-trip through Dolt/git and the CI gate.

## Decisions

1. **Adopt the three-tier model as documentation policy.** Tier 1 for decisions, Tier 2 for living
   references, Tier 3 for institutional docs. This plan codifies the mapping in the table below; it
   does not yet rewrite any single doc.
2. **Use Plannotator's skills as the authoring contract** when an HTML artifact is produced. The
   `html` skill routes to `html-wireframe` / `html-prototype` / `html-plan` / `html-diagram` by
   review question. `design-artifact` supplies creative direction only when the project has no
   design system already. The build contract is: one self-contained file, no build step, semantic
   landmarks, keyboard-operable, no external services, no overflow at mobile width.
3. **`html-plan` is the spine of the Tier-3 write path.** Its promise — "no commitment disappeared,
   accepted decisions separated from open questions, structure not inflated" — is what makes
   Tier-3 HTML trustworthy as a render of markdown. The existing markdown plan/ADR format is *kept*;
   `html-plan` only governs how an HTML render of one is made.
4. **`plannotator-guide` is the Review-phase artifact.** Chaptered by importance, every changed file
   in exactly one chapter (hard coverage rule, validated by `guide.json`), "guide, not review"
   calibration. Share as a file by default; encrypted `guides.show` link only on request. This fills
   the "Review = fresh context" gap as its own doc type.
5. **`tot` is the transport for human-facing Tier-3 docs.** Living URL for the branch tip,
   `@hash` URL for the frozen snapshot. The repo never stores a tot credential; `~/.tot` is
   machine-local, matching the existing "no credentials committed" security rule. Prefer the file
   path for anything security-sensitive; tot's "link is the key" model is fine for
   non-sensitive artifacts only.
6. **School-core is the demo, not the runtime.** This plan only wires the *policy + templates* into
   this repo. Generic "SDLC skills" that other repos install are Deferred (see Scope boundaries);
   the repo-specific curriculum stays here first, per the L3 "curriculum parameterized by repo" ADR.
7. **Slices land behind their own tests, not a big-bang rewrite.** Each implementation unit below
   is independently reviewable and test-backed, matching the U1–U6 product-slices convention.
8. **The slice tracker is a view over the durable store, not a second store.** The multiplayer
   board reads `data/board.json` (built from `last_run.json` + `.beads/interactions.jsonl`) and
   exports `bd` commands; it never writes the store directly. This is the ADR 0005 property extended
   to the triage surface: no drift surface, no new write authority.
9. **The daily workflow ships as a doc, not a tool.** `daily-loop.md` is the start-of-day ritual
   the agent executes and the human audits: `bd ready` + board refresh + single-concern Plan +
   cycle-end bead write. No new script, no new runner — the workflow is the glue that makes U5's
   board + `bd` write path something a session actually runs.
10. **The loop runs on events, not on the Mac being open.** The cloud lane (U7) converts a labeled
   GitHub issue into a bead claim + OpenHands cloud conversation → PR. Work distributes across
   machines (cloud pipeline + Mac curriculum) over the same `bd` store. The PR→human gate becomes an
   *exception filter*: routine slices auto-apply within a trust envelope; humans only see failures
   and high-risk changes.

## High-level design

```text
Outer loop (epic → PRD → SPEC → tickets)
  epic decides GF/BF
    ├─ PRD   → Tier-3 .md (source) + pitch-doc .html render (tot living/@hash)
    ├─ SPEC  → Tier-3 .md (source) + impl-plan .html render (html-plan discipline)
    └─ TICKET→ beads (one testable concern) + risk-map .html view (03-code-review-pr pattern)

Inner loop (Prime → Plan → Implement → Validate → Review → PR)
    ├─ Prime    → Tier-2 module-map .html (html-diagram), regenerated from repo data
    ├─ Plan     → Tier-1/3 tweakable-plan .html (unknowns/08 pattern; decisions first)
    ├─ Implement→ markdown deviation log (unknowns/09 pattern)
    ├─ Validate → Tier-1 prototype/wireframe .html (html-prototype; states + boundary + export)
    ├─ Review   → Tier-3 guided review .html (plannotator-guide; guide.json validated)
    └─ PR       → Tier-3 PR-writeup .html published via tot (living + @hash URLs)
```

## Phase → form → tool mapping (the deliverable of this plan)

| Phase | Proper documentation type | Authoring skill | Canonical source | Transport |
|---|---|---|---|---|
| Pre-PRD exploration | Tier-1 exploration / directions | `html-wireframe` (structural) | none | local |
| PRD | Tier-3 pitch-doc render | `html` + `design-artifact` | `.md` in git | tot (living + @hash) |
| PRD alternatives (GF/BF) | Tier-1 side-by-side compare | `html-wireframe` (2-3 directions) | none | local |
| SPEC | Tier-3 implementation-plan render | `html-plan` | `.md` in git | tot (living + @hash) |
| Ticket | Markdown→beads; risk-map view | `html` + risk-map callouts | beads (data) | local; HTML is a view |
| Prime | Tier-2 module map | `html-diagram` | repo data (generator) | local / docs/site |
| Plan | Tier-1/3 tweakable-plan | `html-plan` | `.md` (or ephemeral) | local / tot |
| Implement | Markdown deviation log | — (unknowns/09 pattern) | `.md` in git | local |
| Validate | Tier-1 prototype / wireframe | `html-prototype` | none (state+boundary+export) | local / tot |
| Review | Tier-3 guided review | `plannotator-guide` | `guide.json` (validated) | file or encrypted guides.show |
| PR → human | Tier-3 PR-writeup render | `html` (report register) | `.md` in git | **tot** (living + @hash) |

## Scope boundaries

### In scope

- The three-tier documentation policy and this phase→form→tool mapping, codified as the canonical
  table ("Phase → form → tool mapping") that later phases cite.
- A `docs/templates/` set carrying the Tier-1/Tier-2/Tier-3 patterns (see T1) so future agents and
  humans have a stable starting point.
- Minimal local helpers needed to prove the loop locally (see T4): a module-map generator and a
  Tier-3 render stub are allowed; both must be zero-dep and testable.
- The **slice-tracking contract** (U5): the triage-board view, the `bd`-command export, and the
  `data/board.json` exporter. These are reader + exporter only — they never write the durable store.
- The **daily workflow doc** (U6): the `daily-loop.md` ritual that turns the contract into the
  start-of-day rhythm (Prime → single-concern Plan → cycle-end bead write). Doc only; no new tooling.
- The **cloud-lane dispatch** (U7): an event trigger (GitHub label → bead → OpenHands cloud
  conversation → PR) plus a trust envelope for auto-apply. Same `bd` store; work can distribute
  across machines without conflicting.
- A mapping to the phase labels already used in this repo (PRD / SPEC / TICKET / Prime / Plan /
  Implement / Validate / Review / PR) so existing skills can adopt it without renaming.

### Deferred (explicitly out of this plan)

- **Installing `effective-html` / `plannotator-guide` skills into `.agents/skills/`.** That is a
  separate, reviewable change with repository-context implications (the skill text is third-party,
  untrusted input until read). This plan only *mandates the authoring contract*; the import
  decision is deferred to a follow-up.
- **Wiring an outer-loop engine** (inform → PRD → SPEC → ticket fan-out automation). The loop today
  is human-driven; this plan makes the doc layer correct first.
- **Greenfield/brownfield determination tooling** beyond PRD-alternatives HTML.
- **Replacing existing markdown plans/ADRs/HANDOFF.** They remain authoritative until a specific doc
  is migrated under the new policy.
- **Adding a tot dependency to CI.** `tot` is a human tool in this plan (machine-local `~/.tot`),
  explicitly to avoid committing credentials or adding a network dependency to the hermetic gate.
- **Raising `CREW_MAX_PER_CYCLE` / closing `fc7.3.6`.** Parallel inner-loop execution is the 
  scheduler's job and is governed by the existing scale plan, not this doc.

## Implementation units

### U1. Land the documentation-policy template set (`docs/templates/`)

**Goal:** Codify the three tiers as templates so the policy is actionable, not abstract.

**Requirements:** Each template must be self-contained, carry the build+verify contract, and cite the
source pattern it follows.

**Dependencies:** None (documentation only).

**Files:**

- `docs/templates/TIER.md` — the three-tier model + mapping rule of thumb.
- `docs/templates/phase-map.md` — the "Phase → form → tool mapping" table.
- `docs/templates/prd-pitch.html` — Tier-3 pitch-doc template (demo → pitch → objections).
- `docs/templates/spec-implementation-plan.html` — Tier-3 impl-plan template, `html-plan` discipline.
- `docs/templates/pr-writeup.html` — Tier-3 PR writeup template.
- `docs/templates/guided-review-guide.schema.json` — the `guide.json` schema shape (from
  `plannotator-guide`).
- `docs/templates/review_guide_example.json` — a non-code, illustrative example.

**Approach:** Port the studied patterns into minimal templates. Keep every template self-contained
(no build step, no external services). For Tier-3 templates, the markdown source stays the
authoritative store and the HTML is a render.

**Execution note:** Test-first only where a template is machine-checkable (the `guide.json` schema
validates with a tiny validator test). The `.html` templates are validated by opening them at
desktop and mobile widths, per the build contract.

**Test scenarios:**

- `guide.schema.json` accepts a well-formed guide and rejects a file placed in two sections.
- A header comment in each Tier-3 template states the canonical `.md` source path convention.
- Each `.html` template opens without a console error and has no external resource references
  (grep check).

### U2. Module-map generator for Prime (`scripts/html_module_map.py`)

**Goal:** Render a Tier-2 module map (the `04-code-understanding` pattern) from repo structure so
Prime deliverables are reproducible.

**Requirements:** Zero-dependency Python (stdlib only, like the school's verify-gate discipline).
Input: a repo path + optional root symbol. Output: one self-contained `.html` with a request-path /
module-layout view and the "key files / gotchas" skeleton filled from the repo.

**Dependencies:** U1 (template style).

**Files:**

- `scripts/html_module_map.py`
- `tests/test_html_module_map.py`

**Approach:** Reuse `repo_reader.get_file_tree()`/`find_relevant_files()` if wired, else stdlib
`pathlib` walking. The generator must be a *starter* — it renders the spatial frame (boxes/arrows)
and leaves the "why" to the agent, matching the movement's division of labor (structure by tool,
meaning by model).

**Execution note:** The output must open locally with no network. `docs/site/` is the existing home
for generated HTML (the board dashboards already live there).

**Test scenarios:**

- Running on a small fixture repo produces a valid `.html` with no external URLs.
- Running on a nonexistent path exits non-zero with a clear message.
- The generated file contains the root symbol's file when the symbol resolves.

### U3. Tier-3 render stub for SPEC/PRD/PR writeup (`scripts/html_render_md.py`)

**Goal:** Turn an existing markdown plan/PRD/PR-writeup into a Tier-3 HTML render without a build
step, preserving source commitments.

**Requirements:** Zero-dependency; accepts `.md` path + template id (`spec` | `prd-pitch` | `pr-writeup`);
outputs a self-contained `.html`. Must preserve every heading and section (the `html-plan` "no
commitment disappeared" guarantee is enforced by test: every markdown heading maps to a DOM element).

**Dependencies:** U1.

**Files:**

- `scripts/html_render_md.py`
- `tests/test_html_render_md.py`

**Approach:** A conservative markdown renderer (headings, lists, tables, code fences, blockquotes,
bold/code-inline only) — deliberately not a full CommonMark engine, to stay stdlib and safe. The
render wraps markdown in the template shell; unsupported constructs fall through as a clearly
marked "raw" block rather than being silently dropped.

**Execution note:** Keep scope tiny. This is a *stub* to prove the tier works locally, not a
documentation framework.

**Test scenarios:**

- A fixture `.md` with headings/lists/tables/code round-trips every heading into the DOM.
- An unsupported markdown construct (e.g. a definition list) fails safely, not silently.
- Output has no external resources (grep check) and no `&lt;script&gt;` injection from input text
  (escaping test).

### U4. Authoring-contract check (`scripts/check_html_artifact.py`)

**Goal:** Make the build+verify contract machine-checkable so CI can enforce it for Tier-2/Tier-3
artifacts.

**Requirements:** Zero-dependency. Checks a given `.html`: self-contained (no external `http(s)`
refs), single file, semantic landmarks present, no horizontal-overflow CSS smell, and for Tier-3
docs the presence of the canonical-source header.

**Dependencies:** U1.

**Files:**

- `scripts/check_html_artifact.py`
- `tests/test_check_html_artifact.py`

**Approach:** A conservative static checker (regex + `html.parser`), in the school's
"compiler before critic" spirit — a lint gate, not a rendering engine.

**Execution note:** This is the piece that lets CI say "the docs you generated are self-contained"
without opening a browser.

**Test scenarios:**

- A self-contained artifact passes; one with `https://external.example/x.js` fails loudly.
- A Tier-3 doc missing the canonical-source header fails.
- A file with an unclosed tag passes the *lint* (this is a static check, not a validator) with a
  clear "structural check only" note in its output.

### U5. Slice-tracking contract + multiplayer board mapping

**Goal:** Give the principal a real-time slice tracker they can watch alongside Hermes and
OpenHands agents, **without building a new store or forking the source of truth**. This unit is the
"multiplayer triage board" answer under the repo's own durability rules (ADR 0005).

**Problem frame (why this is not a greenfield tool):** `board.py` already assigns pipeline columns
(`todo`/`in_progress`/`in_review`/`retry`/`blocked`/`done`); `activity_server.py` already serves
`/api/activity/since?ts=` delta-polling and `/api/agents`; `last_run.json` already carries the
actor (`agent`), the state (`status`), and the reason (`rejection`). The thariqs
`18-editor-triage-board.html` is a **View + Mobility** layer only: four columns
(`now`/`next`/`later`/`cut`), drag-to-move, and a "Copy as markdown" export. It cannot do
**Transactions** (two people dragging = lost update) or **Ground truth** (exported markdown is not
a store). So the honest design keeps beads + git as the shared write path and uses HTML as a
decision surface — exactly the "the board is a view, not the task DB" property ADR 0005 already
locked.

**Requirements:**

- One shared **tracking contract** every participant follows (human + Hermes + OpenHands): the
  board HTML is read-only; the write path is `bd` (claim/update/close). No agent writes the board.
- A card model mapping the existing `last_run.json` fields onto the four triage lanes, reusing
  `board.py:assign_column` where it already expresses intent.
- A **`bd`-command export** (not markdown) from a 18-style `triage.html`: dragging a card emits
  `bd update <id> --status <lane>` / `--blocker`, so the UI stays an input that an apply-gate can
  run against the beads store.
- **Realtime = delta-polling**, the mechanism `activity_server.py` already ships and ADR 0005
  already accepts (`no realtime guarantees: CI every 5 min, JS polls every 15 s`). "Live" for this
  stack means *poll the JSON, not SSE webpush*.

**Dependencies:** U1 (template + build contract), `board.py`/`activity_server.py` (existing, unchanged).

**Files:**

- `docs/templates/slice-tracking-contract.md` — the shared contract (read-only board, `bd` write
  path, four-lane mapping, apply-gate rule). This is the day-1 zero-code artifact every collaborator
  gets pointed at.
- `docs/templates/triage-board.html` — self-contained 18-style board: reads a `board.json` shape,
  four draggable lanes, and a **"Copy as `bd` commands"** export button; no build step, no external
  services. Reuses the repo's dark editorial register.
- `scripts/build_board_json.py` — zero-dep exporter: `board.py` + `last_run.json` +
  `.beads/interactions.jsonl` → `data/board.json` (the wire format the template consumes).
- `tests/test_slice_tracking.py` — focused tests for the `bd`-command export builder and the
  lane-mapping function.

**Approach:**

1. Extract the four-lane mapping (`now`/`next`/`later`/`cut`) as a pure function over the existing
   `last_run.json` statuses + beads issue states, so the mapping is testable without a browser.
2. Build the 18-style board as a **view over `data/board.json`**, pollable with the same vanilla-fetch
   delta pattern `activity_server.py` already uses. Plan-first feasibility note: the successor can
   emit `bd ...` commands and test the exact command strings, keeping the HTML ultimately
   hand-verifiable at desktop/mobile widths.
3. The **apply-gate** (`bd` commands from the board) is *not* wired into the pipeline; the export
   button only prints commands for a human or an agent to run — preserving ADR 0005's
   "no mutable task store, no drift surface."

**Execution note:** This is deliberately a *reader + exporter*, not a collaboration server. The
"multiplayer" feel comes from everyone watching the same `board.json` + git refs, and everyone
moving work through `bd`. If a later need for true bidirectional sync appears, that is a separate
ADR — not this unit.

**Test scenarios:**

- Lane mapping: `last_run.json` `in_progress` → `now`; `in_review`/`retry`/`blocked` → `next`;
  `done` → `done` (not dragged); unknown status → `cut` with the original status preserved.
- `bd`-commands export: a card dragged to `cut` emits `bd update <id> --status canceled` (exact
  string, no markdown header noise).
- `data/board.json` from the exporter has no external URLs and every card has `{id, title, lane,
  actor, reason}`.
- The triage template opens at desktop and mobile with no horizontal overflow and keyboard-operable
  drag fallback.

### U6. Daily workflow doc (`docs/templates/daily-loop.md`)

**Goal:** Turn the tracking contract into a *start-of-day ritual* the agent executes and the human
audits, so the board + `bd` become the operating rhythm rather than a monitor.

**Problem frame (why this is a doc, not a tool):** The inner loop (Prime → Plan → Implement →
Validate → Review → PR) is already the loop the repo runs. What has no home yet is the *decision
gate at the start*: pulling open slices, refreshing the projection, picking the single Next slice,
and ending the cycle with a bead write. The plan's own "Session Completion" (in `AGENTS.md`) ends
with handoff; it never says *what to do first* next session. U6 is that missing "first".

**Requirements:**

- A plain `daily-loop.md` under `docs/templates/` the agent reads at cycle start.
- Exactly three commands, no new tooling: `bd ready` (pull open slices), `python3
  scripts/build_board_json.py` (refresh the projection), open `triage-board.html` (see
  Now/Next/Later/Cut).
- A **Plan step** that enforces the repo's own single-concern rule: pick *one* slice from
  `next`/`later` → `now`, state its exit checks aloud before implementing.
- An explicit **cycle-end rule** (the tracking contract): the cycle ends with a bead write
  (`bd close <id>` or `bd update <id> --status open`) for the worked slice, *before* the next
  cycle starts.
- A "define done here" checklist so the human can audit whether today's run followed the loop.

**Dependencies:** U5 tracking contract (the board + `bd`-only write path this doc operationalizes).

**Files:**

- `docs/templates/daily-loop.md` — the unit's deliverable. Markdown by design (loaded by the agent
  at cycle start; no render step required). The full tracking contract itself stays in
  `slice-tracking-contract.md`; this doc is the *ritual* that consumes it.

**Approach:**

1. Author `daily-loop.md` as a step-by-step ritual: Prime → Plan → Implement → Validate → Review →
   PR, with the three commands in Prime, the single-concern rule in Plan, and the cycle-end bead
   write stated as the closing action.
2. Cross-reference `slice-tracking-contract.md` (contract) and `triage-board.html` (view) instead
   of duplicating their content.
3. Keep it human-auditable: a short "did we follow the loop?" checklist at the end, so the doc is
   both an instruction file and a self-check. No new script, no new test — the workflow is
   validated by the same bead writes the contract already measures.

**Execution note:** This unit deliberately ships no code. It is the day-1 operational glue that
makes U5's board + `bd` write path something a session actually runs. A future "Tier-2 daily
brief" (a generated HTML page that states today's slice, yesterday's close rate, open blockers) is
a separate unit that builds on U2/U3 — not this one.

**Test scenarios (manual, doc-gated):**

- A fresh session reads `daily-loop.md`, runs the three Prime commands, and picks one Next slice.
- The cycle ends with a bead write for the worked slice (verifiable via `bd show <id>` state).
- The human can tell, from the doc's checklist, whether the session followed the loop.

### U7. Cloud-lane dispatch + trust envelope (event → slice → PR, without the Mac)

**Goal:** Break the loop's dependence on the Mac being open, one 16GB device carrying the full
pipeline, and a human rubber-stamping every PR. Move the issue→PR pipeline to the cloud so it runs
on *events*, fans out across machines, and auto-applies routine slices — pulling the human in only
when automated checks miss or the risk is high.

**Problem frame (why the current loop is bound to one device):** Hermes runs on the Mac via
FirstMate+Orca; if the Mac is closed, the pipeline is closed. Beads is designed to sync across
machines (`bd dolt push/pull`), and the board is already a cross-device view. So the transport for
distributed slicing exists; what does not exist is an *event trigger* that converts "a GitHub issue
appears" into "an OpenHands cloud conversation claims the slice, implements it, and opens a PR"
without the Mac holding state. And the apply-gate (Decision 8 / U5) is currently deferred to "print
commands for a human" — which is exactly the "too much human in the loop" friction being removed
here.

**Requirements:**

- An **event trigger** (GitHub Action, label like `cloud`) that converts an issue to a bead and
  starts an OpenHands cloud conversation using `$OPENHANDS_API_KEY` (available in this workspace)
  to implement the slice and open a PR.
- A **trust envelope** defining which slices auto-apply vs. need human approval: routine, low-risk
  slices (tests, constants, docs, zero-diff-API refactors) auto-apply; human is pulled in only when
  checks fail or the change is high-risk (auth, data, public API, curriculum).
- A **distribution boundary** splitting work between the cloud (pipeline: issue intake, PRD/SPEC
  HTML, slicing, routine implementation, validation, PR) and the Mac (curriculum: scoring, two-judge
  review, Engram consolidation). Same `bd` store; different hardware per purpose.
- The existing **PR → human** step becomes an *exception filter* (auto-apply within the envelope),
  not a rubber stamp.

**Dependencies:** U5 tracking contract (the `bd`-only write path the cloud lane uses), U6 daily
loop (the ritual the cloud lane automates), existing `pr_creator.py` (PR creation is reused).

**Files:**

- `.github/workflows/cloud-lane.yml` (or equivalent) — event trigger → bead → cloud conversation.
- `scripts/` dispatch helper that opens the cloud conversation with the slice + trust-envelope
  evaluation.

**Approach:**

1. Implement the trigger + dispatch helper that, on a labeled issue, claims the bead
   (`bd update <id> --claim`), starts the cloud conversation, and watches for a PR.
2. Encode the trust envelope as a small classifier (reuse `triage_classifier.py` where possible) so
   the trigger can decide auto-apply vs. human-approve.
3. Keep the school's pedagogy unscaled: cloud pipeline ships; the curriculum (score/two-judge/Engram)
   stays on the Mac until the cloud pipeline has earned it.

**Execution note:** The school's training loop is a different resource profile from shipping. Do not
port the whole school to the cloud in one step. This unit moves the *pipeline*; the curriculum
migration is a separate follow-up.

**Test scenarios:**

- A label on a synthetic issue produces a bead claim and one cloud conversation (no Mac running).
- A routine slice inside the envelope opens a PR with no human approval; a high-risk slice pauses
  for approval.
- `bd dolt push/pull` keeps the Mac and cloud views of the same board consistent (no drift).

### U8. Curated vault allowlist + live read (Layer 3 → Layer 0, privacy-safe)

**Goal:** Give agents a *context seam* to the KnowledgeCore (Obsidian) notes
that is safe to version and share — without ever dragging the personal Obsidian
vault into the repo. This is the Layer 3 (Obsidian archival) → Layer 0
(CocoIndex vault) bridge the two-loop design needs for the "Prime: context
gathering" inner-loop step.

**Part A — curated allowlist (landed):** `data/vault/` is the safe, versioned
seam. Policy confines it to hand-picked, non-personal notes.

**Part B — live read (landed):** the container reaches the *personal but
curated* Obsidian vault directly over a **Tailscale userspace + SOCKS5**
tunnel, through the Local REST API plugin (`:27124`). `scripts/obsidian_client.py`
is a read-only, folder-confined client; `context_orchestrator` adds a
`[Live Vault]` probe whenever `OBSIDIAN_API_KEY` is set.

**Problem frame:** The outer loop's PRD/SPEC phase and the inner loop's Prime
phase both say "gather context from the vault." But the vault is a personal
Obsidian store with private data (journal, finance, health). Live-tunneling it
into this container has so far required either a relay host, root/TUN in a
sandbox, or exposing private data to the internet — all unsatisfying or unsafe.
A **curated allowlist** solves this: the repo treats `data/vault/` as the
knowledge vault, and a policy confines it to hand-picked, non-personal notes.

**Requirements:**

- `config/vault_allowlist.yaml` — the ONLY glob patterns agents/indexers may read.
- `data/vault/.vault_manifest.json` + `data/vault/README.md` — the tracked contract.
- `scripts/check_vault_allowlist.py` — enforces CI + runtime that only
  allowlisted files live in `data/vault/`.
- `context_orchestrator._vault_allowlist_violation` — runtime fail-closed guard:
  skips CocoIndex search of a dirty curated vault so private data never reaches
  prompts.
- Note content (`school/`, `docs/`, `anchors/`, `roles/`) stays **untracked**
  (gitignored) — personal choice, never in git.
- `scripts/obsidian_client.py` — read-only Live Vault client over the Tailscale
  SOCKS5 proxy; **folder-confined** (agents read `01-Projects`, `02-Agents`,
  `03-Skills`, `04-Reference`, root indexes; personal `00-Inbox`, `05-Daily`,
  `06-Archive` and `Brandon Career` are hard-blocked).
- `context_orchestrator` `[Live Vault]` probe — adds live search snippets when
  `OBSIDIAN_API_KEY` is set; fails open otherwise.

**Dependencies:** the Local REST API obsidian plugin (on the Mac, bound to
`0.0.0.0`), and a reachable Tailscale userspace node with a SOCKS5 proxy
(`localhost:1080`). The client itself is **pure stdlib** — no pip installs —
so it works in the ephemeral sandbox with zero dependency risk.

**Files:**

- `config/vault_allowlist.yaml`
- `scripts/check_vault_allowlist.py`
- `tests/test_vault_allowlist.py`
- `data/vault/.vault_manifest.json`, `data/vault/README.md`
- `context_orchestrator.py` (runtime guard + `[Live Vault]` probe)
- `scripts/obsidian_client.py` (read-only live vault client)
- `tests/test_obsidian_client.py` (path-confidence tests), `tests/test_context_orchestrator.py` (`TestLiveVaultIntegration`)
- `.github/workflows/ci.yml` (privacy guard step)

**Approach:**

1. Declare allowed glob patterns; anyone (agent or human) adds a note only under
   an allowed path, or extends the allowlist deliberately.
2. CI runs `check_vault_allowlist.py --strict` — a fresh checkout (which has only
   the contract files, no notes) passes; any stray personal file fails.
3. Runtime guard in `enrich_prompt` skips the CocoIndex probe if the curated
   vault is dirty.
4. Docs (this plan, `data/vault/README.md`) teach the boundary: shared contract,
   private notes stay on the machine.
5. **Live reads:** `scripts/obsidian_client.py` speaks to the Obsidian Local
   REST API through the tailscale SOCKS5 proxy. It is read-only and
   folder-confined; personal folders are hard-blocked at the client boundary.
6. **Optional probe:** `context_orchestrator.enrich_prompt` adds `[Live Vault]`
   search snippets when `OBSIDIAN_API_KEY` is set. No key → no probe (fail
   open, zero config burden elsewhere).

**Execution note:** The allowlist is the *security boundary*, not the content.
It does not write notes; it confines what agents may read. Content curation is a
human act (copy a non-personal note into `data/vault/school/`, done). The live
client is the *manual* (read-only) equivalent, giving agents the full curated
vault without copying.

**Test scenarios:**

- `tests/test_vault_allowlist.py` green: glob matching (single-`*` vs `**`),
  allowlist load, runtime guard clean/dirty, repo-root-not-guarded.
- CI step passes on a fresh checkout (contract only) and fails when a
  `private/` file is added.
- `enrich_prompt` still returns CocoIndex context for the repo-root vault
  (no regression for code-context indexing).
- `tests/test_obsidian_client.py` green: path-safety confinement — allowed
  areas pass, personal folders hard-blocked, no prefix substring leaks, and
  **search results are filtered per-result** (personal snippets never surface).
- `tests/test_context_orchestrator.py::TestLiveVaultIntegration` green:
  probe off by default, on when key set, formats results, fails open on empty.
- `scripts/obsidian_client.py doctor` (exit 0) when the tailnet bridge is up.

## Open questions

1. **Which docs get migrated first?** Candidate: the *next* SPEC produced under the new policy
   (migrate-on-write) rather than back-migrating existing planning docs. Recommend migrate-on-write.
2. **Do we install the Plannotator skills into `.agents/skills/`?** The authoring contract is what
   this plan mandates; the actual skill import is a separate security/context review. Recommend:
   keep the contract in `docs/templates/` and import skills only if the maintenance cost is justified.
3. **Should `tot` become a repo-integrated step or stay a human action?** Recommend human-only for
   now (credentials stay machine-local; no CI network dependency).
4. **Tier-2 regeneration triggers:** on-event (school-loop commit) vs on-demand. The board
   dashboards already regenerate on schedule; module maps may be on-demand.
5. **Does the guided-review format replace or supplement the CTO/COO two-judge review?** Recommend
   supplement — the guide is orientation (author-side), the two-judge review stays the adversarial
   gate (critic-side). The five judges read the same portable HTML.
6. **Multiplayer board scope:** does the triage board need true bidirectional drag-to-`bd` write
   (a real apply-gate), or is print-commands-for-a-human enough for now? Recommend print-commands;
   an apply-gate is a separate ADR because it introduces a write surface against the durable store.

## Validation plan

- **U1:** template files present under `docs/templates/`; `guide.schema.json` validator test green;
  each `.html` template passes the external-resource grep.
- **U2:** `scripts/html_module_map.py` output opens locally with no network; fixture test green.
- **U3:** `scripts/html_render_md.py` round-trips a fixture SPEC; heading-preservation test green.
- **U4:** `scripts/check_html_artifact.py` gates a self-contained fixture and rejects an external-ref
  fixture; test green.
- **U5:** `tests/test_slice_tracking.py` green (lane mapping + `bd`-command export strings);
  `scripts/build_board_json.py` produces `data/board.json` with no external URLs; the triage
  template opens desktop + mobile without horizontal overflow.
- **U6:** `docs/templates/daily-loop.md` present, references the tracking contract and the board,
  and contains the three Prime commands + the single-concern Plan rule + the cycle-end bead write.
- **U7:** a label on a synthetic issue produces a bead claim + one cloud conversation (no Mac
  running); a routine slice auto-opens a PR, a high-risk slice pauses; `bd dolt push/pull` keeps
  Mac + cloud board views consistent (no drift).
- **U8:** `tests/test_vault_allowlist.py` green; `scripts/check_vault_allowlist.py --strict` passes
  on a fresh checkout (contract only) and fails on a `private/` file; `enrich_prompt` still returns
  CocoIndex context for the repo-root vault (no regression). Live reads validated:
  `tests/test_obsidian_client.py` green (path safety), `TestLiveVaultIntegration` green
  (probe gating/formatting), and `scripts/obsidian_client.py bootstrap` returns live vault docs
  when the tailnet bridge is up.
- **Manual:** open one Tier-1 (prototype) and one Tier-3 (SPEC render) artifact at desktop and
  mobile widths; confirm no horizontal overflow and keyboard-operable controls.

## Execution policy

- This plan does **not** stage, commit, or push anything. It is a proposed boundary contract.
- Implementation units land one at a time with their focused tests, per the U1–U6 product-slices
  convention: no big-bang rewrite, no `git add -A`.
- Follow the non-interactive file-op forms (`cp -f`, `mv -f`, `rm -rf`, `apt-get -y`) anywhere the
  plan's later steps touch files.
- Beads (`bd`) is the tracker for follow-up work created from this plan's "Open questions"; this
  file is the plan, not the tracker.