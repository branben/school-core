# ADR 0005: Durable Cloud Board

**Status:** Accepted  
**Date:** 2026-07-16

## Context

The user wanted the task-board view to survive power loss, dead disk, or a
new computer. A prior plan considered a persisted local `TaskStore` but
rejected it: the task lifecycle is ephemeral (middle columns are empty 99%
of the time), and a second source of truth would drift from GitHub.

The board already existed as a self-contained HTML kanban generated from
GitHub issue state. The question was how to make its state and execution
survive outside a single laptop session.

## Decision

We split durability into three independent axes, each handled by a
different mechanism:

| Axis | Mechanism | Survives |
|------|-----------|----------|
| **State in Git** | `data/` (`issues_cache.json`, `processed_issues.json`, `last_run.json`) committed and pushed | Disk death, rebuild → `git clone` |
| **Execution in CI** | GitHub Actions cron runner (`school-loop.yml`, every 5 min) | Laptop loss, no local Python needed |
| **View hosted** | `lavish-axi share` → ht-ml.app public URL | Local server down, access from anywhere |

Key design properties:

- The board reflects only **GitHub issue state + processed + last_run**.
  There is no mutable task store — no drift surface.
- `last_run.json` is **append-only**; it captures a log of every bridge
  cycle without overwriting history.
- The JS layer uses **vanilla `fetch` polling** (no HTMX, no framework).
- **Lavish** serves a dual role: it is both the review surface (local
  `lavish-axi board.html`) and the publish target (`lavish-axi share` →
  ht-ml.app).

## Consequences

- **Positive:** The board survives laptop death / disk loss / full rebuild.
  Clone the repo and enable Actions — the board regenerates and publishes
  automatically.
- **Risk — third-party vendor:** ht-ml.app is not part of Lavish proper.
  If it disappears, the published URL breaks. Mitigation: re-share from a
  regenerated `board.html` (`lavish-axi share board.html`). Git-state + CI
  remain the durable core.
- **Public surface:** The board shows the issue pipeline, which is sourced
  from public GitHub data. This was explicitly approved. If sensitive data
  ever enters the repo, `lavish-axi share --password` provides a private
  option.
- **No realtime guarantees:** CI runs every 5 minutes, JS polls every 15
  seconds. For sub-minute / rare runs this is adequate (YAGNI on realtime).

## Rejected Alternatives

| Alternative | Reason |
|-------------|--------|
| Persisted local `TaskStore` | Ephemeral lifecycle + drifts from GitHub; second source of truth |
| HTMX vendored lib | Unnecessary; vanilla `fetch` + `innerHTML` suffices; violates zero-dep ethos |
| GitHub Pages | Redundant once ht-ml.app publishes; Lavish share is simpler |
| Managed cloud DB / queue | Overkill for a single-instance polling loop; vendor lock-in |

## Bugs Caught During Build

1. **Key-contract mismatch between static render and JS poll** — The
   server-rendered HTML and the `/api/board.json` poll endpoint used
   different card shapes, causing flicker after the first poll interval.
   Fixed by unifying on a canonical card shape: `{n, t, dom, diff, a, s}`.

2. **`github_fetcher.fetch_issues` filters out all non-ready-for-agent
   issues** — When run against `school-core` itself (where no real
   "ready-for-agent" labels existed), the fetcher returned zero issues.
   Fixed by populating the cache via raw `gh issue list --state open`
   instead of relying on label-based filtering.
