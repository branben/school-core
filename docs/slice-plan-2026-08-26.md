# school-core Last-Mile — Slice Plan & Dispatch Ledger
Date: 2026-08-26 · Director: Brandon (via @sisyphus) · Status: dispatched

## Rule zero (bead 9rn)
NO agent works in `~/school-core`. Each gets an isolated clone. Live tree is
read-only for the director. Violating this is how the repo ended in detached
HEAD earlier today.

| Worker | Clone | Slice |
|---|---|---|
| @student-ci | `/tmp/wk-ci` | S1 — baseline green |
| @student-whymage | `/tmp/wk-whymage` | S2 — evidence comparator (diagnosis only) |
| @student-coder | `/tmp/wk-coder` | S3 — artifact hygiene |
| @student-executor | `/tmp/wk-exec` | S4 — rebase #67 + re-measure |

## Dependency order (why the slices are sliced this way)

```
S1 baseline green ──┐
                    ├──> S4 rebase #67 & re-measure ──> verdict on #67
S3 artifact hygiene ┘
S2 evidence comparator (independent; unblocks the real discriminator)
```

S1 is FIRST because "green CI on real work" is unreachable while main itself
is red (11 failures). S4 depends on S1+S3 because a rebase judged against a red
baseline and a fence-polluted artifact tells you nothing.

---

## S1 — Baseline green · @student-ci · bead school-core-fhm · P1
**Clone:** `/tmp/wk-ci` (branch main @ f82a6e7)

**Measured starting state:** 11 failed / 3 passed (targeted subset, py3.9 via
`/usr/bin/python3` — the Homebrew `python3` has no pytest).

**Known cause for the `test_review_build_gate` cluster:** `director.py:485`
```
repo_path = _resolve_repo_path(repo, explicit_path=repo_path)
TypeError: <lambda>() got an unexpected keyword argument 'explicit_path'
```
A test double patches `_resolve_repo_path` with a lambda lacking that kwarg.

**Contract**
1. Count CAUSES before fixing. 11 symptoms ≠ 1 cause. Classify each of:
   `test_ce_router::test_route_decision_logs_to_bookbag`,
   `test_issue_bridge::TestCrewDispatchPath::{test_timeout_falls_back_direct,test_per_cycle_cap_falls_back_direct}`,
   `test_orca_executor_repo_path.py` (2), `test_review_build_gate.py` (6).
2. TDD: prove RED for the right reason before GREEN.
3. Fix the DOUBLE or the CALLER — whichever is wrong. If `explicit_path` is a
   legitimate new param, the doubles are stale; if not, `director.py` is wrong.
   State which and why.
4. Guard check first: grep `test_n*`, `*_guard`, `*_invariant` for anything
   asserting on `_resolve_repo_path`'s signature.

**Done when:** targeted subset is 14/14 green in `/tmp/wk-ci`, staged diff
reported, `git diff --stat` pasted, zero new failures vs the 11-failure baseline.
**Gate:** no commit, no push.

---

## S2 — Evidence comparator · @student-whymage · beads qpo + a9s · P1
**Clone:** `/tmp/wk-whymage` · **DIAGNOSIS ONLY, no code**

**Measured:** 125 status files in `~/.hermes/school-core-fm-config/state/`
(82 `working:`, 24 `done:`, 14 `failed:`, 4 `resolved:`, 1 malformed).
87 cited SHA occurrences, 60 unique. 9 resolve in either `~/school-core` or
`~/sound-royale-ny`; 51 resolve nowhere. Of the 9: 6 authored by Brandon
(sound-royale-ny), 2 `github-actions[bot]`, 1 `fly-io[bot]`. **0 crew-authored.**
`git ls-remote origin 'refs/heads/fm/*'` → **0 refs**. And **0 of 125** files
carry a `project=` field.

**Question (why-tree to mechanism):** why does `_artifact_identity` honor a
`commit=` without validating (a) reachability and (b) authorship in the worktree?

**Contract**
1. Trace to `file:line`. Name the checkout on every claim.
2. This is a COMPARATOR case, not a vocabulary gap — both second sources
   (`git cat-file -t`, `%an`) were on disk the whole time. Do not propose a
   framework; propose the comparison.
3. Reverse-tree the blast radius: what currently depends on `done:` being
   trusted?
4. Separately: where SHOULD repo attribution be written so clause 3 of the
   discriminator becomes implementable? (@lucas suggests joining `.status`→`.meta`
   rather than adding a field — evaluate both.)

**Done when:** mechanism named at `file:line`, a 3-line comparator proposed
(NOT a registry — a registry whose evidence source is weak passes forever while
lying), and blast radius listed.

---

## S3 — Artifact hygiene · @student-coder · beads 4ti + 03m · P2
**Clone:** `/tmp/wk-coder`

**Two defects, both CONFIRMED:**
- `pr_creator.py:466-468` writes `school-output/<domain>/<num>/output.py` with
  NO fence stripping. On the pr67 branch that file's line 1 is a literal
  ```` ```python ```` → SyntaxError.
- `.github/workflows/ci.yml:40` runs `compileall -q .` over the whole repo,
  including generated artifacts, so one bad artifact reds the entire matrix
  before a single test runs.

**Verified exit codes (bare — never pipe, a pipe reports the LAST element's status):**
```
compileall -q .                        -> 1
compileall -q -x "^school-output/" .   -> 1   # anchor bug: walked path is ./school-output/...
compileall -q -x "school-output" .     -> 0   # correct
```

**Contract**
1. Producer: write `output.md`, strip outer fences
   (`^```[a-zA-Z0-9_-]*\n` and `\n```$`).
2. **Guard tests veto a naive rename:** `tests/test_pr_creator.py` asserts on the
   `.py` path at :190, :221, :289, :311, :368. All five must move together.
3. CI: `ci.yml:40` → `-x "school-output"` (no `^`).
4. `project_verify.yaml:21` uses `compileall -q *.py` (root glob) — unaffected,
   leave it.
5. State what this does NOT fix.

**Done when:** both diffs staged, suite green in `/tmp/wk-coder`, `compileall`
exit 0 on the pr67 branch there.

---

## S4 — Rebase #67 & re-measure · @student-executor · bead 75o · P2
**Clone:** `/tmp/wk-exec` · **BLOCKED until S1 and S3 land**

**Retraction context — read before touching this.** The director previously
reported #67 as "697 deletions incl. `_STATUS_RE` hardening." That was a
MERGE-BASE ERROR and is RETRACTED:
```
merge-base(main, pr67) = a0b7646 (2026-08-17); main is 51 commits ahead
git diff main..HEAD    -> 35 ins / 697 del   <- artifact of a stale branch
git diff a0b7646..HEAD -> 5 ins / 0 del      <- truth (hello.txt + output.py)
```
PR #67 adds five lines and deletes nothing.

**Contract**
1. Rebase `pr67` onto current main **in `/tmp/wk-exec` only**. Report exact
   commands and exit codes.
2. Re-measure `tests/test_crew_dispatch.py::test_happy_path_reads_report_and_tears_down`
   — it passes on main, fails on the stale branch. Hypothesis: STALENESS (old
   source vs current tests), not destruction. Confirm or refute.
3. Always diff against merge-base: `git diff $(git merge-base main HEAD)..HEAD`.
4. Deliver a verdict: mergeable / needs-work / genuinely-regressive, with the
   measurement behind it.

**Done when:** rebased locally, test re-measured both sides, verdict stated.
**Gate:** no push, no force-push, no PR state change. Closing #67 would destroy
the only end-to-end pipeline artifact that exists — the director decides.

---

## Standing constraints (every slice)
- One class per bead. No bundling.
- Name the checkout on every claim (`/tmp/wk-*` + commit SHA).
- CONFIRMED vs UNPROVEN labels, always explicit.
- Tell the director if any premise in the brief is wrong. Premises here have
  been wrong four times today; two agents and the director all retracted.
- HUMAN GATE: no commits, pushes, merges, or PR-state changes. Stage and report.
- `/usr/bin/python3` for pytest. Full suite ≈16 min — run it in background.
- Never pipe a build/test command through `head`/`tail`; it masks the exit code.

## Not delegated (director keeps)
- Closing/merging anything.
- The WG post and job-search sends.
- Deciding #67's fate after S4 reports.
