# School-Core Complete: Implementation Plan

> **Plan status (2026-09-29):** Plannotator approved this plan; `goal.md` exists. The gap reconciliation below is source-read evidence, not a fresh test run or end-to-end rehearsal.

## Goal and scope

Make school-core reliably take an issue from a configured repository, clone that repository for the task, produce and independently verify an exact code candidate, obtain teacher review, and open a reviewable PR back to the same repository. The system is one operator-controlled school-core deployment that can be configured for multiple repositories; repositories are task targets, not courses or tenants. A controlled hosted pilot and the documented learning enhancements are included. This is not a public multi-tenant SaaS launch, automatic merge, or permission to run student coding on the host.

The approved completion facts are in [`facts.md`](facts.md). This plan treats student-task execution as **fail closed**: VM failure stops the coding task; there is no direct host/Orca coding fallback. Trusted school checks and repository-supplied checks remain distinct. The student agent and shell run inside the disposable VM; the verifier checks the exact candidate separately.

### Operator decisions (2026-10-02)

- **PR release gate:** Open a candidate-bound PR only after the trusted checks pass and a teacher approves that exact candidate. A human retains merge authority; no automatic merge.
- **Production execution gate:** Do not enable production student coding until a hosted, per-task disposable VM path is selected and passes the required boundary and lifecycle checks. Existing local SmolVM evidence is developer proof only, not hosted qualification.
- **Guest network/tooling:** Default-deny guest network access and prefer operator-built, preinstalled toolchains. No package/network exception is approved; any narrow broker or endpoint is a separate decision.

These decisions do not select a hosted provider, authorize deployment, or authorize live GitHub/provider writes.


## Repository evidence and current baseline

The outer and inner loops are established in [`docs/sdlc/loops.md`](../../docs/sdlc/loops.md): the human-driven outer loop turns an epic into reviewed PRD/SPEC and one-concern Beads tickets; each ticket follows Prime → Plan → Implement → Validate → Review → PR. Use the outer loop for this goal as a coordinated initiative, then deliver it as small, dependency-ordered Beads slices. The inner-loop PR is a review artifact; it does not imply that school-core merges student output automatically.

Existing capabilities to reuse rather than rebuild include `config/github.yaml` / `github_fetcher.py` repository targeting, `repo_reader.py` cloning and context collection, `candidate_manifest.py` immutable candidate identity, `candidate_gate.py` exact-candidate check evidence, `candidate_pr.py` an injected candidate-bound publication seam, `state_journal.py` / `merge_coordinator.py` lifecycle primitives, `recovery.py`, and `recovery_smoke.py`.

Important gaps and evidence limits:

- `config/github.yaml` has a `target_repos` setting (currently empty by default), and code has per-repo clone/cache and namespace seams. These do not prove that issue intake, context, candidate, trusted checks, review, and PR publication work end to end for a second repo.
- `issue_bridge.py` constructs `enriched_prompt` with collected target-repo context, but crew dispatch passes `issue["prompt"]`. Correct the handoff and explicitly mark repository text as untrusted input; test the actual task text at the crew boundary.
- The existing PR path in `pr_creator.py` can create a PR from response text; its crew patch behavior commits the patch as a file under `school-output`, not as the candidate's source-code changes. `candidate_pr.publish_candidate_pr` validates and publishes an exact candidate through an injected publisher, but search found no production caller. A true code-change PR is therefore a critical integration slice, not an assumed existing capability.
- **P0 — execution and trusted-verification boundary (source-read):** the current student path uses local Orca/worktree execution, and the bridge permits direct fallback after crew failures. No per-task disposable-VM adapter was found. Production coding dispatch for this goal must remain disabled until the guest, constrained model relay, trusted verifier, and no-host-fallback behavior pass their acceptance checks. The current verifier discovers target-repo `project_verify.yaml`; this is not a school-controlled check manifest, and `.github/workflows/school-loop.yml` comments out `VERIFY_GATE_STRICT`. Repository checks may be reported only as extra evidence.
- Learning systems are partly present: score/feedback/context and model-combo learning exist. `docs/pipeline-explainer.md` (last reconciled 2026-08-12) and the Aug. 12 learning audit list gaps, but later code and tests may have closed some. Re-audit each named enhancement before creating implementation slices; preserve working behavior and only build confirmed gaps.
- **P0 — PR correctness (source-read, 2026-09-29):** `issue_bridge.py` appends a `success` run record before attempting PR creation; if creation returns `None` or raises, it still calls `_mark_github_issue(..., "success")`, adds the issue to `processed`, and calls `mark_processed`. Treat this as a confirmed state-ordering defect: a failed or ambiguous publication must not produce durable success, close the issue, or mark it processed until candidate-bound reconciliation succeeds. `candidate_pr.publish_candidate_pr` has tests but no production caller was found; current `pr_creator` is not proof of source-diff PR publication.
- **P1 — grading durability (source-read):** `issue_bridge.py` enqueues successful jobs, but no production drain schedule was found. `school_grader.drain()` acknowledges every job even when `grade()` returns an error. Preserve failed jobs and add retry/terminal dead-letter/operator recovery. Existing tests cover successful draining and error capture separately, not failure retention. The workflow checkpoint omits `data/grading_queue.jsonl` and `data/compound_learning.json`; fresh-checkout durability for these stores is not established.
- **P1/P2 — review and learning integration (source-read):** teacher polling/review exists, but no complete join from candidate ID/head SHA through changed files, trusted verification, teacher decision, restart recovery, and PR was established. Per-repo namespaces, score storage, compound observations, consolidation helpers, and history caps are partial capabilities—not completion evidence. `shadow_routing._lens_evidence()` aggregates CTO/COO judge-lens outcomes under `skills`, not student skill mastery; the CE router logs chosen workflow names, while `ScoreStore` is primarily agent/domain keyed. Tool usage is counted only with `proven=true`, and no normal-runtime producer of that evidence was found. Keep unobserved student skills unknown.
- Retention is present for specific stores: the workflow trims trajectories and consolidation artifacts, and teacher sessions have a separate cap. The checkpoint still omits grading jobs, compound-learning records, and candidate/evidence state; run/board history also needs an explicit data-class policy. Do not claim either “no retention exists” or “all required state is bounded” without checking the relevant store and recovery behavior.
- **P1 — hosted pilot (source-read plus prior restore report):** the workflow runs task execution on a self-hosted Mac and publishes the board on GitHub-hosted Ubuntu; this is not a hosted disposable-VM pilot. The 2026-09-29 restore report is blocked by missing `OMNIROUTE_BASE` and `GITHUB_TOKEN`. Operator/teacher access, resource controls, monitoring, and successful recovery remain acceptance work. `docs/school-core-scale-architecture.md` also states Orca remains the coding execution truth-source, which conflicts with the VM goal. Reconcile it and `campus.md`, `docs/school-core-architecture.md`, `docs/pipeline-explainer.md`, and FirstMate/Orca docs only after behavior is proven.

These current-code observations are **read-level**. No tests or full-pipeline execution were run for this reconciliation. The measured 2026-09-24 smoke remains limited to candidate → target-declared checks → identity → journal/evidence; it does not prove issue intake, a VM, teacher review, a source-code PR, or hosted restore. Findings labeled P0 are implementation blockers for the accepted contract; P1/P2 items are required integration, recovery, or evidence work, not claims that every helper is absent.

### Prior session and run results to carry forward

- **2026-08-12:** `docs/plans/2026-08-12-001-persona-tool-kanban-learning-loop-audit.md` records G1–G5 as implemented/test-backed, while noting role-scoped routing, deterministic rather than fresh-checkout context proof, and remaining FirstMate/live-proof concerns. Treat it as historical evidence, not a current inventory.
- **2026-08-15:** `bd prime` memory records a live FirstMate → Orca → Hermes smoke with status/report identity and `teardown_ok=true`. It explicitly bypassed routing, evidence join, Entire, and teacher review. This is lifecycle evidence, not end-to-end completion evidence.
- **2026-08-16:** `docs/session-2026-08-16-walkthrough.md` and `docs/open-items-2026-08-16.md` record fixes and outstanding runner-side concurrency proof. Concurrency is not a prerequisite for this goal; do not raise it before the single-student contract and VM boundary are proven.
- **2026-09-24:** `data/recovery/smoke-evidence-check/report.json` records a successful disposable-target candidate → declared checks → identity validation → journal/evidence smoke. It does not test GitHub issue intake, VM execution, teacher review, or PR creation.
- **2026-09-29:** `data/recovery/restore-report.json` confirms Beads, local trajectories, and evidence are readable, while restore is blocked by missing local service/credential configuration. This is not a clean hosted restore/pilot pass.

Use these results as baseline evidence with their stated boundaries. For every new external or runtime claim, capture fresh run output and exact candidate/repository identity. A new live issue-to-PR rehearsal requires a separate, explicit operational authorization at the time it is run.

## Solution approach

First construct the single-repository candidate-to-PR path against hermetic fixtures and explicit runner interfaces: the student must produce a source candidate, the independent trusted verifier must test that exact candidate, teachers must review the same candidate and its evidence, and candidate-bound PR publication must target the same repo. This phase is not permission to enable production host execution; production coding dispatch stays disabled for this goal until Phase 2 qualifies the disposable VM and there is no host/Orca fallback. Then prove the same contract for a second repo. Complete only the learning gaps still present after re-audit, then prove restart/recovery and the controlled hosted pilot. Keep changes behind the current outer/inner loops, with Beads as task state and a recorded checkpoint after each vertical slice.

## Ordered phases, dependencies, and verification

### Phase 0 — Outer-loop charter and evidence baseline

**Touches:** `goals/school-core-complete/facts.md`, this plan; `docs/sdlc/loops.md`; `docs/plans/`; Beads; the current runtime paths and their focused tests.

1. Create one parent Bead for this initiative and derive one testable Bead per accepted outcome/vertical slice. Record dependencies and acceptance checks in Beads; do not create a parallel markdown TODO list.
2. Produce/review the authoritative Tier-3 PRD/SPEC using the existing outer-loop contracts. The spec must explicitly say: one operator-controlled deployment, configured repos as cloned task targets, PR rather than auto-merge, coding VM fail-closed, trusted checks separate from repo checks, and the bounded learning backlog below.
3. Build a current evidence map from the actual `main` code and tests. Reconcile the stale Aug. 12 pipeline-explainer and the scale-architecture claims. Record which old gaps are already fixed and which have executable acceptance gaps.
4. Keep `CREW_MAX_PER_CYCLE=1` for initial proof; no parallelism/capacity raise is needed to complete the single-task contract.

**Verification:** Every planned slice has a Bead, an owner, dependencies, explicit “done means,” and focused test/live evidence. The SPEC traceability matrix maps all 10 accepted facts to implementation and verification evidence. No claim relies only on a prior session summary.

### Phase 1 — Single-repository candidate-to-PR vertical slice

**Touches:** `issue_bridge.py`, `repo_reader.py`, `crew_dispatch.py` / student runner boundary, `candidate_manifest.py`, `candidate_gate.py`, `candidate_pr.py`, `pr_creator.py`, `review_packet.py`, `teacher.py` / `teacher_feedback.py`, `state_journal.py`, `tests/test_issue_bridge.py`, `tests/test_candidate_*`, `tests/test_vertical_pilot.py`.

1. Trace and test issue selection → repo clone → task context → student artifact → verify/review → candidate creation → PR publication. Ensure task/issue/repository/cycle IDs flow without ambiguity.
2. Fix context delivery: pass the intended `enriched_prompt` and selected repo context to the student path; label untrusted repository/issue text as data, not policy. Add a regression test that asserts the actual prompt handed to the crew contains the expected repo context.
3. Turn the actual source diff into an immutable `CandidateManifest` on the selected repo clone: repo slug, issue/bead, base ref and SHA, branch, head SHA, diff digest, and candidate ID. Reject dirty/uncommitted or identity-mismatched candidates before grading/publication.
4. Bind verification, Entire/review sensors, teacher packet, score, and PR request to that same candidate identity. Teacher approval/rejection/requested changes must identify the candidate ID and head SHA, and survive restart. Run the school's trusted check manifest independently from the candidate's repository-supplied checks; report repo checks only as extra evidence. A missing/unrunnable trusted gate is a failure, not a pass or silent skip.
5. Wire the already-existing `candidate_pr.publish_candidate_pr` seam into the production path through a provider adapter. Publish the actual validated source diff to a branch/PR in the same repository; do not create a PR that merely stores a patch blob while describing it as a code fix. Make publication idempotent by candidate ID/head SHA and record the PR URL/provider state. Current bridge behavior closes and processes the issue even when publication returns `None` or raises; replace that behavior. If the provider write fails or its result is ambiguous, do not report success, close, or mark the issue processed; persist a candidate-bound `pr_pending`/`pr_failed` state, reconcile provider state before retrying, and make retry safe against duplicate PR creation.
6. Retain human review and merge boundaries. Do not auto-merge. Open a PR only after successful trusted checks and teacher approval bound to the exact candidate; a human still owns merge. Test that neither a skipped/unrunnable trusted gate nor approval for another candidate can authorize publication.
7. Keep production coding dispatch for this goal disabled until Phase 2 qualifies the VM adapter. No direct host/Orca fallback may satisfy the coding-task contract or be invoked after VM infrastructure failure. Any separately retained non-coding behavior must have a distinct documented boundary.

**Verification:**

- `python3 -m pytest tests/test_issue_bridge.py tests/test_candidate_manifest.py tests/test_candidate_gate.py tests/test_candidate_pr.py tests/test_pr_creator.py tests/test_review_packet.py tests/test_vertical_pilot.py -q`; add focused tests for PR `None`/exception/ambiguous paths and assert no durable success run record, issue close, or processed checkpoint is written until candidate-bound reconciliation succeeds.
- Temporary-Git integration tests prove a modified source file appears in the publisher's exact diff; a stale head, wrong repo, changed base, dirty tree, candidate ID mismatch, skipped trusted gate, or empty patch results in no provider write.
- Provider adapter tests use a fake publisher and verify idempotent replay; no live GitHub write is part of unit tests.
- Publication failure/ambiguous outcome tests exercise both the `None` and exception paths in the current bridge, prove the issue remains open/unprocessed, candidate state becomes pending or failed, replay reconciles by candidate ID/head SHA, and no duplicate PR is created.

### Phase 2 — Disposable VM boundary and trusted verifier

**Touches:** `crew_dispatch.py`, `orca_executor.py` only where necessary to cut host execution, `execution_sandbox.py`, `verify_gate.py`, `candidate_gate.py`, guest image/toolchain configuration, the host-side model relay, `.github/workflows/school-loop.yml` or the selected hosted runner workflow, and new VM/security integration tests. Reconcile `campus.md`, `docs/school-core-scale-architecture.md`, `docs/orca-dispatch-architecture.md`, and `docs/school-core-architecture.md` after the boundary works.

1. Write a threat model and interface contract before adapter code. Enumerate host filesystem, repo clone, host credentials, GitHub write credentials, model relay, student guest, returned patch/logs, verifier, and teacher. Define limits, lifecycle states, network policy, and recovery/cleanup behavior.
2. Qualify a hosted disposable Linux VM runner before enabling production student coding. SmolVM is local Apple Silicon developer proof, not hosted qualification or a pre-approved production security guarantee. Verify the current SDK/API and supported hosting before selection; keep the runner interface replaceable so local proof does not lock deployment to a Mac-only substrate.
3. Put Hermes/student agent, repository clone, shell, and repo toolchain inside the guest. No host worktree mount, Orca control socket, SSH agent, raw model/GitHub token, or writable host path may be exposed. Package/copy the repo through a bounded, validated input channel; return only bounded status, report, patch/candidate identity, and selected evidence.
4. Default-deny guest network access. Prefer prebuilt, operator-controlled offline toolchains. Design the model relay as a narrow task-scoped capability: explicit allowed model operations, per-task quota/time limits, bounded request and response, no arbitrary proxying, and revocation on terminal state. Any required model/package endpoint or broker is a separately reviewed exception; none is approved by this decision.
5. Use operator-controlled guest image/toolchain and trusted verification configuration. Do not let repository-controlled Nix/manifest settings change VM privileges, trusted check commands, resource limits, or network policy. Run trusted checks in a separate clean verifier VM against the exact exported candidate; repo-provided checks remain separate extra evidence.
6. Make every VM lifecycle failure (create/start/agent/relay/collect/verify/destroy) a visible blocked/failed result. Never route a coding task to host execution after failure. Failed cleanup quarantines the resource for operator recovery; it is not silently reused. Preserve validated candidate/report evidence only according to bounded retention policy.
7. Test malicious output paths/symlinks, malformed/oversized patches and logs, host-canary reads, forbidden egress, untrusted prompt/config injection, timeout, process death, and cleanup failure. Include a test asserting there is no host execution fallback.

**Verification:**

- Focused tests: `python3 -m pytest tests/test_crew_dispatch.py tests/test_execution_sandbox.py tests/test_verify_gate.py tests/test_candidate_gate.py -q` plus new runner/relay tests.
- A local proof demonstrates create → execute → exact candidate export → clean verifier check → artifact collection → destroy on the target machine. This is necessary developer proof, not hosted readiness; production coding dispatch remains disabled until the selected hosted per-task VM passes the required boundary and lifecycle checks.
- Host canary, host credential, arbitrary-network, and malformed-artifact probes are denied; deliberate VM startup/runtime failure yields blocked status and zero host-run calls.
- Verifier evidence binds task ID, repo, base SHA, candidate ID/head SHA, trusted manifest digest, and checks actually run.

### Phase 3 — Multi-repository configuration and isolation

**Touches:** `config/github.yaml`, `github_fetcher.py`, `issue_bridge.py`, `repo_reader.py`, `conductor.py`, `scoring.py`, bookbag/review/evidence namespaces, PR provider adapter, and tests such as `tests/test_github_fetcher.py`, `tests/test_two_judge_repo_namespace.py`, `tests/test_vertical_pilot.py`.

1. Specify the supported-repo entry: canonical `owner/repo`, issue source/labels/overrides, clone/cache policy, trusted verification policy, toolchain/image reference, and PR base/policy. Keep secrets out of checked-in config; resolve per-repo least-privilege credentials from the deployment secret boundary.
2. Use the selected repo as the identity for issue fetch, clone, candidate manifest, trusted checks, reviewer packet, scores/learning namespace, retry/evidence records, and PR destination. Existing clone/cache and namespace support are starting points, not proof of a working flow. Reject a candidate or callback that crosses repository identity.
3. Replace or safely refactor the current teacher-pair-per-repo approach if it multiplies persistent worktrees; avoid solving multi-repo support by duplicating whole teacher fleets. Keep the Principal/orchestrator repo separate from the target repo being cloned.
4. Add a two-repository hermetic integration fixture. It must show that adding a second configured repo uses its own clone, base, trusted checks, task context, evidence and PR destination, with no code branch or cross-repo leakage.
5. Keep “multiple repos” distinct from “multi-tenant SaaS”; no independent-customer/organization tenancy is in scope.

**Verification:**

- `python3 -m pytest tests/test_github_fetcher.py tests/test_two_judge_repo_namespace.py tests/test_bookbag_decision_provenance.py tests/test_evidence_join.py tests/test_candidate_manifest.py tests/test_candidate_pr.py -q`
- Two-repo integration fixtures assert repository identity at every boundary and prove a candidate from repo A cannot be verified, reviewed, or published as repo B.
- A second repo is onboarded using configuration only, with a documented procedure and no school-core source-code fork.

### Phase 4 — Close the learning loop, based on a fresh gap audit

**Touches:** `director.py`, `growth_tracker.py`, `scoring.py`, `router_experience.py`, `teacher_feedback.py`, `trajectory.py`, `compound_learning.py`, `consolidation_writer.py`, `context_orchestrator.py`, `sleep_state.py`, `review_packet.py`, `school_grader.py`, `conductor.py`, `board.py`, retention/sanitization code, and corresponding tests.

Before changing code, map each approved learning fact and named enhancement to current implementation, persistence, retrieval, and tests. Specifically inspect:

1. **Archival memory write/consolidation:** confirm the production task cycle writes a bounded consolidation under the correct cycle/session identity and that a later fresh cycle retrieves it. Do not mistake an existing helper or a test fixture for an operating loop.
2. **Learning-aware routing:** retain the existing competency/score and ACRouter combo feedback. Define the intended domain/difficulty/growth signal, safe cold-start, explanation record, and bounded promotion rule; test deterministically and do not let an LLM opinion mutate authority by itself.
3. **Tool/skill-use telemetry:** record what was actually invoked, not just what was offered. Redact prompt/repo content and credentials. Demonstrate that telemetry joins to the task/candidate and can be used by scoring/routing without fabricating missing usage.
4. **Asynchronous review/grading and dead-letter recovery:** inspect the existing teacher polling and grading queue before adding a worker. Wire a durable, idempotent consumer to the production lifecycle; a `grade()` error must not be acknowledged as success. Define retry limits, visible terminal dead-letter state, and operator recovery for both teacher review and grading jobs. Persist/checkpoint queue state across fresh checkout or use a separately proven durable store. Do not duplicate an already-working review path; add only missing durability and proof.
5. **Per-skill scoring and telemetry:** separate student skill outcomes from CTO/COO reviewer-lens scores. Define evidence-backed student skill identifiers and thresholds; persist/reload score history and expose it to an appropriate review/board surface. Add a normal-runtime producer for verified tool-use events and join them to the task/candidate; capability offers and judge-lens results are not proof of student tool use or skill mastery. Preserve overall score behavior and ensure unobserved skills remain unknown rather than zero/fabricated.
6. **History retention/compaction:** keep existing trajectory/consolidation/session caps where appropriate, then set explicit bounds by data class (grading jobs, compound learning, audit evidence, candidate state, trajectories, sessions, board/run projections). Ensure durable audit lineage survives compaction, explicitly include each required store in the authoritative checkpoint/backup, and test recovery after pruning.

**Verification:**

- Focused current suite including `tests/test_growth_tracker.py`, `tests/test_router_experience.py`, `tests/test_teacher_feedback.py`, `tests/test_consolidation_writer.py`, `tests/test_context_orchestrator.py`, `tests/test_compound_learning.py`, `tests/test_school_grader.py`, `tests/test_conductor_issue_path.py`, `tests/test_board.py`, `tests/test_sanitize_data.py`, and new end-to-end persistence/reload tests.
- A deterministic multi-cycle fixture proves recorded feedback and archived learning change the next task's available context/routing, while tool-use evidence reflects observed events only. Demonstrate actual retrieval/consumer wiring, not only helper existence or a fixture calling the helper directly.
- Grade-worker success, reported-error, process-interruption, replay, duplicate delivery, dead-letter recovery, and retention tests prove that failed jobs are not silently acknowledged; they preserve exactly one decision/score per task and retain required audit identity across a fresh checkout/restart.
- Skill tests distinguish student capability/skill outcomes from CTO/COO lens evidence; absent or unproven usage remains unknown/absent and cannot affect live routing.

### Phase 5 — Durable hosted pilot and recovery

**Touches:** `recovery.py`, `recovery_smoke.py`, `state_journal.py`, `merge_coordinator.py`, `.github/workflows/` or the selected hosted runner configuration, credential/secret handling, observability, `docs/setup/recovery.md`, `tests/test_recovery.py`, `tests/test_recovery_restore.py`, `tests/test_recovery_smoke.py`, lifecycle tests.

1. Define the provider-neutral deployment contract for the controlled pilot: process/service boundaries, durable state, restart policy, authentication and role authority, secret delivery, VM capacity/resource limits, monitoring, backups, and operator recovery. Select a concrete provider only when the deployment slice is authorized and required inputs are known.
2. Separate ephemeral task workspaces/VMs from durable task/candidate/review/score/memory/audit state. Persist operation IDs and idempotency keys before external PR/provider writes; reconcile after a crash without duplicate PRs or false success. Inventory actual checkpoint/backup coverage: the current workflow stages selected run, score, retry, crew, trajectory, and consolidation state, but not grading-queue, compound-learning, or candidate/evidence state.
3. Extend bootstrap/doctor/restore so a fresh hosted instance can recover Beads and required local/remote learning and evidence stores from their authoritative backup. Keep reports secret-free and distinguish unavailable credentials/services from successful restore. Treat the 2026-09-29 missing-`OMNIROUTE_BASE`/`GITHUB_TOKEN` restore as blocked, not successful recovery.
4. Add health/liveness checks, bounded task concurrency and per-task resources, VM cleanup/quarantine monitoring, failure metrics/alerts, and documented operator procedures. Do not claim a browser UI or multi-tenant service unless separately specified.
5. Pilot with a synthetic or explicitly authorized low-risk repository issue. Validate restart mid-task, relay outage, verifier outage, provider-write ambiguity, teacher outage/dead-letter, and restore. Capture exact issue/candidate/PR identity and teardown evidence.

**Verification:**

- `python3 -m pytest tests/test_recovery.py tests/test_recovery_restore.py tests/test_recovery_smoke.py tests/test_state_journal.py tests/test_merge_coordinator.py tests/test_lifecycle_states.py tests/test_observability.py -q`
- A clean hosted instance passes bootstrap/doctor/restore and completes a bounded end-to-end pilot; the task and its audit evidence remain recoverable after a process restart.
- Fault injection proves no false pass, no host execution fallback, no duplicate candidate PR, and visible/recoverable dead-letter state.
- The current 2026-09-29 restore report is treated as a blocker baseline until missing service/credential dependencies are safely configured and a fresh restore succeeds.

### Phase 6 — Acceptance rehearsal, documentation reconciliation, and close-out

**Touches:** `campus.md`, `README.md`, `docs/school-core-architecture.md`, `docs/school-core-scale-architecture.md`, `docs/orca-dispatch-architecture.md`, `docs/pipeline-explainer.md`, `docs/setup/recovery.md`, `docs/sdlc/loops.md` only if the loop contract itself changes, goal facts and Beads status.

1. Update behavior and security docs only from measured code/runtime evidence. State that Orca may remain a control/coordination/UI surface but does not run student coding commands; update outdated claims that Orca worktrees are the security boundary.
2. Correct `docs/pipeline-explainer.md` and `campus.md` operational status; list deferred/non-goal items explicitly. Preserve the SDLC distinction: outer loop authorizes and decomposes; inner loop validates and opens a review PR; merge remains human-controlled.
3. Run the full repository suite and applicable local gates. Check that docs, issue results, PR content, board, scores, and retained evidence use consistent repo/task/candidate identities.
4. Perform the controlled acceptance rehearsal against the selected repository, then configure a second repo without a code fork. Record the PR as open/reviewable, the teacher/operator decision, verification evidence, learning-state update, and VM teardown. Do not merge or push a goal-completion claim externally without separate authorization.
5. Close only Beads whose acceptance evidence has passed; leave externally blocked or deferred items open with the actual blocker and suggested next action.

**Verification:** all accepted facts have evidence links in the closure record; focused tests and full suite pass; one clean hosted rehearsal and a second-repo onboarding rehearsal pass; current documentation agrees with the measured runtime; no candidate was automatically merged.

## Checkpoints and sequencing rules

- **Checkpoint A — single repo:** Do not expand to multi-repo or hosted rollout until an exact source candidate can traverse trusted verification, candidate-bound teacher review, and candidate-bound PR publication in a hermetic test. PR provider failure or ambiguity must not close/process the issue. Do not claim operational completion without a controlled live rehearsal.
- **Checkpoint B — security:** Keep production coding dispatch disabled until the selected hosted per-task VM passes the lifecycle, host-canary, network, credential, artifact-validation, and no-fallback checks. Local VM proof alone is insufficient. Fail closed if the selected substrate cannot satisfy the threat model; never substitute host/Orca execution.
- **Checkpoint C — second repo:** Do not raise concurrency until one issue per repo can keep repo identities and trusted check policies separate. Multi-repo support does not require parallel dispatch.
- **Checkpoint D — learning:** Do not let new telemetry/routing mutate scores or privileges until source events, provenance, persistence across fresh checkout, and cold-start behavior are tested. Keep current CTO/COO lens aggregates advisory; they are not per-student skill scores.
- **Checkpoint E — hosted pilot:** Do not claim deployment readiness until restart/restore, secret-free health output, capacity limits, dead-letter handling, and cleanup are exercised.
- Each Bead follows the existing inner loop and ends with a PR/review decision and Beads state write. Failed steps loop back to that step; they do not skip verification or create an untracked shadow task list.

## Remaining decisions to resolve before the affected slice

1. **VM substrate and hosting:** Select a hosted per-task VM provider only when its lifecycle, network controls, and operational contract are verified. Do not treat “remote”, Docker, OpenHands Cloud, or a VM that hosts OpenHands as proof of a disposable VM per coding task. No provider is selected or deployment authorized.
2. **Network exceptions:** Default-deny and prebuilt offline toolchains are the baseline. If an implementation needs a narrow model/package endpoint or broker, present that exception for a separate decision; repository configuration cannot choose arbitrary egress.
3. **Durable state source:** Identify authoritative backup/restore for task journals, teacher decisions, learning stores, and candidate evidence before the hosted pilot.
4. **Retention and learning scope:** Choose measurable time/count/size bounds for each history category and specify which low-confidence teacher/model signals can influence routing versus remain advisory.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Bridge persists success before PR creation and closes/processes the issue after PR failure or ambiguity | Order durable success/close after candidate-bound PR reconciliation; test `None`, exception, timeout/ambiguous result, and safe replay. |
| Existing PR path publishes prose/patch files instead of source changes | Wire `candidate_pr` to exact source diff and test provider request against a temporary real Git repo. |
| Grading queue acknowledges a job after a returned grading error, and no scheduled consumer was found | Preserve the job until success or explicit dead-letter; wire and checkpoint a bounded worker with operator recovery. |
| Current direct-Orca fallback silently crosses the VM boundary | Remove fallback for coding tasks; add failure-injection tests asserting no host executor call. |
| VM isolation or relay behavior is assumed, not measured | Threat-model first; run host canary, credential, egress, and failure tests on the selected substrate. |
| Repo-supplied checks are mistaken for trusted school checks | Pin school policy outside the candidate and report repo checks separately. |
| Old docs claim Orca is execution truth-source | Reconcile docs only after tested VM behavior; mark remaining drift explicitly. |
| Multi-repo config leaks context, score, credentials, or PR destination | Carry canonical repo identity through every task artifact; two-repo negative tests. |
| Learning backlog overstates work already completed | Re-audit current code/tests before implementation and remove already-satisfied slices from the Beads graph. |
| Hosted pilot scope grows into SaaS/multi-tenancy | Keep one operator-controlled deployment; no customer tenancy, public SLA, or unrequested UI. |
| Earlier smoke results are misrepresented as full pipeline proof | Preserve each run's exact tested stages and require a new end-to-end acceptance rehearsal. |
| External writes or live issue changes happen without authorization | Unit tests use fakes; require exact, fresh authorization for a real issue, provider write, deployment, or merge. |
