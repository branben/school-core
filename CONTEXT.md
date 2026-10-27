# CONTEXT.md — Vocabulary Map (two-loop collisions)

> **What this file is.** One word = one meaning. This is the *vocabulary-collision*
> layer for the school-core repo: the terms that mean different (sometimes opposite)
> things in the **outer** loop vs the **inner** loop, with the owning `file:line` and a
> confidence for each. Read this before reasoning about "where code runs" or "wha
> failed."
>
> **Loops.**
> - **OUTER** = the issue→PR pipeline: crew dispatch (`src/crew/crew_dispatch.py`),
>   fleet scheduling (`src/tools/school_scheduler.py`), teacher review, scoring. The
>   working grading spine.
> - **INNER** = the student execution environment where the student agent runs code:
>   the VM stack (`src/crew/student_vm_runner.py`, `verifier_vm.py`,
>   `smol_cloud_runner.py`). Built + tested, **not yet wired into prod**; prod path is
>   the Orca leaf worktree (`src/crew/leaf.py` → `OrcaExecutionManager.run_hermes`).
>
> **Relationship to the generated L1.** `onboard.py::generate_context_md` emits
> the L1 `CONTEXT.md` (task-routing table) and, since this change, a generic
> **Vocabulary collisions** section (the collision *classes*, no file:lines).
> This document is the **concrete school-core-specific** map: the same five
> terms with verified `file:line` for this repo. The two are complementary —
> onboard guarantees the layer survives regeneration for *any* repo; this file
> adds the verified pointers. A future `onboard` run on this repo appends its
> generated block (handwritten content outside the markers is preserved), so
> the vocab layer is no longer a clobber risk.
>
> **Precedent (already fixed).** `onboard.py:4-8` documents the same failure mode: L0–L4
> were once called "ICM L0–L4", which collided with the ICM *stage* meaning; the term
> "ICM" was reserved for stages and the layers renamed "context LAYERS". This map
> continues that work for the outer/inner boundary.

---

## The collisions

### 1. `blocked` — the sharpest (3 meanings, two of them near-opposite)

| Side | Owner | file:line | Meaning |
|---|---|---|---|
| OUTER (crew status) | `src/crew/crew_dispatch.py` | `:1136-1158` (halt→`:1138` `{"blocked","needs-decision"}` → returns `"blocked"`) | A crew **paused / awaiting a decision**; classified *recoverable* — **preserve the worktree**. Work is safe, the loop is politely on hold. |
| OUTER (fleet occupancy) | `src/tools/school_scheduler.py` | `:180` (`status in ("running","blocked")`) | A fleet worktree slot is **held by an in-flight crew**. A healthy "busy" signal. |
| OUTER (scoring) | `src/sensors/score_finalizer.py` | `:29` (`if result.get("status") == "blocked": return`) | A terminal status that **skips scoring**. |
| INNER (VM guard) | `src/crew/student_vm_runner.py` | `:130` `class StudentVMBlocked(Exception)`, raised ~30 sites (`:502`–`:748`) | The VM sandbox **refused/rejected an operation** as invalid or unsafe (archive too large, bad branch, unclean worktree). The op did **not** run. |
| SANDBOX (network) | `src/crew/smol_cloud_runner.py` | `:279` & `:811` (`"network": {"mode": "blocked"}`) | Egress is **disabled by design**. A capability that is off, not a failure and not a wait. |

**Why it tangles:** OUTER `blocked` = "halted, recoverable, awaiting decision" while
INNER `blocked` = "operation rejected by a guard." One is a *patient hold*, the other is
a *guard firing*. Read from the wrong loop, a `blocked` looks like progress in one and a
rejection in the other. **Confidence: HIGH** (every line read).

### 2. `stage` — execution phase vs orchestration stage

| Side | Owner | file:line | Meaning |
|---|---|---|---|
| INNER | `src/tools/pipeline_metrics.py` | `:91` `def stage(name)`; used `issue_bridge.py:2124` `"context"`, `:1149` `"verify"`, `:3062` `"scoring"` | A **timed execution phase** of one issue's run (context → student_generation → implement → review → validate → entire → review → output_verification → scoring). Includes *failed* stages. |
| OUTER | `onboard.py` | `:4-8` (docstring) | The **ICM workflow stage model** (Prime → Plan → Implement → …). Orchestration stages, not per-issue timers. |

**Why it tangles:** `metrics.stage("verify")` is a stopwatch around inner work; an ICM
"stage" is a workflow phase. Same word, different grain. **Confidence: HIGH.**

### 3. `refused` — two different gates

| Side | Owner | file:line | Meaning |
|---|---|---|---|
| INNER / VM guard | `src/crew/student_vm_runner.py` | `:130` `StudentVMBlocked` | VM rejects an op as invalid/unsafe (see `blocked`). |
| INNER (binding) | `issue_bridge.py` | `:1871` (`candidate binding refused`) | A candidate binding could not be established. |
| BRIDGE (publish gate) | `src/bridge/candidate_pr_gate.py` | `:74` `class GateDenied` ("refused a provider write"), `:119` ("treat the candidate as refused") | The **PR-creation gate** denies a provider write; carries a machine-readable `reason`. |

**Why it tangles:** both are "refused", but the VM guard and the PR-publish gate are
different owners on different layers. A `refused` from the bridge is not a VM rejection.
**Confidence: HIGH.**

### 4. `failed` — process death vs reviewer verdic

| Side | Owner | file:line | Meaning |
|---|---|---|---|
| OUTER (crew terminal) | `src/crew/crew_dispatch.py` | `:1136` (`{"done","failed","resolved"}`), `:1457` | The crew **process exited badly**. A runtime death. |
| REVIEW (verdict) | `src/entire_review.py` | `:427` (`status: "pass"|"fail"|"skipped"|"error"`), `:571` (`verdict == "request changes"`) | The reviewer **voted** to fail / request changes. A judgment, not a crash. |

**Why it tangles:** a *process failure* and a *reviewer "fail" verdict* share the word
`failed` and both are agreement on a negative — the classic vocabulary-gap signature
(both sources agree on the word, so a comparator finds no disagreement). A `failed`
verdict is not a crashed crew; a crashed crew is not a rejection. **Confidence: HIGH.**

### 5. `status` — the meta-collision (one field, disjoint value-domains)

| Side | Owner | file:line | Value domain |
|---|---|---|---|
| crew lifecycle | `src/crew/crew_dispatch.py` | `:1136-1158` | `done` / `failed` / `resolved` / `blocked` / `needs-decision` / `working` / `timeout` |
| review | `src/entire_review.py` | `:427` | `pass` / `fail` / `skipped` / `error` |
| skillopt contract | `src/tools/skillopt_contract.py` | `:84` | `ready` / `blocked` |
| scoring read | `src/sensors/score_finalizer.py` | `:29` | reads `result["status"] == "blocked"` |

**Why it tangles:** every collision above rides on a dict field literally named
`status`` whose value-set changes by owner. `"blocked"` is valid in the crew domain and
in the skillopt domain but means different things; `"fail"` is a review verdict, not a
crew status. **Never compare two `status` strings across owners without checking the
owner first.** **Confidence: HIGH.**

---

## How to use this

- **One word = one meaning, keyed by owner.** When you see `blocked`/`failed`/`status`,
  find the owning file first, then the meaning.
- **A `status` string is only comparable within its owner.** Cross-owner `status`
  comparison is the bug class this map exists to kill.
- **`blocked` is the tripwire.** If a log says `blocked`, ask "whose blocked?" — outer
  says *recoverable hold*, inner says *guard rejection*, sandbox says *egress off*.
- **To make this durable in code:** teach `onboard.py::generate_context_md` to emit this
  section, so a generated CONTEXT.md carries the vocabulary layer and cannot be
  silently overwritten.

*Verified by reading each cited line, 2026-10-26. All rows HIGH confidence.*
