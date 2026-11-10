# PR body template — ending-PR phase (Tier-3 PR-writeup render)

Canonical source for the GitHub PR body written at the end of the inner loop.
This merges three proven sources:

- **The ending-PR writeup** demonstrated in
  `cheaper-inference/OmniRoute#15983` (Problem / Solution / Migration path /
  Contract pinning / Risk / AI Disclosure / Checklist) — the *engineering
  narrative* a reviewer needs for a real change.
- **The target repo's PR template** (OmniRoute's
  `.github/pull_request_template.md`: Summary / Related Issues / Target Branch /
  Validation / Tests Added / Coverage Notes / Reviewer Notes) — the
  *operational* fields the host repo's tooling and CI expect.
- **The school-core automation evidence chain** (`src/bridge/pr_creator.py`
  `build_pr_body`: Domain / Task Output / Acceptance Evidence / Source chain) —
  the *facts the pipeline already knows and must not lose*.

The GitHub PR body is the *transport* of the canonical `pr-writeup.md`
(phase-map row 21). This template is that transport's shape.

---

## Layout principle: lean core + collapsible evidence

The reader is assumed to be **a maintainer like you — not a domain expert in
the target codebase.** Everything a non-expert needs to triage the PR lives in
a short always-visible core. The deep engineering narrative (for the domain
experts who will actually review the diff) lives inside `<details>` blocks
that stay collapsed by default.

GitHub renders `<details><summary>…</summary>…</details>` as a collapsible
section in PR bodies. Nested details are allowed; the renderer emits them as
one flat group per section.

```markdown
## Summary

<1-3 sentences: what this PR does, user-facing. NOT a repeat of the title.
If there is a "what's in it for the operator/user" angle, put it here first.

[auto] first sentence = PR title + Close/Implements line; agent may expand.]

<details>
<summary><b>Why it matters (problem, context, decisions)</b></summary>

- **Problem / Why:** <what gap this addresses, why it matters, concrete
  failing behavior. Conditional: omitted if trivial (typo/chore/docs).>
- **Approach taken:** <why this option over alternatives. Call out feature
  flags / sidecars / parallel paths that keep legacy behavior intact.>
- **Decisions:** D1–D<n> <from source.json, [auto]>

</details>

## Related Issues & Bead

- Closes #<issue>
- Related to #<...>          [conditional] only if cross-referenced
- Bead `school-core-XXXX`    [auto] from source.json / review evidence

## Target Branch / Base

- Base: `main` for school-core; `release/v3.8.x` (highest open release) for
  OmniRoute target repos.        [auto] derived from git base ref

<details>
<summary><b>Files changed</b> <i>(N files, +A / −D)</i></summary>

- `path/to/file` — <1-line purpose>        [auto] derived from the diff

</details>

<details>
<summary><b>Testing / Validation</b></summary>

- Change type: <provider / routing / UI / i18n / CLI / DB / build-deploy / other>
  [conditional] OTR target; omit for school-core
- Automated: <suite names + counts>        [auto]
- Manual / integration: <curl checks, live probes>  [agent]
- Known gaps: <flaky/timing tests not masked>       [agent]
- CI: <which lanes pass/fail and why, explicitly>   [auto]

</details>

<details>
<summary><b>For the domain expert</b></summary>

- **Migration path / Compatibility:** <if a stepping stone: what remains to
  fully close the issue, what stays on the legacy path meanwhile.>
- **Contract / Scope notes:** <per-surface contract pinning when a protocol
  surface changes; what does NOT change; feature-flag names + defaults.
  Conditional: omit when no protocol/flag surface affected.>
- **Special things to note:** <stateless vs stateful handshakes, env vars
  required at runtime, restart needed, cache/expiry semantics.>
- **Coverage Notes:** <if production code changed: which tests cover it; if
  coverage moved down in a touched file, why + follow-up. [conditional] OTR
  target, or repo with a coverage gate.>
- **Reviewer Notes:** <areas needing extra scrutiny: risky diffs, concurrency,
  migrations, flag interplay, manual validation to repeat. [conditional] OTR
  target; school-core carries this under "Special things".>

</details>

## Risk

- Level: Low / Medium / High          [auto] from trust envelope, fallback agent
- Back out: <how to disable/roll back — concrete command or flag>

## AI Disclosure

AI-assisted. <honest account of what the agent did, what was plan/generate vs
author/verified, and any independent review applied.>

<details>
<summary><b>Checklist</b></summary>

- [ ] Bead ID present (outer loop can trace this PR to an issue)   [auto]
- [ ] Tests added or updated for production-code changes            [auto]
- [ ] Risk acknowledged with a back-out plan                        [auto]

</details>
```

### What stays visible vs collapsed

| Always visible (triage for a non-expert) | Collapsed (on demand) |
|---|---|
| `Summary` | `Why it matters` (problem/approach/decisions) |
| `Related Issues & Bead` | `Files changed` |
| `Target Branch / Base` | `Testing / Validation` |
| `Risk` + back-out | `For the domain expert` (migration/contract/notes/coverage/reviewer) |
| `AI Disclosure` | `Checklist` |

A non-expert maintainer scans five short sections and is done; the domain
expert opens the four `<details>` blocks to review the evidence. The heavy
16-section flat wall is gone.

---

## Auto vs agent-written vs conditional

Sections are marked per data source:

| Marker | Meaning |
|---|---|
| `[auto]` | The renderer fills this from data it already holds (issue, source.json, review, verify, git base). Never a guess. |
| `[agent]` / no marker | The implementing agent writes it from its own work — not fabricated from pipeline data. |
| `[conditional]` | Section appears only when the trigger condition holds (trivial change, migration step, protocol surface, OTR target, coverage gate, mechanical-copy review). |

Guiding rule (from `build_pr_body`'s comments): **never overstate evidence.**
A section whose data is absent is omitted, never synthesized as a blank pass.

## Worked example — an OmniRoute MCP change

The reference PR `cheaper-inference/OmniRoute#15983` is the validating
instance of this shape for a protocol-surface change into an OTR repo. Its
existing body maps onto the template:

- `## Problem` → `Why it matters` → Problem bullet (missing 2026-07-28 spec surface)
- `## Solution` → `Why it matters` → Approach bullet (opt-in v2 sidecar behind `OMNIROUTE_MCP_V2`)
- `## Migration path (to fully close #17131)` → `For the domain expert` → Migration
- `## Contract pinning (per-surface)` → `For the domain expert` → Contract
- `## Special things to note` → `For the domain expert` → Special things
- `## What changed` → `Files changed` `<details>` (+ guided-review pointer)
- `## Testing` → `Testing / Validation` `<details>` (92/92, manual curl, known gaps)
- `## Risk` → `Risk` (Low, back-out = flag off)
- `## AI Disclosure` → `AI Disclosure` (unchanged)
- `## Review` → folded into `AI Disclosure` (the "not a mechanical translation" claim)
- `## Checklist` → `Checklist` `<details>`

So the template above is the generalization; #15983 is the validating instance
for a protocol-surface change into an OTR repo. A trivial school-core scan PR
renders an even shorter body: Summary, Related-Issues-and-Bead, Target-Branch,
Risk, AI Disclosure — nothing else survives.

## Where it lands

- Renderer: `build_pr_body` in `src/bridge/pr_creator.py` now emits this
  shape (lean core + `<details>` collapse). All pre-existing evidence tokens
  are preserved verbatim (regression-guarded in
  `tests/test_pr_body_evidence.py`, which also asserts the new layout in
  `TestPrBodyTemplateLayout`).
- Canonical writeup: `scripts/pr_writeup_gen.py` `build_pr_writeup_md` remains
  the `.md`/`html` report register; the GitHub body is this transport.