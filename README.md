# Agent School Core

<a href="https://github.com/branben/school-core">
  <img src="/.github/animated-badge.svg" alt="Agent School Core – live" width="380" height="80">
</a>

> ⚠️ **Qodo Command CLI is discontinued.** The pre-merge review now uses
> [Entire CLI](https://github.com/entireio/cli) (`entire review`) —
> intent-aware code review that reads git checkpoints. No API key needed.

A **developmental framework for AI agents** where students grow through
practice, feedback, challenge, and rest — not prompt-chaining.

- **Roles over prompts** — persistent personas (Student / Teacher / Principal / Janitor)
- **Compiler before critic** — execute the code first, LLM-as-judge last
- **Adversarial review mandatory** — two-judge (CTO + COO) on every output
- **Growth over performance** — difficulty-adjusted EMA scoring
- **Memory that compounds** — Library/Engram consolidation

## Quick Start

```bash
# One-command setup (Nix)
nix develop github:branben/school-core

# Dispatch a task
orca dispatch --profile student-coder "implement feature X"

# Review + rubber-stamp via AgentMail
python -m school_core review <bead-id>
# → AgentMail card lands in inbox: [/approve] [/reject] [/fix]
```

## Flow

```
  Student ──►  CTO Review  ──►  Bookbag  ──►  AgentMail Card  ──►  /approve
  Student ──►  COO Review  ──►    │                        ──►  /reject
                                 │                        ──►  /fix <note>
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Principal routes work by measured competency  │  No sycophancy     │
  └──────────────────────────────────────────────────────────────────────┘
```

## Personas

| Role | Gate | Description |
|------|------|-------------|
| **Student** | 0-24 pts | Takes easy tasks, builds foundations |
| **Senior Student** | 25-49 pts | Passes medium tasks |
| **Teacher** | 50-74 pts | Can mentor, review, and stress-test |
| **Faculty** | 75+ pts | Handles blockers, designs curriculum |
| **Principal** | — | Routes work by competency |
| **Janitor** | — | Prunes stale trajectories, consolidates Library |

## Project Structure

```
school-core/
├── campus.md                  # 🏛️ Identity & behavioral core
├── docs/
│   ├── HANDOFF.md             # 📋 Bookbag state machine + signal protocol
│   ├── school-core-architecture.md
│   └── agent-school/
├── config/
│   ├── anchors.yaml           # Semantic anchors (TDD, YAGNI, Fagan…)
│   └── profiles/*/SOUL.md     # Persona definitions
├── src/
│   ├── conductor.py           # Principal — routes work + review
│   ├── leaf.py                # Student leaves — disposable worktrees
│   ├── teacher.py             # Teacher lifecycle — sleep/wake/review
│   ├── bookbag.py             # Bookbag state + file-lock protocol
│   ├── qodo_pre_merge.py      # ✨ Entire review shim (pre-merge)
│   └── agentmail_poller.py    # 📬 Inbound /approve → commit+push+close
├── tests/                     # 910-test suite (TDD)
├── campus.md                  # Identity & behavioral core
└── flake.nix                  # Reproducible dev env (Nix)
```

## Live Stats

| Component | Status |
|-----------|--------|
| Test suite | ✅ 910 tests passing |
| Qodo pre-merge | ⚠️ Discontinued → Entire CLI |
| AgentMail loop | ✅ Cron every 2 min |
| Beads kanban | ✅ 0 blocked, 0 open |

## Key Documents

| Purpose | File |
|---------|------|
| Identity | [`campus.md`](campus.md) |
| Handoff contract | [`docs/HANDOFF.md`](docs/HANDOFF.md) |
| Anchors/methods | [`config/anchors.yaml`](config/anchors.yaml) |

## Contributing

See [`docs/HANDOFF.md`](docs/HANDOFF.md). Build against **wired** capabilities only — `campus.md` marks what's operational vs aspirational.

---

> *"Help every agent become better than they were yesterday."*
> — Load [`campus.md`](campus.md) for the full identity.
