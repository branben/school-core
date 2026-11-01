# SCH-13 — Hosted adapter commit + qualification disposition

Adversarial review of the SmolCloud hosted adapter after the operator decision on
interaction `ecc4ebcd` (`substrate=smolcloud`, `commit_seam=authorize_commit`,
resolved 2026-10-02T23:55:49Z).

Verdict: **fix-then-ship** for the code; **blocked** for hosted qualification.
Production student coding stays disabled.

## What the operator authorized

- Substrate: **SmolCloud / hosted smolvm**.
- Commit: the untracked student-VM seam + tests.

The authorization covers committing the qualification **evidence**. It does not
deliver the provider credential or a pinned image digest, and it does not
authorize enabling production dispatch.

## What was committed (this heartbeat)

Commit `975dfcb` on `school/sch-20-seam-hygiene` (not pushed):

| Path | Lines | Note |
|---|---|---|
| `smol_cloud_runner.py` | 581 | hosted adapter, not wired into dispatch |
| `tests/test_smol_cloud_runner.py` | 881 | 52 hermetic tests |
| `tests/test_smol_cloud_hosted_probes.py` | 548 | 8 opt-in hosted probes |
| `project_verify.yaml` | +31 | `core-python-import` (SCH-18) + `smol-cloud-runner-import` (this change) |

Clean-checkout proof (`git archive HEAD` into an empty dir):
adapter + tests present, `python3 -c "import smol_cloud_runner, student_vm_runner"`
succeeds, `pytest tests/test_smol_cloud_runner.py` -> **52 passed**.

## Residuals

### RESOLVED — adapter untracked (was residual 1)

`smol_cloud_runner.py` and both test files were untracked (`git ls-files` -> NO).
Now committed; reachable on a clean checkout. Verified by execution.

### RESOLVED — no verify gate for the adapter (was residual 2)

`project_verify.yaml` had gates for `student_vm_runner`, `verifier_vm`,
`model_relay`, `candidate_pr`, `pr_provider` — not `smol_cloud_runner`. Added
`smol-cloud-runner-import` (`python3 -c "import smol_cloud_runner"`), stdlib-only,
so the adapter is reachable by the gate on a clean checkout.

### RESOLVED — N1 (seam unreachable on clean checkout)

The seam files are tracked (`git ls-files`: `student_vm_runner.py`,
`verifier_vm.py`, `model_relay.py`, `candidate_pr.py`, `pr_provider.py`). Clean
checkout import of the seam succeeds.

### OPEN (BLOCKER) — hosted probes have never run

The 8 probes in `tests/test_smol_cloud_hosted_probes.py` are gated on
`SCHOOL_CORE_HOSTED_PROBES=1` and skip in every default run (verified: 8 skipped
on 3.9.6). They are the "full qualification evidence" the acceptance requires.

They cannot run now:

- `.env` has `SMOL_CLOUD_TOKEN=` with an **empty value** (key present, length 0).
- `SCHOOL_CORE_SMOL_CLOUD_IMAGE` is **absent** from `.env`; the probe requires a
  digest-pinned reference (`alpine:3.21@sha256:<64-hex>`), `_IMAGE_DIGEST_RE`
  (`smol_cloud_runner.py:38`).

Blocker owner: **operator**. Action: append `SMOL_CLOUD_TOKEN` to `.env`
(never in chat) and set `SCHOOL_CORE_SMOL_CLOUD_IMAGE` to a digest-pinned
reference; then authorize the probe run.

### OPEN (MINOR) — `bundle_sha256` is write-only

Computed in `_stage_bundle` (`smol_cloud_runner.py:196`, digest of
`repository.tar` + `task.json` at `:412-418`), passed to the result at `:299`,
format-checked in the dataclass (`student_vm_runner.py:186`). No consumer
recomputes it: `materialize_and_verify_student_result` never compares it, and the
only test (`tests/test_smol_cloud_runner.py:289-308`) asserts the producer's own
value — a self-referential check, not an independent verification.

Minimal change: in the verifier, recompute `sha256(repository.tar || task_json)`
from the bytes the guest actually received and compare to `result.bundle_sha256`,
or drop the field. This is the artifact-identity half of acceptance item 5.

## What is genuinely proven green (ran this heartbeat)

- Adapter suite: **52 passed** on py3.9.6 and py3.14 (hermetic, fake transport).
- Full seam suite (5 files) on py3.9.6, env-neutralized: **99 passed, 3 skipped**.
- Adapter is not wired into dispatch (`grep` -> only its own tests reference it).
- Clean-checkout import + suite for the committed evidence.

## What is NOT claimed

- Hosted qualification is not attempted; no billable machine was created.
- The 8 probes have never executed.
- No provider/GitHub write anywhere in this change.
- `bundle_sha256` is not independently verified.
- The branch is committed, not pushed; no PR opened.
