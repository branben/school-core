---
title: "Activity dashboard: make the HTML actually self-contained"
created: 2026-10-31
status: proposed
author: lucas (school-core outer loop)
project: agent-school
upstream: school-core-tkqn
slice: tkqn
tags: [school-core, dashboard, html, data-embed, lint, brownfield]
measured: 2026-10-31 · generate_activity_dashboard.py read end-to-end
---

# Activity dashboard: make the HTML actually self-contained

## Demo — what a reviewer sees

Today: open `docs/site/activity_dashboard.html` with no network. The page
renders, but it carries no data. The docstring promises "standalone HTML with
embedded data baked in," and the code builds exactly that payload — then drops
it on the floor.

After the fix: open the same file with no network. The page renders with the
same agents, scores, and activity feed it would show with live data. The JSON
payload is present in a `<script type="application/json">` tag the page can
read offline. The artifact matches its promise.

## Pitch — why now, why this

**Problem.** `src/tools/generate_activity_dashboard.py` line 84 builds
`data = json.dumps({"agents": ..., "entries": ...})` and never uses it. The
emitted HTML is not self-contained. A dashboard that claims "embedded data
baked in" but requires a network fetch to show anything is the same failure
mode as a mock test that proves nothing: the artifact looks complete until you
look at what it actually carries.

**Why now.** This is the largest remaining mismatch between docstring and
behavior in `src/tools/`. It was found during F841 lint cleanup and kept with
`# noqa: F841` rather than deleted — which is the right call, because deleting
the line would satisfy the linter while cementing the missing feature.

**Outcome we are buying:**
- Dashboard HTML is genuinely self-contained — open offline, data is present.
- Docstring and behavior agree — no more promises the artifact cannot keep.
- One measurable acceptance test: open the generated page with no network and
  confirm the data is present.

## Scope

| In scope | Out of scope (now) | Explicitly refused |
|---|---|---|
| Inject built payload into emitted HTML | Watch mode / live reload | Refactor to a framework |
| Update docstring only if injection is rejected | External data sources | Network-dependent dashboard |
| One acceptance test for offline data presence | | |

## GF / BF alternatives

- **GF:** Rewrite the dashboard to fetch data from an API endpoint. Rejected:
  the whole point of this artifact is to be standalone. Adding a network
  dependency solves the wrong problem.
- **BF:** Delete the unused `data = json.dumps(...)` line and rewrite the
  docstring to say "HTML requires live data." Rejected: that cements the
  missing feature and turns a one-line fix into a documentation lie.
- **Do nothing:** Leave the `# noqa: F841` as a known-debt marker. Rejected:
  debt markers are only acceptable when there is a plan to pay them. This is
  the plan.

## Objections pre-answered

- **"Isn't this just adding a `<script>` tag?"** Yes. The data is already
  built; it just needs to be written into the HTML. The delta is one line in
  `generate_html()`.
- **"Won't embedding the full JSON bloat the file?"** The payload is small
  (agents + recent entries). The current HTML is already ~30 KB. Adding the
  JSON keeps the file under 50 KB — well within the self-contained artifact
  budget.
- **"Does this break the existing auto-refresh?"** No. The refresh reloads
  the whole page from disk, which includes the embedded data. If we later want
  live updates without a full reload, that is a separate slice.
- **"Why not just fix the docstring?"** Because the docstring describes the
  intended behavior, and the code already builds the data. Fixing the docstring
  to match the bug is the wrong direction — the bug is cheaper to fix than the
  lie is to maintain.

## Done means

- `generate_html()` writes the JSON payload into the emitted HTML.
- Dashboard opens offline with data present.
- One test asserts the payload is embedded in the generated HTML.
- Docstring and behavior agree.
