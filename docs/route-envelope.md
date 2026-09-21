# School-Core Route Envelope

> Versioned contract joining planning, task lifecycle, runtime execution,
> verification, judgment, and learning. This is the boundary between the
> control plane and the execution/assurance planes.

## Purpose

A route envelope answers one question:

> Why did this task take this route, who/what executed it, what evidence was
> produced, and what decision should happen next?

It is an **evidence index**, not a second task database and not a replacement
for the bookbag, `bd`, Entire, or runtime registries. Each system remains the
source of truth for its own fields.

## Contract

```yaml
schema_version: "1.0"

# Control-plane identity
route_id: "route-<bookbag-bead-id>"
bd_id: "<bead-id>"
plan_id: "<repo-relative-plan-path>"
plan_unit: "U<stable-id>"
wayfinder_id: "school-core-wayfinder-v<version>"
knowledge_anchor: "<anchor-or-null>"

# Deterministic classification
intent: "plan | work | debug | review | optimize | compound | research"
domain: "<school-core-domain>"
difficulty: "easy | medium | hard | diploma"
primary_workflow: "<resolved-workflow>"
overlays: ["<zero-to-two-resolved-overlays>"]
route_reason: "<bounded, human-readable reason>"

# Capability admission
capability:
  version: "<bundle-version>"
  school_role: "<student/senior-student/teacher/faculty>"
  task_role: "<coder/searcher/executor/browser/reviewer>"
  profile: "<hermes-profile>"
  skills: ["<anchor-or-skill-name>"]
  allowed_tools: ["<school-facing-tool>"]
  hermes_toolsets: ["<hermes-toolset>"]
  gate_min: 0
  gate_max: 24
  escalation: ["<bounded-policy>"]

# Runtime correlation
runtime:
  dispatcher: "firstmate | direct-orca | local"
  firstmate_crew_id: "<id-or-null>"
  orca_worktree_id: "<opaque-id-or-null>"
  orca_terminal_id: "<opaque-id-or-null>"
  hermes_session_id: "<opaque-id-or-null>"
  cycle_session_id: "<loop-id-or-null>"

# Artifact and assurance evidence
artifact:
  repository: "<repo-slug-or-null>"
  worktree_path_ref: "<redacted-or-null>"
  base_ref: "<ref-or-null>"
  base_commit: "<commit-or-null>"
  branch: "<branch-or-null>"
  commit: "<commit-or-null>"
  report_ref: "<bounded-relative-ref-or-null>"

verification:
  project_gate: "pass | fail | skipped | unavailable | not_applicable"
  project_gate_reason: "<bounded-reason>"
  entire:
    status: "pass | findings | skipped | unavailable | not_run"
    finding_classes: ["<normalized-class>"]
    finding_count: 0

judgment:
  cto_verdict: "PASS | FAIL | pending | not_run"
  coo_verdict: "PASS | FAIL | pending | not_run"
  accepted: false
  score: 0.0
  critical_findings: 0
  bookbag_ref: "<bead-id-or-bookbag-ref>"

# Learning and next action
outcome:
  lifecycle: "completed | failed | retry | blocked | in_flight | skipped"
  quality: 0.0
  cost_ms: 0
  failure_edge: "none | model | task_contract | skill | tool | runtime | verifier | environment | judge"
  failure_mode: "none | syntax | wrong_file | missing_artifact | missing_evidence | identity_mismatch | spawn_failure | timeout | auth | incomplete | quality | disagreement | unknown"
  fallback_reason: "<bounded-reason-or-null>"
  next_action: "close | retry_same | change_capability | repair_contract | repair_runtime | escalate | human_decision | no_op"
  learning_ref: "<trajectory/consolidation/ref-or-null>"
```

Fields may be absent when a path does not provide them, but absence must be
explicitly represented as `null`, `not_run`, or `not_applicable`; do not invent
runtime identifiers or convert unavailable verification into a pass. A persisted
bookbag bead deterministically receives `route-<bookbag-bead-id>`; routes without
a bead keep `route_id: null` until persistence exists.

## Field ownership

| Envelope section | Owning authority | Read by |
|---|---|---|
| `route_id`, classification, workflow, overlays | deterministic route resolver | CE work, reviewers, learning loop |
| `plan_id`, `plan_unit` | CE plan | bd bridge, execution, close-out |
| `bd_id`, lifecycle | `bd` | bridge, operators, read models |
| `wayfinder_id`, `knowledge_anchor` | Wayfinder / KnowledgeCore | route resolver, future agents |
| `capability` | `capabilities.py` resolver | FirstMate/Hermes launcher, evidence consumers |
| `runtime` | FirstMate/Orca/Hermes adapters | bridge, operators, learning loop |
| `artifact` | crew/direct execution + git checkpoint | verify gate, Entire, reviewers |
| `verification.project_gate` | project verification | acceptance logic, learning loop |
| `verification.entire` | Entire adapter | reviewers, learning loop |
| `judgment` | CTO/COO/bookbag/acceptance logic | AgentMail, bd close-out, learning loop |
| `outcome` | bridge outcome normalizer | router feedback, compound loop, Kanban read model |

No consumer should silently overwrite another authority's fields. A later
record may append a transition or correction, but it must preserve the prior
evidence and reason.

## State transitions

```text
created
  → classified
  → planned
  → ready
  → claimed
  → dispatched
      ├─ in_flight
      ├─ blocked
      ├─ retry
      ├─ failed
      └─ artifact_ready
            → verified
            → reviewed
            → accepted
            → closed

Any non-terminal state may enter:
  → blocked       (missing decision/dependency/credential)
  → retry         (bounded transient failure)
  → failed        (terminal failure with evidence)

accepted is not the same as closed:
  accepted = school judgment passed
  closed    = bd lifecycle is complete
```

A fallback must preserve the original route and append a new route transition;
it must not erase the fact that the preferred path failed.

## Redaction and safety

Durable envelopes may contain:

- opaque IDs;
- repo-relative paths;
- branch and commit names;
- bounded statuses and normalized reasons;
- role, profile, skill, and tool names;
- bounded counts, scores, durations, and verdicts.

Durable envelopes must not contain:

- API keys, bearer tokens, or credential material;
- raw prompts or full model responses;
- full report bodies;
- absolute home-directory paths;
- unrestricted codebase snapshots;
- unbounded logs or terminal output.

The envelope is an index into evidence, not a copy of all evidence.

## Learning rules

1. `lifecycle`, `quality`, and `cost_ms` are separate signals.
2. `failure_edge` identifies which layer should change; do not rewrite a skill
   for a runtime or task-contract failure.
3. `next_action` is a bounded recommendation, not an automatic permission to
   mutate policy.
4. RouterExperience may use the envelope to select among approved model combos,
   but it must not change the outer CE workflow without a recorded policy
   change.
5. A compound-learning cycle may promote one policy/skill/route change only
   after comparable evidence and an independent verification result.

## Implementation order

1. Keep this contract as the reviewable source of truth.
2. Use `evidence_join.py` for additive serialization at result/bookbag boundaries.
3. Thread `bd_id`, plan unit, and route identity through crew/direct paths.
4. Normalize Entire and teacher outcomes into the envelope.
5. Add tests for redaction, fallback preservation, state transitions, and
   lifecycle/quality separation.
6. Only then prepare a bounded SkillOpt baseline/held-out experiment.

## Related artifacts

- [`wayfinder-map.md`](wayfinder-map.md)
- [`school-core-architecture.md`](school-core-architecture.md)
- [`pipeline-explainer.md`](pipeline-explainer.md)
- `docs/plans/2026-08-12-001-persona-tool-kanban-learning-loop-audit.md`
- Beads epic: `brandonbennett-een`
