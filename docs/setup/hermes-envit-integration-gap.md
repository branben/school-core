# Hermes ↔ Envit Skill Integration — Current State (2026-09-20)

> **Read-only boundary.** This is the current-state characterization of the seam between
> Hermes' skill loader and envit's materialized skill store. Updated after user discovery
> that all 4 "missing" Hermes-native skills were actually installed in nested subdirs.
>
> **Audience:** operator setting up a fresh Hermes agent on school-core.
> **Depth:** what is and isn't wired today — not a fix plan.

---

## The seam (unchanged — still the core architectural fact)

| Layer | Where it lives | What it does |
|---|---|---|
| **Hermes skill loader** | `~/.hermes/skills/<skill>/SKILL.md` (flat AND nested subdirs) | Loads skill context into agent prompts at session start / tool use |
| **envit 0.1.0 (sync)** | `~/.envit/store/checkouts/<repo>/<commit>/skills/<skill>/SKILL.md` | Materializes declared skill SETS from git at pinned commits into a global store |
| **Integration layer** | **DOES NOT EXIST** (no auto-scan) | Nothing bridges the two automatically. The `bridge_envit_to_hermes.sh` script provides a manual symlink bridge for plannotator skills only. |

A Hermes agent pointed at school-core has NO automatic access to the 8 plannotator SKILL.md files that envit sync materialized — unless the operator runs the bridge script or teaches Hermes to scan `~/.envit/store/`.

---

## What IS wired today

### Plannotator skills (envit-managed, in `~/.envit/store/`)

| Skill SET | Repo | Commit | SKILL.md files |
|---|---|---|---|
| effective-html | plannotator/effective-html | d95debbaef15 | html, html-plan, html-diagram, html-wireframe, html-prototype, design-artifact |
| guides | plannotator/guides | da10932bcb86 | plannotator-guide |
| herdr-annotate | plannotator/herdr-annotate | 7c8f5a177b82 | plannotator-tui |

Materialized and readable on disk. ripwire scans them fine. NOT loadable by Hermes without the bridge script.

### Hermes-native skills (Hermes-managed, in `~/.hermes/skills/`)

**All 8 are installed and loadable.** The "4 missing" claim was stale — written before these were installed or when the flat `ls` search was too shallow to catch subdir nesting.

| Status | Skill | Location | Notes |
|---|---|---|---|
| INSTALLED | pr-verification-gate | `~/.hermes/skills/pr-verification-gate/` (flat) | SKILL.md, references/ |
| INSTALLED | build-verification | `~/.hermes/skills/build-verification/` (flat) | SKILL.md |
| INSTALLED | bounded-adversarial-plan-review | `~/.hermes/skills/bounded-adversarial-plan-review/` (flat) | SKILL.md, references/, assets/ |
| INSTALLED | worst-day-ever | `~/.hermes/skills/worst-day-ever/` (flat) | SKILL.md, assets/ |
| INSTALLED | github-pr-workflow | `~/.hermes/skills/github/github-pr-workflow/` (nested) | SKILL.md, references/ (2 files), modified 2026-08-15 |
| INSTALLED | github-pr-gates | `~/.hermes/skills/github/github-pr-gates/` (nested) | SKILL.md (39KB), references/ (6 files), scripts/ (1), modified 2026-08-13 |
| INSTALLED | pr-merge-readiness | `~/.hermes/skills/github/pr-merge-readiness/` (nested) | SKILL.md (22KB), references/ (2 files), scripts/ (1), modified 2026-09-17 |
| INSTALLED | github-pr-checks-truth | `~/.hermes/skills/github-ops/github-pr-checks-truth/` (nested) | SKILL.md (3.5KB), modified 2026-07-14 |

**None are missing.** The flat `ls ~/.hermes/skills/` lists 230 top-level dirs but does NOT show nested subdirs like `github/` or `github-ops/`. The earlier "missing" assessment used a flat search that missed these. The correct check is recursive: `find ~/.hermes/skills -name SKILL.md`.

---

## What changed in this update

| Before (stale) | After (corrected) |
|---|---|
| `envit.json` `_herm-native-notes.missing` listed 4 skills | Empty — all 8 installed |
| `envit.json` `_herm-native-notes.installed` listed 4 flat skills | Lists all 8 (4 flat + 4 nested with paths) |
| `envit.json` note: "None of these have .skill files" | Note: "All 4 are SKILL.md-only... 2 have references/, 2 have scripts/" |
| school-core-engineering skill: "4 Hermes-native skills missing... need sourcing" | Replaced with "Hermes-native skill inventory (all 8 are pre-installed)" table + descriptions |
| hermes-envit-integration-gap.md: "4 MISSING" table | Replaced with "All 8 installed" table with locations + last-modified dates |
| `setup_envit.sh`: "installs missing Hermes-native skills" | Updated to: "the 8 Hermes-native skills are pre-installed... none need installation from a registry" |

---

## What ISN'T wired (genuine gaps, not stale claims)

| Gap | Severity | What it is |
|---|---|---|
| Hermes has no auto-scan of `~/.envit/store/` | MEDIUM | envit-materialized plannotator skills (8 SKILL.md files) are invisible to Hermes without the manual bridge script. This is by design in envit 0.1.0 — no loader hook exists. |
| `envit.lock.json` pins at HEAD, not exact commits | LOW | A fresh `envit sync --frozen` pulls today's HEAD, not the tested version. Cosmetic until drift matters. |
| `.envit/AGENTS.md` stub is unpopulated by sync | LOW | Cosmetic — the stub says "Generated by envit sync" but envit 0.1.0 doesn't write the repo/skill index into it. |

---

## The bridge script: what it does and doesn't do

`scripts/bridge_envit_to_hermes.sh` closes the plannotator-side gap only:

- **Symlinks 4 unique plannotator skills** into `~/.hermes/skills/`: html-diagram, html-wireframe, design-artifact, plannotator-tui
- **Skips 4 identical skills** (verified by `diff -q`): html, html-plan, html-prototype, plannotator-guide — these already exist in `~/.hermes/skills/` with identical content
- **Does NOT touch the 8 Hermes-native skills** — they're already in `~/.hermes/skills/` (some nested), no bridging needed
- **Does NOT auto-run** — operator runs it manually after `setup_envit.sh`

---

## Files updated in this correction

| File | Change |
|---|---|
| `envit.json` | `_herm-native-notes.missing` → `[]`; `installed` → all 8 with paths; `note` → corrected |
| `school-core-engineering/SKILL.md` | Replaced "4 missing" gap with inventory table + descriptions; updated `setup_envit.sh` description |
| `docs/setup/hermes-envit-integration-gap.md` | Replaced "4 MISSING" table with "all 8 installed" table; corrected all stale claims |
