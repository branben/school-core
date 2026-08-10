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

## FirstMate dispatch (crew spawning)

FirstMate spawns ephemeral Hermes crewmates into Orca worktrees (see
`campus.md` → Operational Reality for the full note). It lives in a
**persistent** location so it survives reboots:

```bash
# One-time install (NOT /tmp — that gets wiped on reboot)
git clone --depth 1 https://github.com/kunchenguid/firstmate.git ~/.local/share/firstmate

# Spawn a crewmate (FM_HOME carries the orca backend config)
FM_HOME=~/.hermes/school-core-fm-config \
  ~/.local/share/firstmate/bin/fm-spawn.sh <task-id> <project-dir> \
  --backend orca --mode no-mistakes --yolo on \
  --harness '/Users/brandonbennett/.local/bin/hermes-fm-wrapper "$($__OPINPUT__ encode launch-brief < $__BRIEF__)"'
```

Working skill: `~/.hermes/skills/firstmate-orca-spawn-hermes/SKILL.md`.

## Quick Start

```bash
# One-command setup (Nix)
nix develop github:branben/school-core

# Dispatch a task
orca dispatch --profile student-coder "implement feature X"

# Review + rubber-stamp via AgentMail
python3 scripts/run_teacher_review_once.py teacher-cto
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
├── conductor.py               # 🎓 Principal — routes work + review
├── director.py                # EFC-gated task pipeline + two-judge review
├── leaf.py                    # 🍃 Student leaves — disposable worktrees
├── teacher.py                 # 🧑‍🏫 Teacher lifecycle — sleep/wake/review
├── bookbag.py                 # 🎒 Bookbag state + file-lock protocol
├── executor.py                # Model routing (ACRouter) + OmniRoute transport
├── src/
│   ├── qodo_pre_merge.py      # ✨ Entire review shim (pre-merge)
│   └── agentmail_poller.py    # 📬 Inbound /approve → commit+push+close
├── scripts/
│   └── run_teacher_review_once.py  # One-shot teacher review pass
├── tests/                     # 942-test suite (TDD)
└── flake.nix                  # Reproducible dev env (Nix)
```

## Live Stats

| Component | Status |
|-----------|--------|
| Test suite | ✅ 942 tests passing, 15 skipped |
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
