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

`src/tools/generate_activity_dashboard.py` built a JSON payload and never embedded it. The emitted HTML was not self-contained. This epic closed the gap between what the docstring promised and what the page actually shipped.

## Why this matters

A dashboard that claims "embedded data baked in" but requires a network fetch to show anything is the same failure mode as a mock test that proves nothing: the artifact looks complete until you look at what it actually carries. The fix is small; the trust restored is large.

## Timeline

| When | Milestone | Outcome |
|------|-----------|---------|
| 2026-10-20 | Discovery — F841 lint backlog cleanup (`8e826e2`) | Found unused `data = json.dumps(...)` in `generate_activity_dashboard.py`; kept with `# noqa: F841` and a comment rather than delete |
| 2026-10-20 | Bead created — `school-core-tkqn` | Acceptance criteria: dashboard HTML is genuinely self-contained, verified offline |
| 2026-10-31 | Outer loop — Epic → PRD → SPEC → Tickets | Backfilled docs and implemented the fix |
| 2026-10-31 | Inner loop — Implement → Validate → Review → PR | Payload injected as `<script type="application/json" id="__ACTIVITY_DATA__">`; acceptance test passes; `main` at `51adfaa` |

## Shipped / slipped

| Shipped | Slipped / deferred |
|---------|---------|
| Bead `tkqn` opened with measurable acceptance criteria | None — fix is complete |
| PRD + SPEC backfilled and published via `tot` | |
| Payload injection in `generate_html()` — one-line fix | |
| Acceptance test `tests/test_activity_dashboard_embed.py` passes | |
| Docstring updated — embed is now real, not promised | |

## Blockers

| Blocker | Owner | Gate |
|---------|-------|------|
| None — fix merged to `main` | | |

## Next moves

1. Render the updated dashboard HTML through `docs/site/activity_dashboard.html` and publish via `tot` as the verified artifact.
2. Close `school-core-tkqn`.
