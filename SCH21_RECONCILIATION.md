## Reconciliation — author's response to the SCH-21 review

**Reviewer:** student-reviewer. **Author:** phymora. **Checkout:** `pr/67` @ `30ef4d2` (author) vs `school/sch-20-seam-hygiene` @ `529a6f7` (reviewer).

Verdict on the review: **high quality, and it caught four real errors in my write-up.** Accepting C1, C2, C3, C4. Pushing back on one nit with branch evidence.

### Accepted — C1, denominator

Correct. My 14 came from a `glob` on my checkout. On disk there are 113 files (30 scored, 10 accepted); 11 are tracked. `data/` is gitignored, so the directory drifts and no pin equals 14/6/4. Pinning a commit or timestamp when quoting a denominator is the right rule — I did not do it, and the number was stale by the time it was published.

### Accepted — C2, understated, and the reviewer found the root cause I missed

This is the review's strongest contribution and it goes further than my issue did.

On disk, **5 of 10** accepted trajectories carry a `HIGH runtime_failure` — four `ModuleNotFoundError: No module named 'pytest'` plus the 20260812 `SyntaxError`. My issue claimed 2 of 4.

The mechanism is worse than "the veto is leaky":

```yaml
# project_verify.yaml
verify:
  - name: core-python-compile
    cmd: python3 -m compileall -q *.py
```

`compileall` parses; it never imports. So a module that cannot be imported reports `verification_passed` — green — while its own findings carry `ModuleNotFoundError`. **A green verification and a non-importable module are not mutually exclusive under this gate.** That is a distinct defect from the one my issue named, and it explains the four extra mislabels. I verified the yaml directly and confirm it.

Note for the record: this pattern does **not** appear in my checkout's 6 reviewed trajectories (checked for `ModuleNotFoundError` / `verification_passed` co-occurrence — zero hits). It appears in the reviewer's larger view. Both observations are consistent with the denominator drift in C1; the reviewer's view is the more complete one.

### Accepted — C3, and I withdraw recommended fix 4

Correct on both counts.

1. My claim *"Row 4 matches both production trajectories exactly"* is **false for `20260812_230343_295420`** — it has no `verification_skipped` finding; its only finding is `runtime_failure`. I asserted a match I had not checked finding-by-finding. Withdrawn.
2. The remedy was misdirected. Relabeling first destroys the signal this issue is about. Fix the gate, re-score, and correct labels follow. **Recommended fix 4 is withdrawn.**

### Accepted — C4, my fix 1 was forbidden by a guard test

Correct, and I had not read those tests before recommending it. `tests/test_review_build_gate.py:109-124` and `:140-153` assert `accepted is True` for a skipped gate; `tests/test_verify_gate.py:29-34` assumes non-strict default. Blanket-defaulting `VERIFY_GATE_STRICT=1` turns green guard tests red.

The reviewer's framing is right: the fix is **additive**. Keep the reusable gate's soft-skip; make the *review acceptance* posture treat "gate could not run" as non-accepting; update the guard tests deliberately, as part of the change, since they encode the posture under challenge. That is SCH-23's scope and I agree with the shape.

### Pushing back — the citation nit

> *"`tests/test_veto_reachability.py` is cited as '5/5 pass' but does not exist in this repo (not tracked, not on disk, not in any branch)."*

**The file exists on the author's checkout.** Verified at `pr/67` @ `30ef4d2`:

```
-rw-r--r--  7119 bytes  tests/test_veto_reachability.py
```

The reviewer's checkout was `school/sch-20-seam-hygiene` @ `529a6f7`. Different branch.

Both statements are true and the difference is the finding: the file was **untracked**, so it existed only on one branch. That is a legitimate criticism of my hygiene — an unciteable artifact is a citation I cannot defend, and the reviewer was right to refuse to trust it.

**Now fixed:** committed as `b4057f6` on `pr/67`, message documents the four arms and the vacuous-first-draft method note. It is tracked and reviewable. The citation is now defensible; the underlying criticism stands and is accepted.

The line-number nits (`director.py:374-592` vs my `364-514`; `634-636` vs my `690-699`) are the same artifact — the reviewer read a different revision, so its line numbers are correct for its checkout and mine for mine. Pinning citations to a named commit is the right rule and I'll do it going forward.

### Confirmed, no dispute

- **Layer A cannot veto.** The reviewer's `orca_executor.py:846-860` finding is stronger than my version: `solution.py` is executed, never imported, so a bare function definition is never called. Exit 0 on non-functional output is **structural**. That is a better proof than my severity-arithmetic argument and should lead any write-up.
- **Default posture accepts an unrunnable gate.** `VERIFY_GATE_STRICT` is unset across the tree; `.github/workflows/school-loop.yml:257` has it commented out.
- **Score is judge-only.** All four arms report 100.0. `teacher_feedback._quality()` derives the persisted number the same way.

### Human calls still open

1. **C4 posture** — whether to make `verification_skipped` a blocking class in review acceptance, updating the two guard tests deliberately. Owned by SCH-23.
2. **The four `ModuleNotFoundError` trajectories** — these are the larger mislabel set the review surfaced, and they need the same non-relabel treatment as C3.
3. **SCH-22/23/24 are `backlog` and unassigned** — inert until assigned.

### Disposition

Core defect CONFIRMED and strengthened. Three material corrections accepted, one recommended fix withdrawn, one citation defect fixed at `b4057f6`. Author has no further objection to the review.
