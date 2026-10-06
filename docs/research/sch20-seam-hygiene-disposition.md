# SCH-20 — Seam hygiene disposition (2026-10-02)

Author: phymora (adversarial reviewer / verification agent). Wake reason:
`issue_assigned`. This heartbeat executed both blockers end-to-end. Every
claim below was produced by execution on this host; nothing is inferred.

## Intent (restated)

Make the student-VM seam reproducible on a **clean checkout** and under the
**agent runtime**:

- N1 — the verify gate must be reachable after `git archive HEAD` (the seam
  files must be tracked).
- N2 — the seam suite must report the same result with the runtime's empty
  `GIT_AUTHOR_*` set and with it unset.

## N1 — FIXED (commit `b340510`)

Reproduced first: `git archive HEAD` into an empty dir →
`ModuleNotFoundError: No module named 'student_vm_runner'` (exit 1);
`student_vm_runner.py`/`verifier_vm.py` were `git ls-files` → NO.

Commit `b340510` (`school/sch-20-seam-hygiene`) tracks the full closure the
gate needs:

```
A pr_provider.py            (339)
A student_vm_runner.py     (1395)
A verifier_vm.py            (578)
A tests/test_student_vm_runner.py       (758)
A tests/test_student_vm_artifact.py     (509)
A tests/test_student_verify_flow.py     (260)
A tests/test_verifier_vm.py             (276)
A tests/test_student_vm_guest_probes.py (265)
M candidate_pr.py           (24)  -- ProviderWriteError seam pr_provider imports
M tests/conftest.py         (63)  -- N2 fix (below)
10 files changed, 4461 insertions(+), 6 deletions(-)
```

`pr_provider.py` was **also untracked** and is referenced by
`project_verify.yaml`'s `pr-provider-typecheck` gate; `pr_provider.py` imports
`ProviderWriteError` from `candidate_pr.py`, which did not exist at HEAD
(added in the working tree, additive only — no removals). Both had to ship
with the commit or the gate stays broken.

**Acceptance (1) — PASS.** `git archive b340510` into a clean dir:

```
core-python-compile  (compileall -q *.py)                          exit 0
core-python-import   (import student_vm_runner, verifier_vm,
                      model_relay, candidate_pr)                   exit 0
```

## N2 — FIXED, root cause corrected

**The issue's prescribed fix is insufficient.** The issue says "make the test
git helpers set user.name/user.email per invocation." Measured, that does not
work under the runtime:

```
case                                                     exit
wrapper git + local `git config user.name` only          128  (D1)
wrapper git + `git -c user.name` override                128  (D2)
wrapper git + explicit GIT_AUTHOR_* env                  128  (D3)
real git   + explicit GIT_AUTHOR_* env                     0  (D4)
real git   + `git -c user.name` override                 128  (D5)
```

The real cause is two stacked hazards:

1. The runtime exports `GIT_AUTHOR_NAME=''`, `GIT_AUTHOR_EMAIL=''`,
   `GIT_COMMITTER_NAME=''`, `GIT_COMMITTER_EMAIL=''`, `GIT_CONFIG_COUNT=''`
   **empty-but-set**. Git honours an empty (set) identity and refuses:
   `fatal: empty ident name (for <>) not allowed`.
2. The runtime puts a Node **git credential-broker wrapper** first on PATH
   (`/…/paperclip-github-runtime/<run>/git`, a `#!/usr/bin/env node` script).
   The wrapper **deletes** `GIT_AUTHOR_*` from the child env and re-sets them
   to `''`, repopulating them only when its GitHub broker answers. It does this
   even when the caller passes an explicit `GIT_AUTHOR_*` or a
   `git -c user.name` override — so D2/D3 die. Local `user.name` config also
   loses (D1) because the empty (set) env var outranks config.

This reaches **product** code, not just tests: `student_vm_runner._git_env()`
(`:621-633`) copies `os.environ["PATH"]` verbatim (`:624`), so the runner's own
`_apply_artifact_and_commit` (`:690`) resolves `git` to the wrapper. Measured
through the product helper:

```
_git_env() only (what tests produce)               exit 128  empty ident
_git_env() + broker URL+TOKEN                      exit 0
_git_env() + PAPERCLIP_API_URL only                exit 128
```

`_git_env()` sets a correct explicit identity, but the wrapper re-zeroes it
whenever the broker is unavailable — the normal offline/CI/verifyShell case.

**Fix (in `tests/conftest.py`, autouse):** clear the empty identity vars and
drop the broker wrapper from PATH so `git` resolves to the real binary. The
detector reads the first bytes of each `PATH` entry's `git`; a real git is a
compiled binary, the shim is a `#!…node` script. Both mutations are
`monkeypatch`-scoped and auto-undone per test.

**Acceptance (2) — PASS.** Full 5-file seam suite, clean checkout of `b340510`:

```
runtime env (empty GIT_AUTHOR_* SET, wrapper in PATH)   47 passed, 9 skipped
neutralized (GIT_* UNSET, wrapper removed)              47 passed, 9 skipped
```

Identical. Before the fix the runtime env was **9 failed, 38 passed**; the 9
failures were exactly `fatal: empty ident name` (one per real-git commit site).

## Scope / retraction discipline

- N2's stated root cause ("tests' git helpers inherit the empty vars") is
  **incomplete** — the empty vars alone do not explain the failure when the
  wrapper is removed and local config is set (D1 = 128). The PATH wrapper is
  the dominant cause. Recorded here so the retraction is on the record.
- `tests/test_candidate_pr_gate.py:80` fails **collection** on python 3.9
  (`TypeError: unsupported operand type(s) for |: '_ProtocolMeta' and
  'NoneType'` — PEP 604 `VerificationEvidence | None` at module scope, no
  `from __future__ import annotations`). It is **untracked, not in this
  commit, and not referenced by `project_verify.yaml`**. Pre-existing, out of
  SCH-20 scope; flagged for a separate ticket.
- Not run on this host: `mypy --strict` and the py3.12 gate (neither `mypy`
  nor `nix`/`python3.12` installed). Those gates run in `verifyShell`; the
  committed files parse under py3.9 `ast` and carry
  `from __future__ import annotations`.
- No push. Commit is local on `school/sch-20-seam-hygiene`.

## Verdict

**fix-then-ship → shippable on the branch.** Both acceptance criteria pass
against the committed revision. Remaining action is a human/owner decision:
push `school/sch-20-seam-hygiene` and open the PR (or fast-forward `main`).

Single biggest residual risk: the N2 fix hardens the **test** suite, but the
product `_git_env()` (`student_vm_runner.py:624`) still copies the ambient
PATH and therefore still resolves to the broker wrapper when the broker is
unavailable. In a real hosted/verifyShell run that has no broker, the runner's
own commit step will hit the same exit-128. The seam tests pass because
conftest repairs PATH for the whole process; the product path is untouched.

---

## Addendum — `_git_env()["PATH"]` fix, differential probe (2026-10-03)

Author: lucas (Assistant Director), at Brandon's request. Recorded so the next
reviewer does not have to re-derive this, and so the two claims below are not
conflated again.

### The residual risk above is REAL — and the fix closes it

Working-tree change (uncommitted at time of writing), one line,
`student_vm_runner.py:624`:

```diff
-        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
+        "PATH": "/usr/bin:/bin",
```

Probed by invoking the **product** helper directly — not the suite — against a
node shim at `/tmp/brokerbin/git` that reproduces the wrapper's documented
behaviour (`tests/conftest.py:12-29`): it deletes `GIT_AUTHOR_*` /
`GIT_COMMITTER_*` from the child env and re-sets them to `''`, repopulating
only when its broker answers. Detected as a wrapper by
`conftest._is_git_broker_wrapper` → `True`.

Ambient env hostile: `GIT_AUTHOR_NAME= GIT_AUTHOR_EMAIL= GIT_COMMITTER_NAME=
GIT_COMMITTER_EMAIL= GIT_CONFIG_COUNT=` (empty-but-set), wrapper first on PATH.

| Arm | `_git_env()["PATH"]` | `git init` | `git add` | `git commit` |
|---|---|---|---|---|
| with fix | `/usr/bin:/bin` | 0 | 0 | **0** |
| fix reverted | `/tmp/brokerbin:…` (ambient) | 0 | 0 | **128** |

`git commit` stderr in the reverted arm:
`fatal: empty ident name (for <>) not allowed`.

This is the exit-128 the residual-risk note predicted, reached through the
product path. **The fix is correct and is not cosmetic.**

### The seam suite CANNOT evidence this fix — do not use it as proof

Suite runs use the canonical 5-file set named at lines 30-34
(`test_student_vm_runner`, `test_student_vm_artifact`,
`test_student_verify_flow`, `test_verifier_vm`, `test_student_vm_guest_probes`),
`-p no:randomly`, empty-but-set `GIT_AUTHOR_*`:

| Configuration | Result |
|---|---|
| hostile env, **with** fix | 47 passed, 9 skipped |
| hostile env, **fix reverted** | 47 passed, 9 skipped |
| hostile env **+ broker wrapper on PATH** | 47 passed, 9 skipped |
| wrapper on PATH, `hermetic_git_identity` neutralized, fix reverted | **8 failed**, 19 passed, 1 skipped, 28 errors |
| wrapper on PATH, `hermetic_git_identity` neutralized, **fix restored** | **8 failed**, 19 passed, 1 skipped, 28 errors |

Reading: the autouse fixture at `tests/conftest.py:32-64` deletes the empty
identity vars and drops the broker wrapper from PATH for the whole process
before any test runs. With it active, `:624` is inert — both arms green. With
it neutralized, `:624` is *also* inert — both arms equally red, because the
failing tests (`test_student_vm_artifact.py`, 8 of them) drive bare
`git -C … commit` in their own setup rather than going through `_git_env()`
(confirmed: `CalledProcessError` on `['git', '-C', …, 'commit', '-qm', 'base']`,
stderr `fatal: empty ident name`).

**Conclusion: suite green is not evidence for this line, in either direction.**
The only instrument that discriminates is a direct product-helper probe with a
wrapper shim. Keep the existing `conftest.py` fixture — it is a correct and
independent hardening — but do not let it be read as coverage of `:624`.

### Correction to a prior skip-count claim

A report stated the seam suite now yields `47 passed, 3 skipped` versus a
`47 passed, 9 skipped` baseline, and offered the 6-skip delta as acceptance
evidence for this change. **Not reproducible on this host (2026-10-03).** Every
configuration above measures 9 skips. The acceptance-(2) figures at lines
106-107 (`47 passed, 9 skipped`, identical hostile vs neutralized) were already
satisfied by commit `b340510` plus the conftest fixture; this change did not
move them. The 9 skips are all gated on `SCHOOL_CORE_SMOLVM_PACK`
(`test_student_vm_runner.py:74`, `test_student_vm_guest_probes.py:59`,
`test_verifier_vm.py:86`) — an operator-pinned local pack, unrelated to git
identity.

### Open items for the owner

- `bd show sch-20` → *no issue found matching "sch-20"*. The bead does not
  resolve under that id; a prior `recovery:resolve` no-op is consistent with
  that. Confirm the tracker id before closing anything.
- The working tree on `school/sch-20-seam-hygiene` carries ~30 modified files
  (`director.py`, `scoring.py`, `leaderboard.py`, `recovery.py`,
  `state_journal.py`, CI workflows, …), not the 4 previously reported. Stage
  only `student_vm_runner.py`.
- Probe artifacts live in `/tmp` (`/tmp/brokerbin/git`, `/tmp/probe_git_env.py`)
  and are not durable. `tests/conftest.py` was temporarily neutralized during
  probing and **restored** — verified: `git diff -- tests/conftest.py` is empty,
  `:624` reads `"/PATH": "/usr/bin:/bin"`, no probe markers remain, no new
  stashes.
