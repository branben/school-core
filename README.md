# Agent School Core

A developmental framework for AI agents where students grow through practice, feedback, challenge, and rest — not prompt-chaining.

## Quick Start

```bash
# One-command setup (Nix)
nix develop github:branben/school-core
# Or without Nix:
pip install -e .[dev] && pre-commit install

# Dispatch a task
python -m school_core dispatch "implement feature X" --repo branben/target-repo
# Review output  
python -m school_core review <bead-id>
# Close out
python -m school_core close <bead-id>
```

## The Flow

```
  ┌──────────┐     ┌──────────┐     ┌──────────────┐     ┌──────────────┐
  │  Student │────▶│   CTO    │────▶│              │────▶│   Campus     │
  │ (coder/  │     │ (review  │     │   Bookbag    │     │ (identity +  │
  │  writer/ │     │  verdict)│     │   contract   │     │  memory)     │
  │ browser) │     └──────────┘     └──────────────┘     └──────────────┘
  └──────────┘                             │   ▲
          ▲                                 │   │
          │                                 │   │
          │   ┌──────────┐                  ▼   │
          └───│   COO    │────▶ (accept | reject | fix)
              │ (review  │
              │  verdict)│
              └──────────┘
```

## Personas

| Role | Gate | What They Do |
|------|------|-------------|
| **Student** | 0-24 pts | Takes easy tasks, builds foundations |
| **Teacher CTO** | 50-74 pts | Reviews for correctness + security |
| **Teacher COO** | 50-74 pts | Reviews for acceptance criteria completeness |
| **Principal** | — | Routes work by measured competency |
| **Janitor** | — | Prunes stale trajectories, consolidates Library |

Launch: `orca dispatch --profile student-coder` | `orca dispatch --profile teacher-coo`

## Project Structure

```
school-core/
├── campus.md                  # 🏛️ Identity & behavioral core (load this)
├── docs/
│   ├── HANDOFF.md             # 📋 Bookbag state machine + signal protocol
│   ├── school-core-architecture.md
│   └── agent-school/
├── config/
│   ├── anchors.yaml           # Semantic anchors (TDD, YAGNI, Fagan...)
│   └── profiles/              # Persona SOUL.md files
├── src/
│   ├── conductor.py           # Principal — routes work, orchestrates review
│   ├── leaf.py                # Student leaves — disposable worktrees
│   ├── teacher.py             # Teacher lifecycle — sleep/wake/review loop
│   ├── bookbag.py             # Bookbag state + file-lock protocol
│   └── adversarial_reviewer.py # Review engine (co-evolution loop)
├── tests/                     # 910-test suite (TDD)
└── flake.nix                  # Reproducible dev env
```

## Key Documents

| Purpose | File |
|---------|------|
| **Identity** | [`campus.md`](campus.md) |
| **Handoff contract** | [`docs/HANDOFF.md`](docs/HANDOFF.md) |
| **Architecture** | [`docs/school-core-architecture.md`](docs/school-core-architecture.md) |
| **Anchors (roles/methods)** | [`config/anchors.yaml`](config/anchors.yaml) |
| **Persona definitions** | `config/profiles/*/SOUL.md` |

## What Makes This Different

- **No sycophancy feedback loops** — adversarial review is mandatory, not optional
- **Scores measure correctness, not confidence** — compiler first, critic second
- **Roles over prompts** — persistent behavioral contracts, not one-off instructions
- **Memory that compounds** — episodic → archival via Library/Engram consolidation
- **Growth over performance** — difficulty-adjusted capability, not flat scores

## Contributing

See [`docs/HANDOFF.md`](docs/HANDOFF.md) for the full lifecycle contract. The
system is **honest about state** — `campus.md` has a status table marking what's
wired vs aspirational. Build against wired capabilities only.

---

> *"Help every agent become better than they were yesterday."*
> — Load [`campus.md`](campus.md) for the full identity.
