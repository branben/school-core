---
title: Persona, Skills, Tools, and Kanban Learning-Loop Audit
type: audit
status: active
date: 2026-08-12
project: school-core
---

# Persona, Skills, Tools, and Kanban Learning-Loop Audit

## ELI5 summary

The school has a notebook, a scoreboard, a routing desk, and a kanban board. They
all contain useful information, but they do not all talk to each other. The
scoreboard changes who is allowed to take harder work. The board mostly shows
what happened. Teacher feedback is written down, but it usually does not change
who gets the next task or which skills are loaded.

**Bottom line:** the pipeline is operational, but its learning loop is only
partly closed.

## Scope and evidence

This audit covers the runtime files that define:

- persona definitions and reviewer profiles;
- domain-to-role and score-gate routing;
- Hermes/Orca tool access;
- bookbag, teacher, trajectory, and consolidation records;
- kanban column assignment; and
- model-combo outcome routing.

Evidence reviewed includes `role_loader.py`, `config/roles/*.yaml`,
`routing.py`, `executor.py`, `router_experience.py`, `leaf.py`, `teacher.py`,
`bookbag.py`, `board.py`, `trajectory.py`, `context_orchestrator.py`,
`consolidation_writer.py`, and the issue/crew path.

## Follow-up implementation status (2026-08-12)

> **Validation update (2026-08-12).** G1–G5 are implemented and test-backed;
> G2 (kanban parity), G4 (cross-cycle context), and G5 (capability evidence)
> are additionally covered by the landing plan `2026-08-12-002` with dedicated
> suites. G6 remains open and is tracked separately (FirstMate spawn-error
> durability). The live crew smoke proof listed below is still the remaining
> operational check.

The five claimable follow-ups are now implemented and test-backed:

- [x] **G1** — `capabilities.py` resolves school rank, task role, profile, skills, tools, gate, and escalation.
- [x] **G2** — `board.py` keeps retry, blocked, crew-in-flight, and school-failed states visible.
- [x] **G3** — `teacher_feedback.py` records bounded redacted evidence and sends the normalized result to RouterExperience.
- [x] **G4** — Layer 3 loads the newest prior same-domain consolidation when a new cycle has no local archive; Layer 2 trajectory reuse remains active.
- [x] **G5** — `director.py` and `issue_bridge.py` surface capability and teacher-evidence blocks in results and `last_run.json`.

Known boundaries are explicit: RouterExperience still selects by task role, not
by domain/difficulty context, and the automated G4 proof is a deterministic
context fixture rather than a live fresh-checkout run. Those are limits to
future optimization, not hidden behavior.

## Current loop map

```text
GitHub issue
  -> classify domain + difficulty
  -> choose specialized role from domain map
  -> score gate + cost-aware agent routing
  -> load prompt/profile/tools
  -> student execution (crew or direct-Orca)
  -> verify / Entire sensor / CTO + COO review
  -> bookbag + trajectory + last_run
  -> score EMA + RouterExperience outcome
  -> next issue
```

### What closes the loop today

1. **Capability gate:** `ScoreStore.qualifying_agents()` and `routing.route_task()`
   use the agent/domain score and difficulty gate. A score change can change
   future eligibility.
2. **Cost-aware choice:** `_best_cost_aware()` prefers cheaper eligible agents
   for easy and medium tasks, while hard/blocker work prioritizes quality.
3. **Model-combo feedback:** `executor.select_combo()` and
   `record_routing_outcome()` feed `RouterExperience`, an epsilon-greedy
   persisted bandit. This changes future combo selection after recorded
   success/quality outcomes.
4. **Context reuse:** trajectory files can be retrieved for similar domains;
   consolidation can add patterns, learnings, and recurring errors to prompts
   when the matching session is available.
5. **Independent review:** CTO and COO write separate verdicts/findings to the
   bookbag. The bridge acceptance decision can reject low-scoring work.
6. **Operational handoff:** `last_run` and the board expose the latest issue
   result, while retry state carries transient failures to a later cycle.

## Persona and skill audit

### Three different persona systems exist

| Layer | Source | What it controls | Current status |
|---|---|---|---|
| School rank | `config/roles/student.yaml`, `teacher.yaml`, `faculty.yaml` | score range, expertise prose, rules, criteria, escalation prose | Declarative; not the complete runtime dispatch contract |
| Task role | `executor.DOMAIN_ROLE_MAP`, `leaf._profile_for_role()` | coder/searcher/executor/browser/reviewer and Hermes profile | Runtime routing path |
| Review persona | `config/profiles/teacher-cto/SOUL.md`, `teacher-coo/SOUL.md` and `teacher.py` | adversarial lens, review format, reviewer tools | Runtime review path |

This split is useful for human readability, but it creates drift risk. The
school-rank YAML describes expertise and escalation, while the actual issue path
selects a specialized task role from the domain map and then selects a Hermes
profile/toolset. A change to one layer does not automatically update the other.

### Tools are selected by code, not by a canonical capability manifest

The student and teacher paths do have explicit tool boundaries. For example,
student execution is routed through Orca/Hermes and teacher review uses the
review toolset (`hermes-cli,file`) with direct OmniRoute fallback. That is a
real safety boundary.

The new `capabilities.py` resolver now exposes a machine-readable bundle for
role, domain, skills/anchors, allowed tools, profile, gate, and escalation
policy. The bundle is additive and preserves the older YAML and runtime maps.
Forbidden tools and promotion policy are still not fully centralized; those
remain future hardening work. The pipeline can now answer mechanically, for the
current dispatch, “Why did this persona receive this profile and tool set?”

## Kanban audit

`board.assign_column()` uses the latest `last_run` entry first, then the
processed set, then the GitHub issue state.

It explicitly recognizes:

- `in_progress` → In Progress;
- `review` / `in_review` → In Review;
- `retry` → Retry Pending;
- `blocked` → Blocked;
- `crew_in_flight` → Crew In Flight;
- `school-failed` / `error` → School Failed; and
- `done` / `success` → Done.

These states remain visible instead of falling through to **To Do**, so a human
can distinguish new work from retry, infrastructure blockage, and a failed
school gate.

The board is therefore a **read model**, not a routing policy. It does not
currently choose a persona, tool bundle, or next action. The bridge and retry
files make those decisions elsewhere.

## Feedback-join audit

| Signal | Stored? | Changes the next decision? | Assessment |
|---|---:|---:|---|
| Agent/domain score | Yes | Yes: eligibility and cost-aware route | Strongest closed loop |
| Model-combo success/quality | Yes | Yes: combo bandit selection | Closed, but context is coarse when recorded only by role |
| CTO/COO verdict | Yes | Yes: current acceptance / school-failed decision | Current-task gate; not promotion learning |
| Teacher findings and diagnoses | Yes | Yes: bounded normalized signal feeds RouterExperience | Evidence is attached to the trajectory and result; router selection remains role-scoped |
| CE route decision | Yes in route/bookbag records | No proven future routing effect | Mostly observability today |
| Trajectories | Yes, when checkpointed | Yes for similar-task context and training selection | Useful, but domain-filtered and freshness-sensitive |
| Layer 3 consolidation | Yes when written | Yes: newest prior same-domain archive can enrich a new cycle | Live fresh-checkout proof remains an operational follow-up |
| Kanban status | Yes in `last_run` | No direct routing effect | Operational read model with status loss for failures |
| Crew result/fallback | Yes in crew records and run output | Yes for same-cycle fallback/retry | Does not yet change future crew policy |

## Historical gaps and implementation contracts

### G1 — Canonical capability contract (P1) — implemented

Unify school rank, task role, skills/anchors, Hermes profile, tool allowlist,
forbidden tools, and escalation policy into one versioned capability manifest.
Keep the existing YAML and runtime maps as compatibility inputs during the
migration, but make one resolver authoritative and testable.

**Done when:** a route decision can return `role`, `profile`, `skills`,
`allowed_tools`, `difficulty_gate`, and `escalation`; tests prove the selected
bundle matches the domain and rank.

### G2 — Preserve kanban terminal and waiting states (P1) — implemented

Extend the board read model so `retry`, `school-failed`, `blocked`, and
`crew_in_flight` remain visible as distinct states instead of falling through
to To Do. Do not let the board mutate GitHub state; this is a read-model change.

**Done when:** every lifecycle state produced by the bridge has a stable board
column or explicit status badge, and tests cover each mapping.

### G3 — Join teacher evidence to routing feedback (P2) — implemented

Create a bounded, auditable feedback record keyed by `agent + domain +
difficulty`, containing the two-judge result, grounded/mechanical result, and
failure classes. Feed only normalized outcomes into routing or promotion rules.
Do not let an LLM judge directly rewrite capability scores without a separate
mechanical or human gate.

**Done when:** a repeated failure class can change the next route or escalation
choice, and the reason is visible in the decision record.

### G4 — Make context continuity explicit (P2) — implemented

Define whether a school-loop cycle gets a stable session or a new session. If
cycles are intentionally new, add a bounded cross-session archival lookup; if
sessions are intentionally stable, persist and restore the session identity.
Test a fresh checkout where a prior trajectory/consolidation changes the next
prompt.

**Done when:** one reproducible fixture proves Layer 2 and Layer 3 context can
change a later task, not merely that files exist on disk.

### G5 — Persist persona/tool evidence in observability (P2) — implemented

Add a compact, redacted block to the bookbag/`last_run` record showing the
selected task role, profile, skills/anchors, toolset, route reason, and fallback
reason. This makes Telegram/GitHub comments useful to a human and to the next
agent without exposing credentials or full prompts.

**Done when:** a reviewer can answer “who acted, with which tools, and why?”
from one result record.

### G6 — Finish FirstMate failure observability (P0 in progress)

Keep the current `school-core-qte` work focused: persist a bounded, redacted
`spawn_error` for `spawn_failed` records. Do not mix capability-manifest or
board work into that change. A successful crew proof should follow after the
error field is durable.

## Recommended execution order

```text
G6  ->  live crew smoke proof
G1  ->  G5  ->  G2  ->  G3  ->  G4
```

Reasoning:

1. Finish the active observability fix before another live crew attempt.
2. Establish one capability contract before adding more feedback joins; it
   gives later records stable role/tool identifiers.
3. Surface state and tool evidence before changing routing behavior, so humans
   can inspect the system while it learns.
4. Add teacher-to-routing feedback only after the record is independently
   reviewable.
5. Prove context continuity last because it depends on stable identifiers and
   durable records.

## Loop-library design: the bounded improvement loop

**Trigger:** a completed or failed school-loop issue cycle.

**Observe:** fresh issue state, domain/difficulty, selected capability bundle,
verification result, CTO/COO verdicts, score delta, fallback, board status, and
recent trajectory/consolidation evidence.

**Choose:** one highest-value correction from the ordered gaps above. Never make
more than one routing/policy change per improvement cycle.

**Act:** implement the smallest test-backed change in the active bead only.

**Verify:** focused tests, one fresh-cycle fixture, and an independent review;
for live crew changes, require a real report plus teardown evidence.

**Record:** bead, changed files, tests, route/bundle evidence, and remaining
risk. Keep secrets, raw prompts, and absolute home paths out of durable records.

**Stop:** stop on a green acceptance check, a blocked external dependency, a
no-progress cycle, or a change that would be destructive/production-affecting
without explicit approval.

## Non-goals

- Do not replace the two-judge review with a single score.
- Do not make Entire or the verify gate an LLM learning signal.
- Do not let the kanban board become a second task database; beads remain the
  lifecycle authority.
- Do not merge school-rank personas and specialized task roles by deleting one
  prematurely; first create a compatibility resolver and tests.
- Do not claim FirstMate is end-to-end proven until a successful report and
  teardown record exist.

## Acceptance checklist for this audit

- [x] Persona sources, runtime role routing, profiles, and tool boundaries mapped.
- [x] Score and model-combo feedback loops separated from stored-only signals.
- [x] Kanban status loss identified with a concrete mapping failure.
- [x] Layer 2/Layer 3 continuity caveat recorded.
- [x] Follow-up units ordered without mixing unrelated changes.
- [x] Persist and validate the bounded redacted crew spawn error (`school-core-qte`).
- [ ] Run a new live crew smoke proof after G6 is green.

## Remaining aspiration (G6)

The only open follow-up from this audit is **G6 — FirstMate failure
observability**: persist a bounded, redacted `spawn_error` for `spawn_failed`
crew records in the `school-core-qte` worktree, then run a successful live crew
proof. Everything else in this audit is implemented and validated.
