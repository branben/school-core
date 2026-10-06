# SCH-18 — Seam import failure, dotfile archive rejection, false-positive gate: fixes + verification

Author: phymora. Child of SCH-13. Every claim below was produced by running the
artifact on the pinned interpreters, not by reading it. Repo: `school-core`, branch
`main`, working tree dirty. Evidence for the defects: `docs/research/sch13-adversarial-review.md`.

## Intent

Make the local SmolVM student-execution seam import on the project's pinned
interpreter (python 3.12), stop rejecting every real repository worktree at
candidate import, and make the verify gate fail — not pass — when a seam module
cannot import. No production dispatch enablement; no provider/GitHub writes.

## Changes

| # | File | Change |
|---|------|--------|
| 1 | `student_vm_runner.py:1` | Add `from __future__ import annotations` (fixes BLOCKER 1). |
| 2 | `project_verify.yaml:30` | Add gate `core-python-import`: `python3 -c "import student_vm_runner, verifier_vm, model_relay, candidate_pr"` (fixes BLOCKER 2). |
| 3 | `student_vm_runner.py:541` | Archive importer now blocks only `..` and `.git`; tracked dotfiles (`.gitignore`/`.gitattributes`/`.gitmodules`) are admitted as inert data (fixes MAJOR 3). |
| 4 | `tests/test_student_vm_artifact.py:439` | New test `test_import_accepts_tracked_dotfiles_from_real_base`: real git base tracking all three dotfiles, real `git archive` → guest-style re-tar, import + materialize, assert content parity. |
| 5 | `issue_bridge.py:19` | Add `from __future__ import annotations` — same defect class, same change set (`_build_trusted_verification_evidence(verify_result: Any \| None, …)` at module scope). Without it the 3.9 matrix cannot import `issue_bridge` via `tests/conftest.py`. |

### Dotfile policy — choice and justification (option (a))

Chosen: **allow dotfiles as inert data files.** Rationale, verified:

- `git archive` of any real base includes tracked dotfiles (`student_vm_runner.py:1189`),
  and the guest returns the whole extracted tree (`tar -cf - -C /workspace .`,
  `student_vm_runner.py:1143`). Rejecting them blocks every real repository — including
  school-core itself, which `git ls-files` shows tracks `.gitignore` and `.gitmodules`.
- `_apply_artifact_and_commit` (`student_vm_runner.py:656`) re-copies every artifact file
  **from content** and runs `git add --all` (`:683`), then asserts `git ls-files` equals
  the artifact file list (`:684-686`). Dotfiles are therefore re-materialized from their
  bytes, not trusted from the archive.
- No submodule init is ever run: `grep -n submodule` across `student_vm_runner.py`,
  `verifier_vm.py`, `candidate_manifest.py`, `candidate_gate.py`, `candidate_pr.py`
  returns nothing. A tracked `.gitmodules` is inert data on the host.
- `.git` and `..` remain blocked — the traversal/executable-VCS hazards.

## Verification (executed)

Acceptance for SCH-18:

1. **`python3.12 -c "import student_vm_runner, verifier_vm"` succeeds.** ✅
   Also succeeds on 3.9.6 and 3.11.15 (previously `TypeError`/`NameError`).

2. **pytest collection of the four seam files under py3.9/3.11/3.12.** ✅ 50 tests
   collected on each of 3.9.6 / 3.11.15 / 3.12.13 (previously all four ERRORed on
   collection).

3. **The gate fails when a seam module cannot import.** ✅
   `_discover_commands` loads `core-python-import`; the import command exits 0 on the
   fixed tree and exits 1 on a non-importable module. `compileall` exits 0 on that same
   broken module — the false positive the new gate closes.

4. **Tracked `.gitignore`/`.gitattributes`/`.gitmodules` base imports cleanly and the
   materialized candidate matches base dotfile content.** ✅
   `test_import_accepts_tracked_dotfiles_from_real_base` passes on 3.9.6 and 3.12.13.
   Before fix 3 the same archive was rejected: `BLOCKED: archive member has unsafe path:
   '.gitattributes'`.

5. **No production dispatch enablement; no provider/GitHub writes.** ✅ Only the future
   import, the archive path policy, the gate config, the new test, and the `issue_bridge`
   future import changed. No dispatch flag flipped, no provider call added.

Full seam suite: **47 passed, 3 skipped** on python 3.12.13 (pinned) and on python 3.9.6.
`tests/test_issue_bridge.py`: **124 passed** on 3.12.13 after the `issue_bridge` fix.

## Residual / not claimed

- The four seam files and `student_vm_runner.py` remain **untracked in git** (`??`); the
  fixes are on disk, not committed. Committing/pushing is out of scope for this child.
- `mypy --strict` (gates 3 and 4) was not run here — mypy is not installed on this host;
  it is provisioned by the flake. Not credited with catching anything.
- The real-guest probes (`tests/test_student_vm_guest_probes.py`) still skip without
  `SCHOOL_CORE_SMOLVM_PACK`; not re-run.
- Findings 4 and 5 of the SCH-13 review (`bundle_sha256` dead field; `guest_id` regex
  mismatch) were not in this child's acceptance and remain open.

## Verdict

**fix-then-ship → verified.** All four SCH-18 acceptance criteria reproduced green on the
pinned toolchain; the seam imports and runs on every interpreter the project declares.
