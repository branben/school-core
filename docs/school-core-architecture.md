# Agent-School Architecture (zoomed-out, 4 layers)

> Goal of this doc: lock the *shape* of the system before code moves. This is
> the map. It exists because the framework kept getting conflated into
> "the school-core repo," which is the reference implementation — not the
> runtime. The runtime is **Hermes**, dispatched *by* Orca, routing student
> thinking *through* OmniRoute. Four layers. Keep them apart.

## The one analogy

| Layer | What it is | Owns | Verified native primitive |
|-------|-----------|------|--------------------------|
| **1. Runtime** | The office building | worktrees (rooms), terminals (desks), the click-to-open-task front desk, the clock (schedules) | `orca worktree create`, `orca automations create --provider hermes` |
| **2. Brain** | The employee's mind | SOUL/persona (`-p`), skills, beads (task tracking), AgentMail (escalate-to-human + act on reply) | `--agent hermes --prompt` (Hermes **is** a registered Orca agent — proven by the live "Spec-Gap Harness Loop" automation) |
| **3. Curriculum** | The company operating manual | routing, two-judge review (CTO/COO), EFC competency gates, acceptance rule | *(none — this is policy, lives as a Hermes skill, parameterized by repo)* |
| **4. Substrate** | The model faucet | which LLM actually runs a student's thinking; cost/free-tier routing | OmniRoute @ `localhost:20128` (`/v1/chat/completions`); Hermes `delegation.model` |
| **5. Memory** | The library | 4-layer context (Obsidian vault + Engram + Serena/CocoIndex MCP) | `data/vault` (DEFAULT_VAULT); Engram/Serena/CocoIndex MCP tools |

> Note: "Memory" (the 4-layer context) is a cross-cutting concern, not a
> strict tier — every layer reads from it. Listed separately so it isn't
> forgotten (it was, in earlier zoom-outs).

## The click flow (repo-agnostic)

When a human clicks a task in Orca (issue/PR URL opens in the agent terminal):

```
Orca (L1)  ── clicks task ──▶ opens <repo> worktree + hands context to agent
   │
   ▼
Hermes (L2) ── Principal SOUL + school skill (L3) ── loads, classifies bead,
   │           routes by EFC to a student leaf (another Hermes in a child worktree)
   ▼
Student leaf (L2) ── thinks via OmniRoute (L4) ── model = delegation.model
   │              (auto/coding:reliable → openrouter → localhost:20128)
   ▼
Bookbag written to ~/.hermes/bookbag/<bead>.json  (L3 contract)
   │
   ▼
CTO + COO (L2, two Hermes personas) review ── write cto_verdict / coo_verdict
   │
   ▼
Principal ── _persist_acceptance: accepted = both PASS AND score≥50, no critical
   │
   ▼
AgentMail (L2) notify_verdict → human replies /approve /reject /fix → Principal acts
```

**None of L2–L4 care which repo the worktree is.** That is the repo-agnostic
property. It is *almost* true in code today; the coupling points are listed
below so the refactor knows exactly what to cut.

## Layer responsibilities (precise)

### L1 — Runtime (Orca) — owns NOTHING about judgment
- Provides the worktree the agent operates in (`orca worktree create --repo <id>`).
- Provides the **trigger** (schedule/automation) — *not* a `while True` Python
  loop inside a terminal pane. Native `orca automations create --trigger …`
  is the scheduler.
- Provides click-to-task (already built — opens issue/PR URL in agent terminal).
- **Must NOT** reimplement loops. The `while True` in `run_principal_loop.py`
  / `run_teacher_loop.py` is the fragile anti-pattern to retire (`orca-worktree-launch`
  skill: native spawn "RETIRES the hand-rolled … code entirely").

### L2 — Brain (Hermes) — the portable runtime
- SOUL/persona via `-p principal` / `-p cto` / `-p coo` (profiles in
  `~/.hermes/profiles/`, or repo `config/profiles/<name>/SOUL.md` primary).
- Beads = task tracking (`bd`), replacing any in-repo issue list.
- AgentMail = **bidirectional** control plane (agents report verdicts → human
  replies `/approve|/reject|/fix` → Principal acts). REST, not MCP, proven from
  plain Python with `AGENTMAIL_API_KEY` (`scripts/principal_mail.py`).
- **The student leaf is a Hermes agent** (in a child Orca worktree), not a raw
  `hermes chat -q` one-shot and not a direct API call.

### L3 — Curriculum (the school skill) — policy, repo-parameterized
- Classification + EFC routing (`director.py` gate: easy/medium/hard/diploma).
- Two-judge review: `TEACHER_LENSES = {cto:[CORRECTNESS,SECURITY], coo:[COMPLETENESS]}`.
- Bookbag contract (`bookbag.py`): NOW **verdict-record only** — `cto_verdict`,
  `coo_verdict`, `accepted`, `findings`, `score`. Task lifecycle (`status`,
  `claim`, `files_changed`, `verification`) belongs to `bd` (beads). The bead id
  IS the bookbag id.
- Acceptance rule (`conductor.py:_persist_acceptance`): both PASS AND score≥50
  AND no critical → `accepted` bool.
- **This is the part that must become a Hermes skill**, parameterized by
  `repo_path`, so it is no longer coupled to the school-core checkout.

### L4 — Substrate (OmniRoute) — the model faucet
- Local gateway at `localhost:20128`. `/v1/models` → HTTP 200 (verified up).
- Hermes `delegation.model` controls which alias a *spawned child* uses
  (currently `auto/coding:reliable` → openrouter → `localhost:20128`).
- In-repo `executor.py` has a *separate* `COMBO_MAP` (`auto/best-free` per role)
  — this is an **orphaned** model layer: the live leaf path does NOT call it.
- **The leaf path bypasses OmniRoute entirely today** (see Gap A). The student
  must route thinking via the Hermes delegation config so it flows through
  `localhost:20128`, not a bare `hermes chat -q` one-shot.

### L5 — Memory (4-layer context)
- Layer 0 Ambient: vault structure / domain glossary (CocoIndex search of vault).
- Layer 1 Structural: Serena (exact symbol, LSP) + CocoIndex (semantic AST).
- Layer 2 Episodic: Engram (`mem_store`/`mem_search`/`trigger_rem_cycle`).
- Layer 3 Archival: Obsidian vault (`data/vault`, YAML frontmatter notes),
  sleep/wake consolidation (`sleep_state.py`).
- Self-healing: `classify_issue()` auto-triage + StaffPlugins (janitor,
  session_manager, score_auditor, adversarial_reviewer) operating on the
  school's own state.
- Root system = research papers (EFC, SkillOpt, Harness-1, Sleep, Compiling
  Workflows, Semantic Anchors) — the basis for every design decision.

## Coupling points to cut (for repo-agnostic + Hermes↔Orca integration)

| # | Location | Problem | Fix |
|---|----------|---------|-----|
| **A** | `leaf.run_via_hermes` → `orca_executor.run_hermes` (`hermes chat -q`) | Student leaf is a **one-shot**, bypasses OmniRoute (L4) and the Hermes delegation config. "Delegating students through OmniRoute" does nothing because no delegation happens. | Route the student through Hermes `delegate_task` (or set `delegation.model`) so thinking flows via `localhost:20128`. One-shot `hermes chat` = no agent loop. |
| **B** | `bookbag.py` is a full task tracker (status/claim/files_changed) | Duplicates `bd` (the repo-mandated tracker) AND contradicts CLAUDE.md/AGENTS.md ("use `bd` for ALL task tracking"). | Collapse bookbag to **verdict-record only** (`cto_verdict`/`coo_verdict`/`accepted`/`findings`/`score`). `bd` owns task lifecycle; bead id = bookbag id. |
| **C** | `conductor.py --serve` → `run_principal_loop.py` (`while True` pane) | Manual loop in a terminal — the #1 "won't stand up in Orca" failure mode. | ✅ Replaced with `orca automations create --provider hermes` (Orca owns schedule). `--stop-serve` removes it. |
| **D** | `_boot_teachers` → `terminal send "python3 run_teacher_loop.py"` | Fragile launcher; empty-terminal + `teacher-*-review` terminal spray every boot. | ✅ Each teacher = persistent worktree (rediscover-or-create) + a `orca automations create --provider hermes --workspace path:<wt> --trigger "every 5m"` automation running `scripts/run_teacher_review_once.py <role>` (one-pass judge, then exits). No `while True` pane, no terminal spray. `run_teacher_loop.py`/`run_principal_loop.py` deleted. |
| **E** | Curriculum files live **inside** school-core repo | Not portable to other repos. | Move curriculum into a Hermes skill (`agent-school-core`) parameterized by `repo_path`. school-core becomes the demo, not the runtime. |
| **F** | `executor.py` COMBO_MAP (`auto/best-free`) orphaned | Duplicates OmniRoute routing that the live path ignores. | Either wire it in (if you want per-role L4 routing) or delete it to avoid two sources of truth for L4. |
| **G** | `bookbag.py:wait_for_bookbag` — 5s filesystem poll for handoff | Hand-rolled pub/sub on disk; reimplements Orca's agent bus. | Replace with `orca orchestration send --to <handle>` (push). Bookbag written by the *receiver* on message receipt (still the durable audit record). |
| **H** | `engram_adapter.py` + `trajectory.py` + `sleep_state.py` reimplement memory/consolidation | Duplicates **Engram** (which `director.py`/`trajectory.py` already import). | Use Engram as the durable store; retire the local YAML/JSON memory layer. |
| **I** | `activity_log.py` + `activity_server.py` (port 8765) homegrown dashboard | Reimplements Orca's native observability; contradicts the "Lavish" preference (want lifecycle events on Orca's *existing* dashboard, not a new server). | Emit lifecycle events to Orca's native activity surface; delete the custom HTTP server. |

## The Paperclip parallel (validation)

Paperclip's "leader hires CTO + COO" = your Principal → teacher-cto/coo. Same
topology, independently arrived at. Your differentiator (what "people want"):
the **AgentMail bidirectional rail + beads + SOUL personas riding on Orca's
runtime**, instead of a closed agent that opens a URL and waits. That is the
integration worth shipping — and it is repo-agnostic by construction once
E is done.

## Pedagogy (role boundaries — INVARIANTS)

The kitchen analogy: Student = line cook, Teacher = health inspector (one
standard), Principal = head chef (routes + decides), You = owner (sets menu +
tastes final plate). **Each role has ONE job and minimum context. Overlap is
the bug.** These boundaries are hard invariants — the refactor must not blur
them.

| Role | Unique job (the ONE thing) | What it needs | Must NOT do |
|------|---------------------------|--------------|------------|
| **Student (Leaf)** | *Produce the artifact + evidence.* Run the task, change files, write the verdict-record (`bookbag.py`: `output`, `files_changed`→`bd`, `verification`). | Orca worktree (L1) · OmniRoute thinking (L4) · MCP context — Serena/CocoIndex/Engram (L5) · the bead (from `bd`) | Decide/judge quality · self-review on correctness/completeness · accept itself |
| **Teacher (CTO / COO)** | *Judge ONE axis.* CTO = CORRECTNESS+SECURITY. COO = COMPLETENESS. Write `cto_verdict`/`coo_verdict` = PASS/FAIL. | The student's verdict-record (one bead) · its single lens · nothing else | Edit code · dispatch work · see the other teacher's verdict (keep judges independent) |
| **Principal** | *Route, reconcile, accept.* Classify bead → EFC-route to student; collect both verdicts (via `orchestration send`, not poll); apply acceptance rule → `accepted` bool. | `bd` queue (L3 task) · routing table (EFC) · the two verdicts · acceptance rule | **Edit code, EVER — even on `/fix`.** `/fix` = re-dispatch a fresh student with the note, not a Principal edit. |
| **You (human-in-loop)** | *Set direction + final gate.* Inject beads/tasks; on AgentMail verdict, reply `/approve` `/reject` `/fix`. | AgentMail rail (L2) · ELI5 summary · one decision per bead | Watch the loop live (that's the dashboard) · micro-edit · read a log firehose |

### The `/fix` ruling (explicit)
`/fix` is **another instruction to the router**, identical in shape to the
original task — it re-dispatches the bead to a NEW student worktree with your
note as context. The student fixes → CTO+COO re-judge → Principal applies the
acceptance rule → AgentMail sends a new verdict → you `/approve`.

**No Principal edit path exists.** Rationale: a Principal edit would bypass the
two-judge gate, shipping un-reviewed code — violating the framework's core
guarantee that *every artifact that leaves the school is two-judge-verified*.
Cost of re-dispatch = one cheap free-tier student cycle; the integrity
guarantee is not negotiable. Principal's only write-capability stays the
`accepted` boolean.

### Pedagogical rules
1. **One job per role — no overlap.** The "Must NOT do" column is a hard boundary.
2. **Minimum context per role.** Teacher gets one bead + one lens. Student gets
   the task + the room. Context bloat is how roles blur.
3. **The verdict-record is the only handoff artifact.** Student writes it →
   teachers read it → principal reconciles it. `bd` tracks *task state*; the
   bookbag tracks *judgment*. Two concerns, two stores.
4. **You are the apex judge, not the supervisor.** Final gate (approve/reject/
   fix-instruction) via AgentMail — never the editor, never the live-watcher.

## Lifecycle & Orca cost (INVARIANT)

**3 persistent resources + disposable students. This caps Orca footprint at a
fixed 3 regardless of bead volume — the fix for zombie worktree/terminal
pressure.**

| Role | Lifecycle | Orca cost shape | Why |
|------|-----------|----------------|-----|
| **Principal** | persistent (1 worktree + 1 terminal, always up) | fixed ×1 | Router, never disposed |
| **Teachers (CTO/COO)** | persistent (1 worktree + 1 terminal each) | fixed ×2 | Judges; stay warm to receive verdicts via `orchestration send` |
| **Student** | **disposable** (worktree + terminal created per bead, **deleted on dispose**) | O(beads), zero after | Only role that churns; its cleanup must be airtight |

### Rules
- **Rediscover-before-create for the 3 persistent roles.** Before booting, scan
  `orca worktree list` and reuse an existing `principal`/`cto`/`coo` worktree by
  prefix (the `orca-worktree-launch` skill's guard). This prevents the
  auto-suffix leak (`cto-2`, `cto-lens-2`, …) that accumulates worktrees.
- **Guaranteed-dispose for students.** On cycle end: `orca worktree rm --force`
  + `orca terminal rm`. No suffix reuse survives a cycle. If disposal fails
  twice, return `False` (don't `rm -rf` — leaves stale Orca registry entries).
- **Measured baseline (2026-07-26):** 25 worktrees (mostly `auto-*` automation
  spray + 1 stray `cto-lens-2`), 6 terminals (2 `teacher` = the persistent
  CTO/COO, the rest from other projects). Target steady state: exactly
  `principal` + `cto` + `coo` persistent; `main`/`auto-*` belong to OTHER
  projects' automations, out of scope here.

## Verification checklist (after any refactor)

- `orca repo list` contains the target repo (register once if not).
- `orca automations create --provider hermes …` returns `ok:true` (principal loop).
- `orca worktree create --agent hermes --repo <target> --prompt …` returns
  `agentTerminalHandle` (student/teacher spawn).
- Student thinking visibly routes via `localhost:20128` (OmniRoute request log),
  not a bare `hermes chat` one-shot.
- Dispatch a task on a **non-school-core** repo → bookbag appears with both
  verdicts → `accepted` computed. (Proves repo-agnostic.)
- AgentMail thread for the bead receives `/approve`/`/reject`/`/fix` and the
  Principal acts on next tick.
- **Lifecycle invariant:** `orca worktree list` shows exactly `principal` +
  `cto` + `coo` persistent; students are ephemeral (gone after cycle).
- **No `*-2`/`*-3` suffixed worktrees** survive a boot/re-boot (rediscover-
  before-create holds).
