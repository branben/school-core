---
title: "Activity dashboard: embedded-data gap"
type: epic
status: active
date: 2026-10-20
scope: school-core P3
upstream: school-core-tkqn
tags: [school-core, dashboard, html, data-embed, lint]
---

# Activity dashboard: embedded-data gap

> Canonical path: `docs/epics/2026-10-20-activity-dashboard-embed-gap.md`.
> This is the initiative root. Slices and PRDs cite this file as their source.

## One-line pitch

`src/tools/generate_activity_dashboard.py` builds a JSON payload and never embeds it. The emitted HTML is not self-contained. This epic closes the gap between what the docstring promises and what the page actually ships.

## Why this matters

A dashboard that claims "embedded data baked in" but requires a network fetch to show anything is the same failure mode as a mock test that proves nothing: the artifact looks complete until you look at what it actually carries. The fix is small; the trust restored is large.

## Timeline

| When | Milestone | Outcome |
|------|-----------|---------|
| 2026-10-20 | Discovery — F841 lint backlog cleanup (`8e826e2`) | Found unused `data = json.dumps(...)` in `generate_activity_dashboard.py`; kept with `# noqa: F841` and a comment rather than delete |
| 2026-10-20 | Bead created — `school-core-tkqn` | Acceptance criteria: dashboard HTML is genuinely self-contained, verified offline |
| Next | Inner loop — Prime → Plan → Implement → Validate → Review → PR | One slice: inject payload into HTML or update docstring to match reality |

## Shipped / slipped

| Shipped | Slipped |
|---------|---------|
| Bead `tkqn` opened with measurable acceptance criteria | Fix not yet implemented — the `# noqa: F841` is a known-debt marker, not a resolution |
| Docstring audit complete — gap confirmed | No rendered HTML artifact for the dashboard yet |

## Blockers

| Blocker | Owner | Gate |
|---------|-------|------|
| No inner-loop run yet — slice needs to be pulled into Prime | `student-coder` / `student-reviewer` | `bd claim tkqn` → inner loop |

## Next moves

1. Run the inner loop on `tkqn`: Prime → Plan → Implement → Validate → Review → PR.
2. After merge, render the updated dashboard HTML through `docs/site/` and publish via `tot` as the verified artifact.
