# Agent School Core

A **developmental framework for AI agents** where students grow through
practice, feedback, challenge, and rest — not prompt-chaining.

The name is literal: it is a school. Students attempt tasks, teachers review
their work, the principal routes by readiness, and the janitor cleans up what
didn't teach anything. Every artifact in the repo exists to support that loop.

---

## The pedagogy (read this first)

Agent engineering is developers trying to reframe pedagogy. The structure that
has survived 800 years of human education — school — is the same structure
needed for LLM agents that cannot grow by prompt alone.

**The problem:** LLMs are competent mimics. Give them a prompt, they produce
something plausible. When the edge cases surface, when "good enough" turns out
to be wrong, they have nowhere to go. They can't fail, learn from failure, or
grow.

**The school's answer:** Every task is a practice act, not a one-shot execution.

| School element | Agent engineering analog |
|----------------|--------------------------|
| **Practice** | Every task is a Student attempt — disposable, scored, reviewed |
| **Feedback** | Adversarial review by CTO + COO — stress-testing, not validation |
| **Challenge** | Difficulty is measured. Harder tasks only after mastery |
| **Rest** | Teacher sleep/wake cycles. Trajectory pruning. Memory consolidation |
| **Growth** | Difficulty-adjusted EMA scoring — not flat pass/fail |
| **Curriculum** | Semantic anchors (`[TDD]`, `[Fagan Inspection]`) activate full methodologies on demand |

The compiler runs before the critic speaks. Execution over estimation. Tests
over vibes.

---

## The SDLC — two loops

Everything lands as `bd` slices (one testable concern per slice), and every
slice walks both loops:

**Outer loop** — epic → PRD → SPEC → tickets (human-driven)
**Inner loop** — Prime → Plan → Implement → Validate → Review → PR (agent-driven)

```
Outer:  epic → PRD (Tier-3 .md + .html render)
            → SPEC (Tier-3 .md + impl-plan .html)
            → tickets (beads, one testable concern each)

Inner:  Prime    → module-map .html (generated from repo data)
        Plan     → tweakable-plan .html (decisions first)
        Implement→ dev-log.md (deviation log)
        Validate → prototype .html (states + boundary + export)
        Review   → guided review (plannotator-guide; guide.json validated)
        PR       → PR writeup .html published via tot
```

Every slice runs Prime through PR as its own TDD loop. A failed sub-step loops
back to **that** step, not the whole slice.

**Chain of custody:** every inner-loop artifact carries a mandatory `source`
block (YAML front-matter in `.md`, HTML comment in `.html`) tying it back to
the bead, spec, and prior artifact. REVIEW's `guide.json` accumulates all four
prior artifacts plus the SPEC's acceptance criteria. `review_gate.py` check #5
verifies the chain before the PR passes.

Full map: `docs/sdlc/loops.md`

---

## The roles

| Role | Gate | What they do |
|------|------|-------------|
| **Student** | 0–24 pts | Takes tasks, builds foundations |
| **Senior Student** | 25–49 pts | Passes medium tasks |
| **Teacher** | 50–74 pts | Mentors, reviews, stress-tests |
| **Faculty** | 75+ pts | Handles blockers, designs curriculum |
| **Principal** | — | Routes work by competency |
| **Janitor** | — | Prunes stale trajectories, consolidates Library |

The tiers are cosmetic log labels — the real gates are `GATES(scoring)` +
readiness-confidence + two-judge review.

---

## The tool chain

| Tool | Role |
|------|------|
| **Hermes** | Agent runtime — loads personas, manages skills, MCP servers |
| **Orca** | Persistent worktree backend — survives agent deaths |
| **FirstMate** | Dispatch layer — spawns ephemeral crewmates into Orca |
| **Beads (`bd`)** | Durable task tracking — backed by Dolt, syncs via git refs |
| **Engram** | Memory — episodic trajectories + archival consolidation |
| **AgentMail** | Communication bus — inbound `/approve /reject /fix` |
| **Entire** | Pre-merge review — intent-aware, reads git checkpoints |
| **Serena** | Symbol awareness — LSP-based file/symbol resolution |
| **plannotator** | Guided review — structured diff reading order |

---

## What is wired (tested, not aspirational)

| Capability | Status |
|-----------|--------|
| Students (Hermes) dispatch | ✅ Wired |
| Principal verify gate | ✅ Wired |
| Adversarial review (CTO + COO) | ✅ Wired |
| Pre-merge review (Entire) | ✅ Wired |
| Inner-loop chain of custody | ✅ Wired (6 templates + review_gate.py check #5) |
| Beads kanban + CI gating | ✅ Wired |
| Serena LSP layer | ✅ Wired |
| CocoIndex vault layer | ✅ Wired |
| Engram Layer 2 (episodic) | ✅ Wired |

**What is partial or deferred:**
- AgentMail inbound poller — code exists at `src/agentmail_poller.py` but no active cron/launchd entry was found (`ps aux | grep agentmail_poller` returns nothing); the `/approve /reject /fix` workflow is wired but the poller is not currently running
- AgentMail async two-judge — sync mode works; async Phase 2 is future
- Archival consolidation (Layer 3) — gate fires, write-side hook pending
- Student VM stack — built+tested, not wired into prod dispatch (SCH-32b)
- Cloud lane (U7) — present but not the default path

---

## Ecosystem

| Repo | What it is |
|------|-----------|
| [`worst-day-ever-skill`](https://github.com/branben/worst-day-ever-skill) | Adversarial stress-testing — 8 attack dimensions, severity-ranked disaster reports |
| [`wde-mcp-server`](https://github.com/branben/wde-mcp-server) | MCP server exposing Worst Day Ever as tools + resources |
| [`school-core-skills`](https://github.com/branben/school-core-skills) | 16-skill kit for the two-loop SDLC |
| [`orca`](https://github.com/branben/orca) | Persistent worktree backend |
| [`OmniRoute`](https://github.com/branben/OmniRoute) | Model substrate (L4) |
| [`hermes-agent`](https://github.com/branben/hermes-agent) | Agent runtime |

---

## Getting started

```bash
# One-command setup (Nix)
nix develop github:branben/school-core

# See what needs work
bd ready

# Walk one slice through the inner loop
# Prime → Plan → Implement → Validate → Review → PR
# End with: bd close <id>
```

## Clean-device recovery

A clean checkout can verify itself end-to-end with one command:

```bash
bash scripts/fresh_device_check.sh
```

Optional target override:

```bash
bash scripts/fresh_device_check.sh owner/repo
```

What it does, in order:

1. `.env` materialization — copies `.env.example` → `.env` if missing
2. `venv` + `requirements.txt` — installs runtime deps
3. `nix develop` — builds the hermetic `verifyShell` (flake.nix)
4. `bd prime` — restores durable state context
5. `pytest` — runs the full suite
6. `verify_gate` — clones the target repo and runs declared checks

Evidence trail: the script exits non-zero on any failure and prints
per-stage `[fresh-device]` markers so a log tells you exactly where
recovery broke.

---

## Key documents

| Purpose | File |
|---------|------|
| Identity + pedagogy | [`campus.md`](campus.md) |
| SDLC map | [`docs/sdlc/loops.md`](docs/sdlc/loops.md) |
| Handoff contract | [`docs/HANDOFF.md`](docs/HANDOFF.md) |
| Quality gates | [`docs/quality-gates-checklist.md`](docs/quality-gates-checklist.md) |
| Notification tone | [`docs/notification-style-guide.md`](docs/notification-style-guide.md) |

---

## Contributing

See [`docs/HANDOFF.md`](docs/HANDOFF.md). Build against **wired** capabilities
only — `campus.md` marks what's operational vs aspirational.

---

> *"Help every agent become better than they were yesterday."*
> — Load [`campus.md`](campus.md) for the full identity.
