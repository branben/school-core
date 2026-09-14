# Phase → form → tool mapping

> Canonical table from the SDLC plan
> (`docs/plans/2026-09-13-001-feat-effective-html-sdlc-plan.md`, section
> "Phase → form → tool mapping"). Cites tiers per
> [TIER.md](TIER.md). Later phases in this repo cite THIS table as canonical.

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

## How to read the table

- **Proper documentation type** names the tier (see `TIER.md`) and the form
  that tier takes for that phase.
- **Authoring skill** is the authoring contract to follow when an HTML
  artifact is produced: `html-wireframe`, `html-prototype`, `html-plan`,
  `html-diagram`, or the `html` router with `design-artifact` / risk-map
  callouts. "—" means the phase stays markdown-only.
- **Canonical source** is where truth lives. "none" (Tier 1) means the
  throwaway file is not canonical and the ruling decision lives elsewhere.
- **Transport** is how the artifact reaches a human reader: purely local, in
  `docs/site`, published via `tot` (living URL for the branch tip + `@hash`
  URL for a frozen snapshot), or shared as an encrypted `guides.show` link.

## Build + verify contract

All HTML artifacts in any row above must satisfy the one-file contract:

- ONE self-contained `.html`, no build step, semantic landmarks,
  keyboard-operable, no external services/URLs, no horizontal overflow at
  mobile width.

See `TIER.md` → "Build + verify contract" for the full wording.

## Template map

Every phase in the table above has a template available in this directory.
Templates follow the **Effective HTML** guide (the `effective-html` skills +
`docs/sdlc/context-skills.md`): a single self-contained HTML artifact that
moves the reader's judgment, sharing one CSS register.

| Phase | Template | Tier | Skill pattern | Canonical source |
|---|---|---|---|---|
| Pre-PRD exploration | `exploration-wireframe.html` | 1 | `html-wireframe` (2-3 directions, grayscale) | none |
| PRD alternatives (GF/BF) | `prd-alternatives.html` | 1 | `html-wireframe` (side-by-side decision) | none |
| PRD | `prd-pitch.html` | 3 | pitch-doc (demo → pitch → objections) | `.md` in git |
| Ticket | `ticket-risk-map.html` | 1/2 | risk-map view over beads data | beads (data) |
| SPEC | `spec-implementation-plan.html` | 3 | `html-plan` | `.md` in git |
| Prime | `prime-module-map.html` | 2 | `html-diagram` (SVG boxes/arrows) | repo data (generator) |
| Plan | `plan-tweakable.html` | 1/3 | `html-plan` (milestones + data-flow + risks) | `.md` (or ephemeral) |
| Validate | `validate-prototype.html` | 1 | `html-prototype` (click-through + boundary) | none |
| Review | `guided-review-guide.schema.json` | 3 | `plannotator-guide` | `guide.json` (validated) |
| Review | `review_guide_example.json` | 3 | `plannotator-guide` example | `guide.json` (validated) |
| PR → human | `pr-writeup.html` | 3 | PR-writeup (report register) | `.md` in git |

**Implement** intentionally stays `.md` (dev log / unknowns-09 pattern) — see
docs/templates/TIER.md.
