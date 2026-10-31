---
title: "spec: activity dashboard embedded-data fix — implementation plan"
created: 2026-10-31
status: proposed
author: lucas (school-core outer loop)
project: agent-school
prd: docs/plans/2026-10-31-001-activity-dashboard-embed-prd.md
upstream: school-core-tkqn
repo: branben/school-core @ HEAD
tags: [spec, dashboard, html, data-embed, implementation, sdlc]
---

# SPEC — Activity dashboard embedded-data fix

## Decisions locked before any code

| # | Decision | Alternative rejected | Why |
|---|---|---|---|
| D1 | Inject payload into HTML via `<script type="application/json">` | Update docstring to drop the promise | Docstring describes intended behavior; code already builds the data. Fixing the docstring to match the bug is the wrong direction. |
| D2 | Place the `<script>` tag immediately after `<nav>` | Place at end of `<body>` | Early placement lets any inline script read it before rendering; keeps the data close to the root for offline inspection. |
| D3 | Name the global variable `__ACTIVITY_DATA__` | `window.__DATA__` or no global | Triple-underscore prefix signals "internal but inspectable"; matches the existing `__call__` / `__call__` fail-closed naming in the codebase. |
| D4 | One acceptance test: assert payload present in generated HTML | Test the live dashboard via network | Network-dependent test proves nothing about the self-contained claim. |
| D5 | No change to `--watch` mode | Watch mode re-embeds on every regenerate | Watch mode already regenerates the whole HTML from disk; the embedded payload is regenerated with it. No special case needed. |

## Milestone 1 — inject payload in `generate_html()`

**Current code** (`generate_activity_dashboard.py:84`):

```python
data = json.dumps({"agents": agents, "entries": entries}, ensure_ascii=False)  # noqa: F841
```

This line builds the payload but never injects it.

**Fix:** add one line after the `<nav>` block in the returned HTML:

```html
<script type="application/json" id="__ACTIVITY_DATA__">{data}</script>
```

The variable `data` is already in scope inside `generate_html()`. The template
is a single f-string; inserting the JSON is a one-line edit.

**Remove the `# noqa: F841`** — once the variable is consumed, the lint
warning disappears. Update the comment to explain why the payload lives in
the HTML rather than in a separate fetch.

## Milestone 2 — acceptance test

**New test:** `tests/test_activity_dashboard_embed.py`

```python
def test_generated_html_embeds_payload(tmp_path):
    from src.tools.generate_activity_dashboard import generate_html
    agents = [{"id": "test", "name": "Test", "score": 0, "max_score": 0,
               "gate": "easy", "role": "Student", "domains": {},
               "latest_activity": None}]
    entries = []
    html = generate_html(agents, entries)
    assert 'id="__ACTIVITY_DATA__"' in html
    assert '"agents"' in html
    assert '"entries"' in html
```

This test proves the artifact carries its own data — the exact claim the
docstring makes. It runs in milliseconds, needs no network, and never flakes.

## Milestone 3 — update docstring

The module docstring currently says "produces a self-contained HTML file with
all data baked in." After the fix, that sentence is true. Remove the inline
`# noqa: F841` comment (no longer needed) and replace it with a one-line note:

```python
# Payload is injected into the HTML as <script type="application/json"
# id="__ACTIVITY_DATA__"> so the page is genuinely self-contained.
```

## Verification commands

```bash
python3 -m pytest tests/test_activity_dashboard_embed.py -q
python3 src/tools/generate_activity_dashboard.py --output /tmp/activity_test.html
grep -c '__ACTIVITY_DATA__' /tmp/activity_test.html   # must be 1
```

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| JSON payload contains characters that break the HTML | low | `json.dumps(..., ensure_ascii=False)` returns valid JSON; the `<script>` tag is not parsed as HTML when `type="application/json"`. |
| Large payload makes the HTML unwieldy | low | Current payload is small (agents + recent entries). If it grows past 100 KB, revisit — but that is a separate slice. |
| Reviewer wants the payload in a separate `.json` file | low | That is the current behavior, and it is the bug. The PRD explicitly refuses network-dependent dashboards. |

## Out of scope

- Watch mode changes (D5 — no special case needed)
- Live data refresh without page reload
- Dashboard redesign or visual changes
- External data sources
