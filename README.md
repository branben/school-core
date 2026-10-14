# Agent School Core

<a href="https://github.com/branben/school-core">
  <img src="https://raw.githubusercontent.com/branben/school-core/main/.github/animated-badge.svg" alt="Agent School Core – live" width="380" height="80">
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

## Ecosystem

School Core is the judgment + growth layer over a wider stack it leans on.
Adjacent repos in the same ecosystem:

| Repo | What it is |
|------|-----------|
| [`worst-day-ever-skill`](https://github.com/branben/worst-day-ever-skill) | Adversarial stress-testing methodology — 8 attack dimensions, 6 safeguards, severity-ranked disaster reports |
| [`wde-mcp-server`](https://github.com/branben/wde-mcp-server) | MCP server exposing Worst Day Ever as a SEP-2640 skill, tool, and resources |
| [`school-core-skills`](https://github.com/branben/school-core-skills) | 16 agent skills for the two-loop SDLC — epic to tickets, ticket to merged PR |
| [`orca`](https://github.com/branben/orca) | The ADE for fleets of parallel agents — worktrees, terminals, automations (submodule) |
| [`OmniRoute`](https://github.com/branben/OmniRoute) | Free AI gateway — School Core's model substrate (L4), with upstream contributions |
| [`hermes-agent`](https://github.com/branben/hermes-agent) | The agent that grows with you — Hermes sessions, profiles, delegation |

### Worst Day Ever

[`worst-day-ever-skill`](https://github.com/branben/worst-day-ever-skill) is an
adversarial stress-testing methodology: **MAP** the interface, **GENERATE**
pathological scenarios across 8 attack dimensions (input boundary, state
machine torture, temporal/timing, authn/z shadow, data integrity, concurrent
load, external dependency failure, brownfield mining), **SANDBOX** them,
**EXECUTE** one at a time, and **REPORT** a severity-ranked disaster list with
remediation — *before* your users give your project its worst day. It runs
under 6 safeguards: no real data, no secrets, sandboxed execution, no external
calls, bounded duration, no mutation.

School Core applies the methodology internally — see
[`docs/school-core-worst-day-ever.md`](docs/school-core-worst-day-ever.md) and
`tests/test_worst_day_critical_nodes.py`, which prove the fail-closed nodes
(N5.1/N5.2/N5.3) now catch the worst-day failures instead of swallowing them.

**Worst Day Ever as MCP** — [`wde-mcp-server`](https://github.com/branben/wde-mcp-server)
exposes the skill over the Model Context Protocol (Rust, built on
[`rmcp`](https://github.com/modelcontextprotocol/rust-sdk) with the SEP-2640
skills extension):

- **Skill** — `io.modelcontextprotocol/skills` extension
- **Tool** — `worst_day_ever_assess`, callable by any MCP client
- **Resources** — each of the 8 dimensions readable individually
- **Transports** — stdio (`wde-mcp-stdio`) or streamable HTTP (`127.0.0.1:3001/mcp`)

### School Core Skills

[`school-core-skills`](https://github.com/branben/school-core-skills) is a
16-skill kit for the SDLC that School Core's slices walk. Two loops:

```
outer:  grill-me → vision → nose-up → to-prd → to-spec → to-tickets
inner:  Prime → Plan → Implement → Validate → Review → PR
```

Install for any agent:

```bash
npx skills add branben/school-core-skills
```

### OmniRoute contributions

[`OmniRoute`](https://github.com/diegosouzapw/OmniRoute) is School Core's model
substrate — `executor.py` and `omni_relay.py` route student thinking through
`OMNIROUTE_BASE` (`localhost:20128/v1`), with ACRouter combo selection on top.
Upstream contributions from [`branben/OmniRoute`](https://github.com/branben/OmniRoute):

- **MCP context sources** — Notion MCP (6 tools, dashboard tab) · Obsidian MCP (24 tools)
- **MCP fixes** — `enforceScopes` guard reordered before lookup · GitHub skill tools discoverable via `omniroute_tool_search`
- **Admission / capacity** — per-connection virtual admission lanes · process-wide chat-body budget · combo-lane awareness + activation UX + MCP visibility
- **PII redaction fix** — stop redacting code syntax as IPv6
- **In flight** — `[defer]` 2026-07-28 spec-compliant MCP v2 server sidecar (#15983)

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
│   ├── entire_review.py       # ✨ Entire review shim (pre-merge)
│   └── agentmail_poller.py    # 📬 Inbound /approve → commit+push+close
├── scripts/
│   └── run_teacher_review_once.py  # One-shot teacher review pass
├── tests/                     # 2400-test suite (TDD)
└── flake.nix                  # Reproducible dev env (Nix)
```

### Utility scripts (repo root)

One-shot tools that are intentionally not part of the runtime pipeline:

| Script | Purpose |
|--------|---------|
| `regenerate_budgets.py` | Print the live per-difficulty turn-budget table, read from `orca_executor` (read-only) |
| `patch_papers.py` | Patch the generated leaderboard HTML (`docs/site/leaderboard.html`) with paper references |
| `school_md_generator.py` | Generate a `school.md` teaching doc for another repo via a cloud agent |

## Live Stats

| Component | Status |
|-----------|--------|
| Test suite | ✅ 2371 tests passing, 29 skipped (2400 non-live) |
| Entire pre-merge | ✅ Wired (`entire review`, intent-aware; replaces discontinued Qodo) |
| AgentMail loop | ✅ Cron every 2 min |
| Beads kanban | ✅ 0 blocked, 0 open |

## Key Documents

| Purpose | File |
|---------|------|
| Identity | [`campus.md`](campus.md) |
| Handoff contract | [`docs/HANDOFF.md`](docs/HANDOFF.md) |
| Anchors/methods | [`config/anchors.yaml`](config/anchors.yaml) |
| Notification tone spec | [`docs/notification-style-guide.md`](docs/notification-style-guide.md) |

## Notify env

Notifications (verdict cards, issue alerts, CI alerts) route through
`school_mail.py` + the shared `agentmail_client.py`. Set the control-plane
inbox so cards land where the poller watches:

```bash
AGENTMAIL_SCHOOL_INBOX=vault-synthesis@agentmail.to   # control-plane (default)
# AGENTMAIL_INBOX=…                                    # override destination
```

Resolution: `AGENTMAIL_SCHOOL_INBOX` → `AGENTMAIL_INBOX` → first inbox on the
key. The key itself comes from `AGENTMAIL_API_KEY` or `~/.hermes/config.yaml`.
See [`docs/notification-style-guide.md`](docs/notification-style-guide.md) for
the full contract.

## Contributing

See [`docs/HANDOFF.md`](docs/HANDOFF.md). Build against **wired** capabilities only — `campus.md` marks what's operational vs aspirational.

---

> *"Help every agent become better than they were yesterday."*
> — Load [`campus.md`](campus.md) for the full identity.
