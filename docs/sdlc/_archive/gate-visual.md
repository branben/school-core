# GATE 7: VISUAL — Visual Verification & Prototyping

> **Kanban ID:** `t_9972fd8f` | **Board:** sdlc-gates | **Status:** in-progress
> **Depends on:** PLAN (`t_85e5b418`) | **Blocks:** AUDIT (`t_1af3e909`)

## Purpose

The VISUAL gate produces shareable, verifiable visual artifacts that make the SDLC workflow **inspectable by humans and agents alike**. It transforms abstract pipeline definitions into concrete wireframes, interactive prototypes, and visual diffs that can be reviewed, annotated, and published.

## Toolchain

| Tool | Role | Skill Wrapper |
|------|------|---------------|
| `effective-html` | Wireframes, prototypes, diagrams | `plannotator-visual-explainer` |
| `Computer Use` | Screenshot capture, visual regression | `computer_use` (deferred tool) |
| `plannotator annotate` | Human review gate with annotations | `plannotator-visual-explainer` |
| `tot` | Publish shareable URLs | CLI: `tot <file>` |

## Workflow

### 1. Generate Wireframe / Prototype

Use the `plannotator-visual-explainer` skill to produce self-contained HTML artifacts:

```bash
# Architecture diagram → Visual explainer path
plannotator annotate prototype.html

# Implementation plan → Plan path (with approval gate)
plannotator annotate plan.html --gate
```

**Content type routing:**
- **Plan/design doc** → Plan path (prescriptive structure: header → summary strip → timeline → architecture → mockups → code → risks → open questions)
- **PR/diff explainer** → PR path (TL;DR → why → file tour → risk map → test plan)
- **Architecture/general** → Visual explainer path (delegates to nicobailon/visual-explainer with Plannotator theme tokens)

### 2. Capture Screenshots (Visual Regression)

Use the Computer Use tool to capture baseline and post-change screenshots:

```
# Navigate to the running app or published tot URL
# Capture screenshot → save to docs/sdlc/screenshots/
# Compare against baseline using pixel diff
```

**Screenshot convention:**
```
docs/sdlc/screenshots/
├── baseline-<feature>-<timestamp>.png
├── post-<feature>-<timestamp>.png
└── diff-<feature>-<timestamp>.png
```

### 3. Create Visual Diffs

Visual diffs make regressions obvious:

```bash
# Using ImageMagick (if available)
compare baseline.png post.png diff.png

# Or use Python PIL
from PIL import Image, ImageChops
diff = ImageChops.difference(Image.open('baseline.png'), Image.open('post.png'))
diff.save('diff.png')
```

### 4. Publish with `tot`

Publish the HTML prototype to a shareable URL:

```bash
tot docs/sdlc/gate-visual-prototype.html
# Returns: https://tot.page/<id>
```

**Update existing page:**
```bash
tot update https://tot.page/<id> docs/sdlc/gate-visual-prototype.html
```

### 5. Annotate for Review

Open the artifact in Plannotator's annotation UI for human review:

```bash
# Informational (no approval needed)
plannotator annotate prototype.html

# Gate (requires approve/deny)
plannotator annotate plan.html --gate
```

## Design System Tokens

When creating HTML prototypes, use Plannotator's theme tokens for consistency:

```css
:root {
  --background: oklch(0.97 0.005 260);
  --foreground: oklch(0.18 0.02 260);
  --card: oklch(1 0 0);
  --primary: oklch(0.50 0.25 280);
  --secondary: oklch(0.50 0.18 180);
  --muted: oklch(0.92 0.01 260);
  --muted-foreground: oklch(0.40 0.02 260);
  --accent: oklch(0.60 0.22 50);
  --destructive: oklch(0.50 0.25 25);
  --success: oklch(0.45 0.20 150);
  --warning: oklch(0.55 0.18 85);
  --border: oklch(0.88 0.01 260);
  --code-bg: oklch(0.92 0.01 260);
  --font-sans: 'Inter', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
  --font-display: ui-serif, Georgia, serif;
  --radius: 0.625rem;
}
```

## Verification Checklist

- [ ] HTML prototype is self-contained (no external dependencies except CDN)
- [ ] Renders correctly in both light and dark palettes
- [ ] Published to tot with shareable URL
- [ ] Screenshots captured for key views/states
- [ ] Visual diffs generated (if baseline exists)
- [ ] Plannotator annotation link created for review
- [ ] Artifact linked to kanban task `t_9972fd8f`

## Output Artifacts

| Artifact | Path | Purpose |
|----------|------|---------|
| Gate documentation | `docs/sdlc/gate-visual.md` | This file — skill wrapper |
| HTML prototype | `docs/sdlc/gate-visual-prototype.html` | Interactive SDLC workflow demo |
| Screenshots | `docs/sdlc/screenshots/` | Visual regression baselines |
| tot URL | (published) | Shareable review link |

## Integration with SDLC Pipeline

```
PLAN (t_85e5b418)
    │
    ├──► VISUAL (t_9972fd8f) ← THIS GATE
    │        │
    │        ├── effective-html prototype
    │        ├── Computer Use screenshots
    │        ├── tot publish
    │        └── plannotator review
    │
    └──► AUDIT (t_1af3e909) ← unblocked after VISUAL completes
```

## Next Gate

Once VISUAL is complete, AUDIT (`t_1af3e909`) unblocks. AUDIT depends on:
- ✅ VISUAL (`t_9972fd8f`) — this gate
- ⬜ KPI (`t_12706ba1`) — parallel track
- ⬜ REVIEW (`t_be662ce1`) — downstream of TDD

---

*Gate version: 1 | Created: 2026-09-11 | Board: sdlc-gates | Workflow: school-core SDLC*
