# Three-tier documentation model

> Source pattern: the *density axis* (read → react → edit) and *tier axis*
> (disposable viewport → canonical-HTML → canonical-markdown-render-on-share)
> distilled from the effective-HTML sources studied for the SDLC plan
> (`docs/plans/2026-09-13-001-feat-effective-html-sdlc-plan.md`).

Every documentation artifact in this repo belongs to one of three tiers. The
tier is a property of *what the artifact is for*, not of who opens it.

| Tier | Canonical source | Purpose | Regenerate policy | Authoring tools |
|---|---|---|---|---|
| **Tier 1 — Disposable viewport** | None (throwaway `.html`) | A single decision: explore directions, interview, prototype feel, triage ordering, change-quiz | Render-and-discard; the prompt that made it is the seed, not the file | `html-wireframe`, `html-prototype`, exploration directions |
| **Tier 2 — Canonical HTML** | The `.html` itself (generated from repo data) | Living reference: design system, module map, feature explainer, verify/status dashboards | Regenerate from real tokens/symbols/state; keep generator + artifact in git | `html-diagram`, chart/status dashboards, design-system sheets |
| **Tier 3 — Canonical markdown, HTML render** | Markdown in git (diffable, reviewable, grep-able) | Long-lived institutional docs: PRD, SPEC, PR writeup, status, post-mortem, ADR | Keep `.md` authoritative; generate `.html` as a *view*; publish via `tot` | `html-plan` (source-preserving), pitch-doc, report, PR-writeup patterns |

## Tier 1 — Disposable viewport

```text
Canonical source : none (a throwaway .html for ONE decision)
  Regenerate    : render-and-discard; delete or keep as scratch, never canonicalize
  Tools         : html-wireframe (structural), html-prototype (states/boundary/export),
                  exploration directions (side-by-side compare, at most 2-3 directions)
  Transport     : local
```

A Tier-1 file exists for exactly one decision: *"should X be this or that?"*,
*"how would this feel to use?"*, *"which direction do we go?"*. It is cheap to
produce precisely because it is cheap to throw away. The HTML captures the
decision surface; the conversation/prompt that produced it is the seed, not
the file, so there is no obligation to keep the file in git once the decision
lands anywhere else (a bead, a `.md`, a ticket).

Typical shapes: a wireframe for one interaction, a 2-3 column side-by-side
compare of greenfield vs brownfield directions, a prototype demonstrating
interaction states. Do NOT paste Tier-1 output into the repo as if it were a
Tier-2/3 artifact — if the decision matters long-term, migrate the *outcome*
(not the throwaway) into the right tier.

## Tier 2 — Canonical HTML

```text
Canonical source : the .html itself, generated from repo data
  Regenerate    : regenerate from real tokens/symbols/state; keep generator AND
                  the generated artifact in git
  Tools         : html-diagram, chart/status dashboards, design-system sheets
  Transport     : local, or docs/site
```

Tier 2 is for *living references* whose shape is inherently spatial or
graphical: the module map of the codebase, the request paths between
services, the design system (type scale, color tokens, component inventory),
verify/status dashboards over state that changes as the repo changes.

The authoritative artifact here **is** the HTML. It is generated from real
repo data — symbols, modules, config, issue/bead state, CI results — never
hand-edited prose. The generator and its output both go in git so a reader
can always tell which data produced the picture, and a change to the repo
should produce a regenerated (diffable) HTML rather than a hand-patch.

If the *fact* the diagram describes starts needing to round-trip through
git/Dolt or a CI gate, it stops being Tier 2 and becomes data (→ Tier 3's
"markdown/data stays canonical" rule).

## Tier 3 — Canonical markdown, HTML render

```text
Canonical source : .md in git (diffable, reviewable, grep-able, PR-able)
  Regenerate    : keep .md authoritative; generate .html as a VIEW of it,
                  publish via tot (transport)
  Tools         : html-plan (source-commitment-preserving), pitch-doc, report,
                  PR-writeup patterns
  Transport     : tot — living URL (version-less) + @hash URL (frozen commit)
```

Tier 3 is for *long-lived institutional documentation*: PRDs, SPECs, PR
writeups, status, post-mortems, ADRs. The `.md` in git is the source of
truth, diffable in review, grep-able, and safe in code review. The `.html`
is a generated *view* — easier for a human to skim, to link, to share — and
it must never silently diverge from the `.md`. Every Tier-3 `.html` carries a
header comment naming its canonical `.md` source path (see the templates:
`prd-pitch.html`, `spec-implementation-plan.html`, `pr-writeup.html`).

Tier-3 HTML is published with `tot`: a *living* (version-less) URL that always
points at the branch tip, plus an `@hash` URL that freezes the exact commit so
a PR, review, or post-mortem can cite a snapshot that never changes. The repo
never stores a `tot` credential; `~/.tot` is machine-local. Prefer the file
path for anything security-sensitive — `tot`'s "link is the key" model is for
non-sensitive artifacts only.

When the artifact's job is to present a *decision* to a human, the tier-3
render may add structure (demo → pitch → objections pre-answered) so the
reader reacts to the substance, not to the formatting.

## Mapping rule of thumb

> **HTML is right where the artifact's job is to help a human react, decide,
> or feel.** Markdown stays canonical where the artifact's job is to be
> *data*.

If the point of the document is to move a human — trigger a reaction, support
a decision, give a feeling of how something fits or flows — HTML earns its
keep: layout, ordering, emphasis, side-by-side comparison are services to the
*reader's judgment*. If the point is that machines (or future diffs) must
reliably read, merge, and round-trip the content — tickets (beads and their
fields), verify results, scores — then markdown/structured data stays
canonical, and any HTML is only a *view* over that data (e.g. the ticket
risk-map and triage board are views; beads are the store).

Apply the tier by asking two questions in order:

1. **Who is the reader, and what does the artifact do to them?** React/decide/
   feel → HTML has a role. Data plumbing/review of facts → markdown stays
   canonical.
2. **How long does it live, and how is it regenerated?** One decision →
   Tier 1, render-and-discard. Living reference whose shape is spatial →
   Tier 2, generated from repo data. Institutional doc that must be diffable
   and grep-able forever → Tier 3, `.md` authoritative + HTML render.

## Build + verify contract (shared by ALL tiers)

Every HTML artifact in this repo — Tier 1, 2, or 3 — must satisfy the same
minimal contract before it is shared or merged:

- **ONE self-contained file**: a single `.html`; all CSS and JS inline; no
  served chunks, no multi-file build output, no CDN links.
- **No build step**: opening the file (or `fetch`ing a sibling data file in
  the same repo at most) is all it takes to render. No bundler, no compile.
- **Semantic landmarks**: `header` / `nav` / `main` / `section` /
  `article` / `footer` structure with a single `h1`; regions are
  discoverable by assistive tech and by readers.
- **Keyboard-operable**: all interactions reachable and actionable with the
  keyboard alone (focus management, no pointer-only controls, Escape paths
  where a modal/overlay exists).
- **No external services/URLs**: no `http(s)` references for assets, fonts,
  scripts, or data. Everything the file needs ships inside the file.
- **No horizontal overflow at mobile width**: content must reflow at ~375px
  viewport; tables wrap or scroll within their container, grids collapse,
  `pre`/code blocks wrap.

Verification ritual: open the file at desktop width and at mobile width in a
browser, tab through the interactive elements, and resize the window to check
for horizontal scroll. Where the artifact is machine-checkable (e.g. the
`guide.json` schema), a test in `tests/` guards it — see
`tests/test_guide_schema.py` for the guided-review example.

## Per-tier cheat sheet

| Question | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| Where is truth? | nowhere (throwaway) | the `.html` | the `.md` |
| Keep in git? | only if still useful after the decision | yes: generator + artifact | yes: `.md` (+ `.html` render optional) |
| Regenerate how? | hand-craft per decision | from real repo data | `tot` render of `.md` (or manual view) |
| Typical tools | `html-wireframe`, `html-prototype` | `html-diagram`, dashboards | `html-plan`, pitch-doc, PR-writeup |
| Transport | local | local / docs/site | local / `tot` (living + `@hash`) |