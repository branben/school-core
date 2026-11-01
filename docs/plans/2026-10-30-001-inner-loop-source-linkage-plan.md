# Plan — implement the inner-loop source-block linkage

**Date:** 2026-10-30
**Epic:** school-core-3ii (Wayfinder map — video-generation instrumentation)
**Status:** active

## Goal

Make the inner loop's six artifacts (PRIME → PLAN → BUILD → TEST → REVIEW → PR) carry a mandatory `source` block that ties each one back to the ticket/Spec. An empty or missing source block causes the artifact's render to fail-closed, so the inner loop cannot run on an orphaned slice.

## Why this is a small plan, not a framework

The repo already has fail-closed gating everywhere (`review-gate.json` empty → CI red; `APPROVED_ACTORS` empty → no publish; BeadID regex → no path traversal). This plan adds one required field to six artifact templates and one check to the existing review gate — no new service, no parallel store, no registry.

---

## 1. Define the source-block schema

Add a YAML front-matter block to every inner-loop artifact. The block is mandatory; absence or empty required fields cause the render step to exit non-zero.

```yaml
---
bead:       <bd id, e.g. school-core-fip>
spec:       <spec file path> · <milestone id>
decisions:  <comma-separated SPEC accepted-decision ids>
prev:       <prior inner artifact path or "none">
---
```

| Field | Source | Inner-loop phase that introduces it |
|-------|--------|-----------------------------------|
| `bead` | The ticket itself | PRIME |
| `spec` | The ticket's `spec:` field | PRIME |
| `decisions` | The ticket's `accepted:` field | PRIME |
| `prev` | The artifact produced by the prior phase | PLAN onward (PRIME = `none`) |

**Rules:**
- `bead`, `spec`, and `decisions` are required on every inner artifact. `prev` is required on PLAN through PR (PRIME = `none`).
- `decisions` must be a subset of the SPEC's `accepted:` table — the render step cross-checks against the SPEC file.
- `prev` on REVIEW is the **list** of all four prior artifacts (module-map, tweakable-plan, dev-log, prototype), not a single path.

---

## 2. Update the six templates

Each template in `docs/templates/` gets a new front-matter placeholder at the top, plus a render-time check.

| Template | Path | Add |
|----------|------|-----|
| Prime module-map | `prime-module-map.html` | YAML front-matter with `bead`, `spec`, `decisions`, `prev: none` |
| Plan tweakable | `plan-tweakable.html` | Same + `prev: prime-module-map.html` |
| Dev-log | `docs/templates/dev-log.md` | Markdown front-matter with same block + `prev: tweakable-plan.html` |
| Validate prototype | `validate-prototype.html` | Same + `prev: dev-log.md` |
| Review guide | `docs/templates/guided-review-guide.schema.json` | Add required `source: { bead, spec, decisions, artifacts[] }` |
| PR writeup | `pr-writeup.html` | Same + `prev: review-guide.json` |

**Implementation note:** `guide.json` already carries a `source` field for
plannotator's own provenance override (repo, branch, head). Adding chain-of-custody
fields to that same object would collide with the tool's schema. The implemented
solution is a **separate `source.json` sidecar** that `review_gate.py` check #5
reads independently. The template at `docs/templates/source.json` carries the
five required fields: `bead`, `spec`, `decisions`, `artifacts`, `spec_acceptance`.

**Fail-closed rule:** the renderer (whatever generates the `.html` from the template) must exit non-zero if any required field is missing or empty. This mirrors the existing `review-gate.json is empty → CI red` behavior — same pattern, new field.

**`dev-log.md` is a new template** because the inner loop currently has no BUILD artifact at all. It is a plain markdown file (not HTML) with the same front-matter block and a body of deviation entries:

```markdown
---
bead:     school-core-fip
spec:     docs/plans/2026-09-30-002-spec-openai-video-instrumentation.md · M3
decisions: D2, D4
prev:     docs/templates/plan-tweakable.html
---

## Build deviations

### [timestamp] — wrappers in tracing.py
- PLAN said: add wrappers at lines 1927–1928
- Built: wrapped `_materialize_logged_file_input` plus added `_extract_video_output`
- Reason: D3 required file-input routing; the existing code did not expose the helper as a separate callable
```

---

## 3. Wire REVIEW as the accumulation checkpoint

`source.json` (the REVIEW sidecar) cites **all four** prior inner artifacts:

```json
{
  "bead": "school-core-fip",
  "spec": "docs/plans/2026-09-30-002-spec-openai-video-instrumentation.md · M3",
  "decisions": ["D2", "D4"],
  "artifacts": [
    "docs/prime-module-map.html",
    "docs/plan-tweakable.html",
    "docs/dev-log.md",
    "docs/validate-prototype.html"
  ],
  "spec_acceptance": "<verbatim acceptance criteria from SPEC M3>"
}
```

**Review gate check (new, in `scripts/review_gate.py`):**
After the existing diff checks, add a single function that:
1. Reads `source.json` from the PR diff.
2. Verifies `bead`, `spec`, `decisions`, `artifacts`, and `spec_acceptance` are all present and non-empty.
3. For each path in `artifacts`, verifies the file exists in the PR diff or in the base commit (for artifacts produced in prior cycles).
4. Prints `review-gate: source-block complete` or `review-gate: SOURCE-BLOCK MISSING — <which field>` and exits non-zero.

This is one function, ~30 lines, using the same `git()` helper the gate already has. No new dependency.

---

## 4. Update `docs/sdlc/loops.md`

Add a **"Inner-loop source block"** section after the inner-loop phases table. The section says:

> Every inner artifact carries a YAML front-matter `source` block. An empty required field causes the artifact's render to exit non-zero. REVIEW's `source.json` accumulates all four prior artifacts plus the SPEC's acceptance criteria; the review gate checks the chain before passing.

This makes the rule discoverable without reading the templates or the code.

---

## 5. Implementation order (minimal blast radius)

1. `dev-log.md` template (new file — zero blast radius).
2. Add YAML front-matter to `prime-module-map.html`, `plan-tweakable.html`, `validate-prototype.html`, `pr-writeup.html` (edit templates in place — all are currently blank placeholders, no production content).
3. Create `docs/templates/source.json` sidecar template.
4. Add the review-gate source-block check to `scripts/review_gate.py`.
5. Update `docs/sdlc/loops.md`.
6. Validate: run `python scripts/review_gate.py --base origin/main --head HEAD` against PR #162 (or the current PR branch) — must exit non-zero with a clear message until a PR carries all the artifacts, then exit 0 with `review-gate: source-block complete`.

---

## 6. What this does NOT fix

- It does not wire the inner loop into `crew_dispatch.py` or the `school-loop.yml` cron (SCH-32b is a separate bead).
- It does not make the VM stack (`SmolVmRunner`) the live executor (F5 in the audit — out of scope).
- It does not retroactively populate artifacts for the Wayfinder epic — that requires running the inner loop on `school-core-fip`, which is still stale.

---

## Acceptance criteria

1. `prime-module-map.html`, `plan-tweakable.html`, `dev-log.md`, `validate-prototype.html`, `source.json`, and `pr-writeup.html` all require the `source` block.
2. `scripts/review_gate.py` exits non-zero when a PR's `source.json` is missing any required field, and exits 0 with `review-gate: source-block complete` when all fields are present and every cited artifact exists in the PR or base tree.
3. `docs/sdlc/loops.md` documents the rule.
4. A PR that carries a `source.json` with an empty `bead` is red; a PR with all fields filled is green.
