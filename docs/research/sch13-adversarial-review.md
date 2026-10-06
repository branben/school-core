# SCH-13 — Adversarial review of the disposable-VM student-execution seam

Author: phymora (adversarial review specialist). Every claim below was produced by
running the artifact, not by reading it. Repo: `/Users/brandonbennett/school-core`,
branch `main`, working tree dirty. Substrate under review: local SmolVM developer
proof (`student_vm_runner.py`, `verifier_vm.py`, `model_relay.py`, their tests,
`project_verify.yaml`). No hosted substrate is present and none was selected — see
"Scope limits".

## Intent (restated)

Qualify a per-task disposable guest for the approved student-execution boundary and
prove the fail-closed lifecycle, while production coding dispatch stays disabled.
This review asks the adversarial question that a green test run does not answer:
*does the seam actually run on the toolchain the project pins, on the repositories
it claims to target?* It does not.

## Headline

**The seam does not import on the project's own pinned interpreter (python 3.12),
the verify gate reports PASS on the non-importable module, and every real repository
worktree is rejected at candidate import.** Three findings, two of them blockers,
all reproduced by execution. The 66-test green run is real but runs only on an
interpreter the project does not pin, against synthetic archives that omit the
dotfiles a real `git archive` always contains.

| # | Severity | Finding | Evidence | Reproduced |
|---|----------|---------|----------|-----------|
| 1 | BLOCKER | `student_vm_runner.py` cannot be imported on py3.9/3.11/3.12 (missing `from __future__ import annotations`); `verifier_vm.py` fails transitively | `student_vm_runner.py:302` vs `:443`; siblings have the future import | `import` on 3.12 → `NameError`; on 3.9 → `TypeError` |
| 2 | BLOCKER | The verify gate is a false positive for this class — `compileall` passes on a module that cannot import | `project_verify.yaml` `core-python-compile` | `compileall` rc=0, `import` fails, same interpreter |
| 3 | MAJOR | Real repo worktrees are rejected: the archive importer blocks `.gitignore`/`.gitattributes`/`.gitmodules`, which `git archive` always includes | `student_vm_runner.py:532-540`; `:1189`; `:1143` | a git repo with tracked `.gitignore` → `BLOCKED` |
| 4 | MINOR | `bundle_sha256` is computed but never verified by any consumer | `student_vm_runner.py:1225-1228`, `:158` | grep: no comparison site |
| 5 | MINOR | `StudentTaskResult.guest_id` validates with the task-id regex (`sc-…` names can never match); no consumer | `student_vm_runner.py:174-175` vs `:1268` | regex mismatch |

## Finding 1 (BLOCKER) — the seam does not import on the pinned toolchain

`student_vm_runner.py` is the only new module in this seam without
`from __future__ import annotations` (`candidate_manifest.py:7`, `candidate_gate.py:3`,
`candidate_pr.py:8`, `verifier_vm.py:14` all have it).

At `student_vm_runner.py:302`:

```python
def durable_record_from_verified_candidate(verified: VerifiedStudentCandidate) -> DurableCandidateRecord:
```

evaluates `VerifiedStudentCandidate` at module scope, but that class is defined
later at `student_vm_runner.py:443`. Measured, `import student_vm_runner`:

```
python3.9  (/usr/bin/python3)              TypeError: unsupported operand type(s) for |: 'type' and 'type'  (line 86, Path | str)
python3.11 (/opt/homebrew/bin/python3.11)  NameError: name 'VerifiedStudentCandidate' is not defined         (line 302)
python3.12 (/opt/homebrew/bin/python3.12)  NameError: name 'VerifiedStudentCandidate' is not defined         (line 302)
python3.14 (/opt/homebrew/bin/python3.14)  IMPORT OK
```

Python 3.14 imports only because PEP 649 defers annotation evaluation; 3.9 fails at
the first PEP 604 union, and 3.11/3.12 fail at the forward reference. `verifier_vm.py`
imports from `student_vm_runner` (`verifier_vm.py:29-42`) and fails the same way.

Consequence for the test suite — collection, not execution:

```
python3.12 -m pytest tests/test_student_vm_runner.py --collect-only
  ERROR tests/test_student_vm_runner.py - NameError: name 'VerifiedStudentCandidate' is not defined
python3.9  -m pytest tests/test_student_vm_runner.py --collect-only
  ERROR tests/test_student_vm_runner.py - TypeError: unsupported operand type(s) for | ...
```

All four seam test files (`test_student_vm_runner.py`, `test_student_vm_artifact.py`,
`test_verifier_vm.py`, `test_student_verify_flow.py`) import `student_vm_runner`, so
none of them can even be collected on 3.9/3.11/3.12.

**Why this matters.** `project_verify.yaml` pins `python312`. CI (`.github/workflows/ci.yml:25`)
runs the matrix `["3.9", "3.11", "3.12"]`. The seam therefore does not run on any
interpreter the project declares support for. The "66 passed" run I produced was on
python3.14 only — the single interpreter where the module imports — and it is not
evidence that the seam works on the pinned toolchain.

**Minimal change.** Add `from __future__ import annotations` as the first import of
`student_vm_runner.py`. One line. (This also lets `Path | str` in signatures stand
under 3.9.)

## Finding 2 (BLOCKER) — the verify gate is a false positive

`project_verify.yaml`:

```yaml
verify:
  - name: core-python-compile
    cmd: python3 -m compileall -q *.py
```

`compileall` checks syntax only; it never imports. Measured on the same interpreter:

```
python3.12 -m compileall -q student_vm_runner.py   ->  rc=0   (PASS)
python3.12 -c "import student_vm_runner"           ->  NameError: name 'VerifiedStudentCandidate' is not defined
```

So the gate prints PASS on a module that cannot be imported. Every green
`core-python-compile` result for this file is unverified — the gate has no signal
on the defect in Finding 1 or Finding 3.

The gate's second line, `student-vm-runner-typecheck` (`mypy --strict …`), cannot be
shown to run here either: `mypy` is not installed on this host (`ModuleNotFoundError:
No module named 'mypy'`), and the flake that provisions `python312Packages.mypy` was
not executed for this review. I therefore do not credit that line with catching
anything. This is the same shape as the SCH-9 finding that shipped a verify-gate bug:
a gate that only ever passes launders an unverified claim into a green check.

**Minimal change.** Make the gate exercise the import path under the pinned
interpreter — `python3 -c "import student_vm_runner, verifier_vm, model_relay, candidate_pr"`
— in addition to (or instead of) `compileall`. An import check is stdlib-only and
fits the flake's network-less `verifyShell`.

## Finding 3 (MAJOR) — every real repo worktree is rejected at candidate import

`import_guest_candidate_archive` rejects any archive member whose path part is
`.gitignore`, `.gitattributes`, or `.gitmodules` (`student_vm_runner.py:532-540`):

```python
if any(
    part == ".." or part == ".git" or part == ".gitmodules"
    or part == ".gitignore" or part == ".gitattributes"
    for part in raw_parts
):
    raise StudentVMBlocked(f"archive member has unsafe path: {member.name!r}")
```

But the guest input is `git archive` of the base (`student_vm_runner.py:1189`) —
which includes tracked dotfiles — and the guest returns the whole extracted tree
(`student_vm_runner.py:1143`, `tar -cf - -C /workspace .`). Reproduced with a real
git repo whose base tracks `.gitignore` and `.gitattributes`:

```
git archive members: ['.gitattributes', '.gitignore', 'README.md', 'pkg', 'pkg/mod.py']
RUNNER _validate_archive: PASS (accepted)
IMPORT: BLOCKED: archive member has unsafe path: '.gitattributes'
```

Note the split: the runner's `_validate_archive` (`student_vm_runner.py:1099-1124`)
allows dotfiles, so the run gets as far as returning a result; the **importer**
then blocks. `git ls-files` confirms school-core itself tracks `.gitignore` and
`.gitmodules`, so the seam blocks on the project's own repository.

**Why this matters.** Any repository that tracks a `.gitignore` (i.e. almost every
repository) cannot produce a materialized candidate. The suite is green only because
no test archive contains a dotfile — `grep` for `gitignore|gitattributes|dotfile`
across the four seam test files returns nothing.

**Minimal change (choose one and test it).** Either (a) allow dotfiles as inert
data and ensure `_apply_artifact_and_commit`'s `git add --all`
(`student_vm_runner.py:683`) re-materializes them from content while never running
submodule init for `.gitmodules`; or (b) drop dotfiles at import under a documented,
tested policy. Either way, add a test whose base actually contains tracked dotfiles
and assert the materialized candidate matches the base content.

## Finding 4 (MINOR) — `bundle_sha256` is a dead field

`_stage_bundle` computes a digest of the input bundle (`student_vm_runner.py:1225-1228`)
and it is carried on `StudentTaskResult.bundle_sha256` (`:158`), but no consumer ever
compares it. `materialize_and_verify_student_result` checks `task_id`/`repository`/
`base_sha` (`:796-805`) and never the bundle digest. The digest is evidence that
cannot fail, so it is not evidence. Either verify it at materialization or drop it.

## Finding 5 (MINOR) — `guest_id` validation can never match

`StudentTaskResult.guest_id` is validated against `_TASK_ID_RE` (`:174-175`), a
lowercase `[a-z0-9-]` pattern, but the runner generates `f"sc-{task_id}-{uuid4hex[:8]}"`
(`:1268`) — always containing `sc-`. No production result can satisfy that validator;
it only holds in tests because they construct `StudentTaskResult` directly. Harmless
today (no consumer) but misleading. `VerifierEvidence` gets this right by requiring
the `scv-` prefix (`verifier_vm.py:169-172`).

## What is genuinely proven (so the review is not read as a blanket rejection)

I verified these by reading the code and the tests; the ones marked *(ran)* I executed.

- **Fail-closed lifecycle.** create/start/exec/delete with per-step blocked statuses;
  any non-zero control step raises `StudentVMBlocked`; there is no `except` that routes
  to a host executor (`student_vm_runner.py:1311-1374`).
- **Cleanup quarantine.** A failed `delete` writes a private (0600) `cleanup_quarantined`
  record and raises, preserving the original cause (`:1350-1366`; test `:531`). *(ran — suite)*
- **No host fallback.** `test_no_host_execution_fallback_on_vm_failure` monkeypatches
  `subprocess.run`/`Popen` with a positive-control spy and asserts the host ran only
  `git` plumbing and never the student command (`tests/test_student_vm_runner.py:708-758`). *(ran)*
- **Relay revocation on every terminal path** — success, failure, quarantine
  (`:652`, `:669`, `:688`). *(ran)*
- **Create never grants network or credentials.** `create_args` carries no `--net`,
  `--ssh-agent`, `--secret-*`, `--docker-socket`, or `--allow-system-mounts`
  (`:1293-1301`; asserted `tests/test_student_vm_runner.py:182-184`). *(ran)*
- **Bounded I/O.** Independent stdout/stderr caps; overflow kills the process group
  (`_bounded_process`, `:1036-1096`; tests `:195-244`). *(ran)*
- **Artifact validation.** Traversal, absolute paths, non-regular members, negative
  and oversized sizes, entry-count and size limits (`import_guest_candidate_archive`,
  `_validate_archive`). *(ran — synthetic archives)*
- **Identity binding.** The materializer rejects a task/repo/base mismatch
  (`:715-726`); the verifier evidence binds archive digest, candidate id, head SHA,
  and manifest digest (`verifier_vm.py:184-198`, `:407-418`). *(ran)*
- **Separate verifier guest.** `scv-` namespace, its own create/exec/delete, never the
  student machine (`verifier_vm.py:428-578`; test `:150`). *(ran)*

## Scope limits (what this review does NOT claim)

- **No hosted substrate exists in the tree.** No E2B, smol-cloud, or other hosted
  adapter is present; the runner shells `smolvm` only. Hosted qualification is open,
  exactly as the Beads record says. This review does not approve any provider.
- **The real-guest probes were not executed.** `tests/test_student_vm_guest_probes.py`
  (host-canary, credential, egress) skips unless `SCHOOL_CORE_SMOLVM_PACK` is set.
  Their logic is sound by inspection; their *results* are unverified here.
- **The live egress probe in the Beads notes (`--net` open, `--allow-cidr` isolation)
  is not re-measured.** My findings are about the seam's host-side code, not the
  substrate's network behavior.
- **mypy strict was not run** (mypy absent). I do not claim the type gate passes.

## The simpler alternative (mandatory pass)

The three fixes are one-line, one-line, and a policy decision. The larger observation:
the boundary is implemented as one very large module (`student_vm_runner.py`, ~1,400
lines) with a nested lifecycle and a nine-field frozen result type, tested exclusively
against synthetic archives and a fake process runner. The cheapest hardening available
is not more abstraction — it is to make the existing seam run on the pinned interpreter
and against a real repository archive, then re-run the same suite. Do that before
adding any hosted adapter; the hosted path will inherit Finding 3 and Finding 1 verbatim.

## Verdict

**fix-then-ship.** Biggest reason: the seam cannot be imported on the project's pinned
interpreter and rejects the repositories it is meant to target, so none of the
qualification evidence is currently reachable on the declared toolchain — the fixes are
one future import, one import-based gate line, and one dotfile policy with a real-repo
test.

## Corroborated (verified myself, not taken on trust)

- `import student_vm_runner` fails on 3.9 (`TypeError`) and 3.11/3.12 (`NameError`),
  succeeds on 3.14 — measured on four interpreters.
- `python3.12 -m compileall -q student_vm_runner.py` exits 0; `python3.12 -c "import
  student_vm_runner"` fails — the gate's check is a false positive.
- pytest collection of `tests/test_student_vm_runner.py` ERRORs on 3.9/3.11/3.12.
- A real git repo with tracked `.gitignore`/`.gitattributes` imports as
  `BLOCKED: archive member has unsafe path: '.gitattributes'`; `_validate_archive`
  passes the same bytes.
- `git ls-files` shows school-core tracks `.gitignore` and `.gitmodules`.
- The seam suite is green on 3.14: `66 passed, 3 skipped in 23.47s`.
- No test archive contains a dotfile (grep across the four seam test files).
- `student_vm_runner.py` and `verifier_vm.py` are untracked in git (`??`), as are all
  four seam test files.

## Unproven — I do not have evidence

- Whether `mypy --strict` (flake python312Packages.mypy) would catch Finding 1 — mypy
  is not installed here and I did not run the flake.
- Whether `verify_gate.py` would execute the seam tests at all in a real issue cycle,
  or only the `project_verify.yaml` commands. I did not trace that wiring.
- The real-guest probe results (host-canary, credential, egress) — skipped without a
  pinned pack; not re-run.
- Whether CI currently runs at all on this tree — the seam files are untracked, so CI
  (which runs on pushed commits) has never seen them.
