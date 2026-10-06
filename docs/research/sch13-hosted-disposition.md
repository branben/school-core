# SCH-13 — Hosted-substrate disposition review (2026-10-02)

Author: phymora (adversarial reviewer). Wake reason: `finish_successful_run_handoff`.
This heartbeat re-ran the artifact under the run's real environment. One prior
finding is **retracted**; one new structural finding is raised. Every claim below
was produced by execution on this host; nothing is inferred from memory.

## Intent (restated)

SCH-13 asks to qualify a **hosted** per-task disposable VM for the approved
student-execution boundary, keep production student coding disabled, and preserve
the no-host/Orca/direct-model fallback. The acceptance requires the *hosted*
lifecycle (create/start/run/export/verify/destroy) plus tests for no-fallback,
host isolation, forbidden-egress denial, artifact/identity validation,
timeout/death/cleanup and quarantine.

## Blocker A — no hosted substrate exists in the tree (unchanged)

Scoped search of the seam and the boundary doc for any hosted adapter
(`e2b|smol-cloud|fly.io|firecracker|microvm|gvisor|daytona|remote substrate`)
returns **zero hits** in `student_vm_runner.py`, `verifier_vm.py`,
`model_relay.py`, `docs/student-vm-boundary.md`. The runner shells out to a local
`smolvm` binary (`student_vm_runner.py:1296,1304,1306,1311`). Hosted
qualification cannot be produced without (a) an operator-chosen substrate and
(b) an adapter that does not exist. This is an operator/provider decision, not a
reviewer decision. **Blocked on a named human.**

## RETRACTION — prior Finding 5 was wrong

My SCH-13 review (and the post-SCH-18 review) claimed:

> `StudentTaskResult.guest_id` is validated against `_TASK_ID_RE` but the runner
> generates `sc-…` ids that can never match, so no production result can satisfy
> the validator.

**That is false.** `_TASK_ID_RE = ^[a-z0-9][a-z0-9\-]{0,99}$`
(`student_vm_runner.py:54`). The generated id is
`f"sc-{request.task_id}-{uuid.uuid4().hex[:8]}"` (`:1270`). Measured:

```
task_id=sjv-7   guest_id=sc-sjv-7-<8hex>    len=17  match=True
task_id=task-1  guest_id=sc-task-1-<8hex>   len=18  match=True
```

The regex permits lowercase letters, digits and hyphens, so every ordinary
`sc-…` id matches. The finding was **wrong as stated**; I retract it. The only
true residual is a length edge: a 100-char `task_id` passes the request validator
(`^[a-z0-9][a-z0-9\-]{0,99}$` allows len 1–100) but yields a 112-char `guest_id`
that fails `_TASK_ID_RE` at `:176`. That is a **nit** (unreachable with real
ids), not the blocker I claimed.

## Finding 4 (MINOR, unchanged) — `bundle_sha256` is write-only

`bundle_sha256` is computed at `:1290` (`self._stage_bundle`) and passed into the
result at `:1338`; `:186` validates only its 64-hex **format**. No consumer
recomputes or compares it. `materialize_and_verify_student_result` (`:777`)
checks `task_id`/`repository`/`base_sha` but never the bundle digest. The digest
is evidence that cannot fail. Either verify it at materialization or drop it.

## Finding N1 (structural) — the verify gate breaks on a clean checkout

`project_verify.yaml:31` adds:
`python3 -c "import student_vm_runner, verifier_vm, model_relay, candidate_pr"`.
The first two modules are **untracked** in git
(`git ls-files` → `student_vm_runner.py NO`, `verifier_vm.py NO`;
`model_relay.py`/`candidate_pr.py` are tracked). Reproduced by extracting
`git archive HEAD` into an empty dir: the import fails with
`ModuleNotFoundError: No module named 'student_vm_runner'`.

Consequence: on any clean checkout / CI / fresh clone, `core-python-import` (and
`student-vm-runner-typecheck`) fail with a *missing file*, not a code defect.
The SCH-18 "false-positive closed" claim holds **only in this dirty working
tree**. The seam files must be committed before the gate is meaningful.

## Finding N2 (major, test-environment) — the 9 seam failures are an ambient-env hazard

The 9 failing seam tests fail with
`fatal: empty ident name (for <>) not allowed` (exit 128). I isolated the cause
by running the matrix:

```
case                                        result
wrapper git in PATH + empty GIT_AUTHOR_*    9 failed
wrapper git removed + empty GIT_AUTHOR_*    same failures (module-level git config calls also fail)
wrapper git removed + GIT_AUTHOR_* unset    12 passed (artifact+verify_flow)
full 5-file seam suite, env neutralized     47 passed, 9 skipped
```

Root cause: the Paperclip runtime exports **`GIT_AUTHOR_NAME=''`,
`GIT_AUTHOR_EMAIL=''`, `GIT_COMMITTER_NAME=''`, `GIT_COMMITTER_EMAIL=''`** into
the agent's environment. Git honours an *empty* (set) `GIT_AUTHOR_NAME` as an
explicit empty identity and refuses to commit; it only falls back to user.name
when the variable is **unset**. The seam tests' own git helpers
(`tests/test_student_verify_flow.py:34-39`, `tests/test_student_vm_artifact.py`)
and the module-level `git config` calls inherit these empty vars and fail before
any runner logic runs.

This is not a product-code defect (the runner sets its own identity via
`_git_env()` and, when the env is clean, the full materialize-and-commit flow
passes). But the seam suite does not isolate its git identity, so it is
**green in a clean shell and red under the agent runtime** — a reproducibility
trap for every CI/Paperclip run. Fix: the test git helpers should set
`user.name`/`user.email` per invocation (or the suite should neutralize the
`GIT_*` identity vars), so the tests are hermetic against the ambient env.

## What is genuinely proven green (ran this heartbeat)

- Seam suite on python 3.9.6 with a neutralized git env: **47 passed, 9 skipped**
  (the 9 skips are the real-guest probes gated on `SCHOOL_CORE_SMOLVM_PACK`).
- Fail-closed lifecycle, no-host-execution-fallback (positive-control spy),
  relay revocation on all terminal paths, create grants no `--net`/`--ssh-agent`/
  `--secret-*`, bounded stdout/stderr, traversal/oversize artifact rejection,
  identity binding, separate `scv-` verifier guest — all covered by the 47.
- The new `core-python-import` gate is stricter than `compileall`: I reproduced a
  module (`x: Later = 1; class Later`) where `compileall` PASSes and the import
  gate FAILs on 3.12. The false-positive class is genuinely closed for 3.12.

## Scope limits (not claimed)

- No hosted substrate; hosted qualification not attempted.
- Real-guest probes skipped (`SCHOOL_CORE_SMOLVM_PACK` unset).
- `core-python-import` runs under python 3.12 only (verifyShell). It does **not**
  catch a PEP-604 `X | Y` annotation that is valid on 3.12 but a `TypeError` on
  3.9 (reproduced). The CI matrix includes 3.9, so this class is still uncovered
  by the gate — minor, because `from __future__ import annotations` is now present.
- `mypy --strict` not run (not installed on this host).
- Seam files remain untracked; nothing was committed.

## Verdict

**blocked.** Two independent blockers: (A) no hosted substrate exists and no
provider has been approved — an operator decision; (N1) the seam files are
untracked, so the new verify gate and the whole qualification evidence are
unreachable on a clean checkout. Finding 5 retracted; Finding 4 minor.

Single biggest reason: the deliverable (hosted qualification) requires an
operator-chosen substrate that does not exist in the tree, and the supporting
evidence is not committed.
