# SCH-27 — Verify bundle_sha256 at materialization

Adversarial review of the `bundle_sha256` write-only residual (SCH-13 Finding 4, MINOR).

Verdict: **resolved**. The digest is now verified at materialization time.

## Problem

`StudentTaskResult.bundle_sha256` was computed by both producers (`SmolCloudRunner._stage_bundle` and `SmolVmRunner._stage_bundle`) but never recomputed by any consumer. `materialize_and_verify_student_result` validated task_id/repository/base_sha identity but never re-hashed the uploaded bundle. The only test asserted the producer's own returned value — a self-referential check.

## Fix

### 1. Carry the bundle inputs in the result

Added `repository_tar: bytes` and `task_json: bytes` fields to `StudentTaskResult` (student_vm_runner.py:159-160). Both producers now populate them:

- `SmolCloudRunner._execute` (smol_cloud_runner.py:370-371): passes the staged `repository.tar` and `task.json` bytes.
- `SmolVmRunner._execute` (student_vm_runner.py:1349-1350): reads the staged files back and passes them.

### 2. Recompute at materialization

`materialize_and_verify_student_result` (student_vm_runner.py:820-826) now:
1. Rejects results with empty `repository_tar` or `task_json`.
2. Recomputes `sha256(repository_tar + task_json)`.
3. Compares to `result.bundle_sha256`; raises `StudentVMBlocked` on divergence.

### 3. Divergence-catching test

`test_bundle_sha256_divergence_is_rejected` (tests/test_student_vm_artifact.py:522-581):
- Constructs a result with `bundle_sha256="e" * 64` (wrong) and correct `repository_tar`/`task_json`.
- Asserts `materialize_and_verify_student_result` raises `StudentVMBlocked` matching `"bundle_sha256"`.
- Reconstructs with the correct digest and asserts materialization succeeds.

This test FAILS if the verification is removed — it is not self-referential.

## Verification

- `pytest tests/test_student_vm_artifact.py` — 9 passed
- `pytest tests/test_smol_cloud_runner.py` — 78 passed
- `pytest tests/test_student_vm_runner.py` — 26 passed, 1 skipped
- Combined: 113 passed, 1 skipped
- `python3 -m compileall` — clean

## Files changed

- `student_vm_runner.py` — dataclass fields + validation + materialization check
- `smol_cloud_runner.py` — populate new fields
- `tests/test_student_vm_artifact.py` — updated existing tests + new divergence test

## Production impact

None. Production student dispatch remains disabled. The change is purely defensive: the verifier now catches a producer/consumer mismatch that was previously invisible.
