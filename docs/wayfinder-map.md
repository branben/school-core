# School-Core Wayfinder Map

> Durable topology map for the Agent School stack. This document answers **who owns what**, **which artifact crosses each boundary**, and **where evidence returns**. It does not replace a CE plan, a `bd` issue, or the runtime implementation.
>
> **Canonical location:** `school-core/docs/wayfinder-map.md`
> **Maintained with:** `docs/route-envelope.md`, `docs/school-core-architecture.md`, `docs/pipeline-explainer.md`, and the active CE plan for the current change.

The route-envelope schema is the companion contract for the identifiers and
state transitions shown in this map.

## The stack in one view

```text
                         CONTROL PLANE

 KnowledgeCore ──► deterministic intent route ──► Wayfinder map
        │                                             │
        └──────────────────────┬──────────────────────┘
                               ▼
                         CE plan + U-IDs
                               │
                               ▼
                    bd beads + dependencies
                               │
                               ▼
                         CE work execution
                               │
                               ▼
                         EXECUTION PLANE

                  FirstMate ──► Orca ──► Hermes
                  dispatch       runtime    agent/tools/model
                               │
                               ▼
                         git artifact + evidence
                               │
                               ▼
                         ASSURANCE PLANE

       project verify gate ──┬── Entire sensor ──┬── CTO/COO review
                             │                  │
                             └──────┬───────────┘
                                    ▼
                         acceptance + bookbag verdict
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
          bd lifecycle update                     AgentMail human gate
                 │
                 ▼
                         LEARNING PLANE

      trajectory + consolidation + score + RouterExperience
                                    │
                                    ▼
                         next bounded route decision

                         OBSERVATION SURFACES

          Kanban / last_run / activity / reports = read models
```

## Ownership boundaries

| Concern | Authority | Evidence it emits | Must not become |
|---|---|---|---|
| Durable task lifecycle | `bd` | bead status, dependencies, claim, close | A second Kanban/bookbag tracker |
| User intent and durable context | KnowledgeCore | anchor/context references | A task executor |
| System topology | This Wayfinder map | ownership and dependency edges | A duplicate implementation plan |
| Implementation decisions | CE plan | requirements, decisions, U-IDs, tests | Mutable execution state |
| Worktree/agent dispatch | FirstMate | crew status, report, fallback | A quality judge |
| Runtime isolation | Orca | worktree, terminal, automation identity | Curriculum or scoring policy |
| Agent behavior | Hermes | session/tool/model evidence | Task lifecycle authority |
| Mechanical verification | `verify_gate.py` / project checks | test/typecheck/lint result | A subjective grader |
| Intent-aware diff review | Entire | checkpoint/diff findings | The sole acceptance gate |
| Quality judgment | CTO + COO | verdicts, findings, score | A task queue |
| Verdict record | `bookbag.py` | accepted, findings, score | Full task tracking |
| Human decision rail | AgentMail | approve/reject/fix | Automated merge authority |
| Learning state | trajectories, consolidation, scores, RouterExperience | bounded outcome/history | Unverified policy mutation |
| Operational visibility | Kanban, `last_run`, activity | read-only status projection | Routing authority |

## Canonical task identity

A task should be traceable through this chain:

```text
KC anchor
  ↔ wayfinder_map
  ↔ ce_plan_id + plan_unit (U-ID)
  ↔ bd_id
  ↔ cycle_session_id
  ↔ firstmate_crew_id
  ↔ orca_worktree_id + terminal_id
  ↔ hermes_session_id
  ↔ git base/branch/commit + Entire checkpoint
  ↔ verify result
  ↔ bookbag verdict
  ↔ bd close/update
  ↔ learning record
```

Not every execution path exposes every identifier yet. Missing identifiers are
observability gaps, not permission to invent a value. The route-envelope work
should add the missing joins incrementally and preserve redaction boundaries.

## Lifecycle paths

### Normal implementation path

```text
request
  → deterministic intent classification
  → KnowledgeCore pull
  → Wayfinder lookup/update
  → CE plan or existing plan unit
  → bd bead claim
  → CE work
  → FirstMate/Orca/Hermes when admitted
  → artifact and evidence
  → verify gate + Entire + CTO/COO
  → bookbag verdict
  → bd update/close
  → trajectory and compound record
```

### Low-risk solo fast path

```text
existing bd bead
  → deterministic low-risk admission (`fast_lane.py`)
  → CE work in current/disposable worktree
  → local verify + Entire
  → bd close with evidence
```

The fast path retains `bd`, verification, Entire, and close-out evidence. It
only removes unnecessary crew/school overhead for work that is already bounded.
Admission fails closed without a bd identity, one-file/low-risk scope, or all
required checks. `summarize_lane_metrics()` must show lower overhead with no
higher rework before the policy is promoted.

### Failure and repair path

```text
failure evidence
  → classify edge and mode
  → choose one bounded next action
      ├─ retry same route
      ├─ change capability/role
      ├─ repair task contract
      ├─ repair runtime/tool
      ├─ escalate to teacher/faculty
      └─ request human decision
  → record reason and evidence
  → stop after no progress
```

A failed task must not silently become a successful task merely because a
fallback produced a response. Lifecycle success, task quality, and pipeline cost
remain separate signals.

## Current implementation truth

| Area | Current state | Boundary to preserve |
|---|---|---|
| Capability bundle | Implemented and recorded for role/profile/skills/tools/gate | The resolver is additive while older role maps remain compatible |
| FirstMate → Orca → Hermes crew path | Wired behind a flag with bounded fallback and artifact checks | Do not claim every model/delegation route is proven by worktree success |
| Verify gate | Wired with explicit production preflight and reusable soft-skip/strict behavior | A skipped gate is not a pass |
| Entire | Wired as a non-blocking intent-aware sensor | It informs review; it does not replace CTO/COO acceptance |
| CTO/COO review | Independent correctness/security and completeness lenses | Teachers judge; they do not dispatch or edit |
| `bd` | Repository task authority | Bookbag and Kanban must not duplicate lifecycle state |
| Kanban | Read model of `last_run` and issue state | It displays state; it does not choose the next route |
| RouterExperience | Adaptive combo selection with static cold-start fallback | It may optimize model choice inside a route, not the outer CE workflow |
| CE route decision | Recorded in route/bookbag evidence | Future routing effect remains an open feedback-loop task |
| SkillOpt | Installable, but no reproducible corpus/held-out benchmark contract yet | Do not claim lift until the measurement spine exists |

## Design invariants

1. **One authority per concern.** `bd` owns lifecycle; CE owns plan decisions;
   Orca owns runtime; school-core owns judgment and growth.
2. **Compiler before critic.** Mechanical verification runs before subjective
   review whenever the project can execute the artifact.
3. **Entire is evidence, not authority.** Intent-aware findings enrich the
   review record without silently changing the acceptance rule.
4. **A report is not proof by itself.** The report, branch, commit, base, and
   inspected artifact must refer to the same work.
5. **Adaptive routing stays inside deterministic routing.** Exploration may
   choose among approved model combos, but it must not change the task's
   workflow without a recorded policy decision.
6. **One policy change per learning cycle.** Observe, choose one correction, act,
   verify independently, record, and stop on no progress.
7. **Read models do not become authorities.** Kanban, activity, and dashboards
   project state from authoritative records; they do not mutate task policy.
8. **No fabricated evidence.** Missing credentials, missing artifacts, skipped
   verification, and unavailable runtimes are explicit terminal states.

## Follow-up map

The implementation work is tracked under Beads epic
`brandonbennett-een`:

```text
.3  durable Wayfinder map                         ← this artifact
.1  canonical cross-layer route envelope
.2  normalized failure-edge/outcome taxonomy
.4  CE U-ID ↔ bd/runtime evidence join
.9  deterministic route → verifier resolution
.5  Entire findings → learning record
.6  post-bead compound learning loop
.7  solo-dev fast lane
.8  bounded SkillOpt experiment contract
```

The dependency order intentionally delays SkillOpt until the pipeline can
separate skill quality from model, routing, runtime, verifier, and task-contract
effects.
