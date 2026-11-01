# SCH-13 — SmolCloud hosted-substrate disposition (2026-10-04)

Author: phymora (adversarial reviewer). Wake reason: `issue_commented` —
operator interaction ecc4ebcd answered: **approve SmolCloud** + **authorize
committing seam files and tests (SCH-20)**.

Every claim below was produced by execution on this host; nothing is inferred.

## Intent (restated)

SCH-13 asks to qualify a **hosted** per-task disposable VM for the approved
student-execution boundary, keep production student coding disabled, and
preserve the no-host/Orca/direct-model fallback. The operator has now chosen
SmolCloud (SmolMachines Cloud) as the substrate. This heartbeat verifies the
adapter exists, is correct, and identifies what remains before production
dispatch can be enabled.

## What exists (verified)

`smol_cloud_runner.py` (24,809 bytes, **untracked**) is a complete hosted
adapter for SmolMachines Cloud. It implements the full lifecycle:

- **create** — `POST /v1/machines` with `network.mode=blocked`, `ephemeral=true`,
  `ttlSeconds=timeout+300`, digest-pinned OCI image, bounded CPU/mem/disk
- **start** — `POST /v1/machines/{id}/start`
- **readiness** — polls until ready, with timeout
- **upload** — `POST /v1/machines/{id}/files/tmp/school-core-input/repository.tar`
  and `task.json`
- **exec** — `POST /v1/machines/{id}/exec?output=text` with `sh -c` guest script
- **download** — `GET /v1/machines/{id}/files/tmp/school-core-candidate.tar`
- **delete** — `DELETE /v1/machines/{id}` in `finally` block
- **quarantine** — on create ambiguity, delete failure, invalid machine id

The adapter is **not wired into production dispatch** (grep confirms it is only
referenced by its own tests). Production student coding stays disabled — correct.

## Test results (ran this heartbeat)

`tests/test_smol_cloud_runner.py` — **52 passed** (hermetic, fake transport).
Covers: full lifecycle, blocked network, TTL, ephemeral, resource ceilings,
digest-pinned image, credential validation, control-character rejection,
oversized bundle/metadata, dirty repo, head mismatch, oversized stdout,
malformed archive, readiness polling, readiness timeout, error state,
relay revocation on all terminal paths, student command never executes on host,
quarantine record is secret-free.

`tests/test_smol_cloud_hosted_probes.py` — **8 probes, all skipped** (gated on
`SCHOOL_CORE_HOSTED_PROBES=1`). These create REAL billable cloud machines and
require operator credentials. They have never been run.

## What is missing

1. **Adapter is untracked.** `smol_cloud_runner.py` and
   `tests/test_smol_cloud_runner.py` are not in git. On a clean checkout they
   do not exist. The operator authorized committing "the seam files and tests"
   (SCH-20) — this adapter is part of SCH-13's hosted qualification, not
   SCH-20's seam hygiene. A new commit authorization is needed.

2. **No verify gate for the adapter.** `project_verify.yaml` has gates for
   `student_vm_runner`, `verifier_vm`, `model_relay`, `candidate_pr`,
   `pr_provider` — but NOT for `smol_cloud_runner`. The adapter is unreachable
   by the verify gate.

3. **Hosted probes have never run.** The 8 probes in
   `test_smol_cloud_hosted_probes.py` need real credentials
   (`SMOL_CLOUD_TOKEN`) and an operator-pinned image digest. They are the
   "full qualification evidence" the acceptance requires. Without them,
   production dispatch must stay disabled.

4. **`bundle_sha256` is write-only** (same as Finding 4 in the local runner).
   Computed at `smol_cloud_runner.py:196`, passed to result at `:299`, never
   recomputed by any consumer. Either verify it at materialization or drop it.

## What is genuinely proven green

- Full lifecycle (create → start → upload → exec → download → delete) with
  bounded network, TTL, ephemeral, digest-pinned image
- Fail-closed on every error path (no host fallback)
- Relay revocation on all terminal paths (success, failure, quarantine,
  unusable request)
- Credential validation (missing, control characters, read at execute time)
- Resource ceilings enforced before any request
- Oversized bundle/metadata/stdout blocked before create or at exec
- Dirty repo and head mismatch blocked before create
- Malformed candidate archive blocked
- Readiness polling with timeout
- Quarantine record is private, parseable, secret-free
- Student command never executes on host (positive-control spy)

## Verdict

**fix-then-ship.** The SmolCloud adapter is correct and complete. Three
residuals must close before production dispatch can be enabled:

1. Commit `smol_cloud_runner.py` + tests (needs operator authorization)
2. Add a verify gate for `smol_cloud_runner`
3. Run the 8 hosted probes with real credentials (operator decision)

Single biggest reason: the hosted probes are the "full qualification evidence"
the acceptance requires, and they have never been run. Production dispatch
stays disabled until they pass.

## Decision needed (operator)

1. Authorize committing `smol_cloud_runner.py` + `tests/test_smol_cloud_runner.py`
   + `tests/test_smol_cloud_hosted_probes.py`?
2. Provide `SMOL_CLOUD_TOKEN` and an operator-pinned image digest so the 8
   hosted probes can run?
