# Quality gates checklist

The canonical review checklist for school-core. Referenced by the
`code-quality-gates` skill and by `config/profiles/student-reviewer/SOUL.md`
("checklist-driven review"). This is the artifact those two references point at.

**How to use it.** A reviewer walks these in order against the diff. A writer walks
them before claiming "the gates passed". Each item is a *question with a checkable
answer* — not advice.

---

## 0. The failures that produce a green gate over broken code

Check these FIRST; everything below is secondary. Each is a mechanism that has
actually shipped a defect with CI green. (An earlier revision of this file said
"the only two mechanisms" — that was an overclaim. There are four.)

**A. Green by absence.** A gate passed because it had *zero coverage of the surface
that changed*, not because the surface was correct.
- Ask: *which* test exercised this code path? Name the file and the scenario.
- If the answer is "the suite is green" without a named test, treat it as untested.
- Python-native tells: a new line carrying `# pragma: no cover`; a failing test
  decorated `@pytest.mark.skip` / `@pytest.mark.xfail`; a `conftest.py` fixture
  that stubs the thing under test; an assertion that asserts nothing.
- Real case (Rust SDK, recorded for contrast): a conformance baseline with
  `server: []` — the extension suite passed because it contained no scenarios for
  the extension.

**B. A gate that CRASHED is recorded as a pass.** The reviewer did not review; the
record says it did.
- Any path that catches an exception and returns a clean result is a candidate.
- Python-native tells: `except Exception: return OK` with no marker; a retry or
  fallback prompt rebuilt inline that drops its context blocks; a `try/except`
  around a gate whose failure branch returns the success value.
- The marker must be machine-readable AND consumed — a flag the *acceptance*
  calculation reads. An unread flag is decoration.
- Real case (school-core): a lens that raised returned `PASS, findings=[],
  parse_failed=False`, and `director.py:792` computes `accepted = ... and not
  parse_failed`, so an unmarked crash fed an **approving vote** for work nobody
  reviewed. Fixed 2026-10-25; pinned by
  `tests/test_lens_parse_fail_open.py::TestLensCrashIsNotAnApproval`.

**C. Suppression that disables the gate.** The check was correct and was silenced.
- CRITICAL: blanket or module-wide suppressions — `# type: ignore` covering a whole
  module, `# pylint: disable=...` at file scope, `# noqa` **without a reason**.
- CRITICAL: a suppression added in the same PR as the code it hides, when the
  reason is absent or does not name a real gap.
- NOT a defect: a targeted `# noqa` / `# type: ignore` **with a reason** naming a
  real gap, plus a follow-up bead. That is the skill's own documented remedy
  (`code-quality-gates/SKILL.md:313-314`, `:323-326`) — *"the gate going green is
  not the goal."* Check the bead exists.
- Rust SDK case (different repo): `#![allow(dead_code)]` at crate root defeated
  `clippy -D warnings` and hid a dead validator.

**D. A gate that was never wired.** The most common one, and the skill's own first
rule — an earlier revision of this file omitted it.
- **Do not claim a gate ran if it is not wired.** `code-quality-gates/SKILL.md`
  rule 1 and rule 3: as of 2026-10-24 the L0 local gate is **unwired** —
  `.beads/hooks/l0-gate.sh` does not exist and `.beads/hooks/pre-commit` chains
  nothing. Before citing L0 as a reason a commit is safe, re-run the six checks in
  the skill.

---

## 1. Correctness

- [ ] **Every error path is reachable.** Is there a branch that cannot be taken?
- [ ] **A function returning `None` to signal failure is not caught by
      `except Exception`.** If a write of *gate evidence* can return `None`, the
      caller's `except` will not fire and the failure is swallowed. Ask what reads
      that record later.
- [ ] **A constraint read from shared config is applied in EVERY branch** that
      builds the command. A conditional pin is not a pin — grep each branch and
      assert it in each mode.
- [ ] **Silently-dropped arguments.** A model/library that ignores unknown kwargs
      (e.g. pydantic without `populate_by_name`) fails open: print the constructed
      object and confirm the field is what you set, not the inverse.

## 2. Gates and their labels

- [ ] **Gate on the finding's CLASS, not the verdict string.** A fail-closed
      fallback can return `PASS` by design, so a `verdict == "FAIL"` check is
      bypassed by a crash.
- [ ] **A severity LABEL set by the producer must be the label the consumer gates
      on.** Run the producer's real output through the consumer's predicate and
      print it. A mismatch makes the gate unreachable while the suite stays green.
- [ ] **A gate returning `None` is a soft-skip, not a pass.** "No result" and
      "passed" must be distinguishable downstream.
- [ ] **Every claimed defect cites the test that goes RED→GREEN, and the spec line
      it violates.** No test, no commit. A test written *after* the fix and passing
      on arrival proves nothing about the original defect.

## 3. Tests

- [ ] **Is this tested?** Changed source has a corresponding changed test.
- [ ] **Does the new test fail without the fix?** Verify RED for the right reason
      before GREEN.
- [ ] **A guard test can PIN the bug it was written to catch.** When a test goes red
      on your fix, read its *comment* before weakening the code: if the comment
      states the intended contract, the assertion is what is wrong.
- [ ] **Assert the ARTIFACT, not a path derived from `__file__`.** A `__file__`-derived
      assertion passes silently when a module happens to sit where the expectation
      lands.

## 4. Before changing a boundary

- [ ] **Before adding a second guard, prove the primary caller's existing gate** —
      read the same value, at a line *before* publication — so the new guard cannot
      newly block a legitimate path. Cite both line numbers.
- [ ] **Grep for tests that assert on the thing you are about to change.** A guard
      test can veto a fix. If a guard forbids it, provide the missing thing instead
      of weakening the code.
- [ ] **Before treating an ignored return value as a gate hole, find the CONSUMER of
      the record.** The fix shape depends on whether anything downstream reads it.

## 5. Hygiene

- [ ] No secrets in added lines.
- [ ] No VCS / editor / build artifacts in the changed-file list.
- [ ] Hook scripts are POSIX `sh`, not bash — verify with `dash -n` **and** `bash -n`.
- [ ] Hook content goes OUTSIDE the `BEGIN/END BEADS INTEGRATION` markers, or the
      next `bd hooks install` will silently drop it.

---

## What this checklist does NOT cover

- It is **not** a Rust/Cargo gate. Rust SDK work belongs to `rust-sdk-pr-workflow`.
- It is **not** a substitute for running the gates. A checklist read is not a gate run.
- L0 local (compileall · mypy on contract modules · secret scan) is **NOT wired** as
  of 2026-10-24 — see `docs/review/mechanical-gate.md`. Do not cite it.

## Provenance

Distilled from the `code-quality-gates` skill — primarily rules 9-18, plus rule 1
and rule 3 (a gate that was never wired), rule 20 (write the reader first), and rule
21 (a rebuilt prompt silently drops its blocks). The skill is the source material;
this file is the artifact a reviewer can be pointed at.

Two revisions of this file were wrong and were corrected against the source:
- It claimed "the only two mechanisms" produce a green gate over broken code, and
  omitted the skill's own rule 1 — a gate that was never wired.
- §0B called every same-PR suppression CRITICAL, contradicting the skill's
  prescribed remedy (`# noqa` **with a reason** + a follow-up bead).
- §0 cited a Rust construct (`#![allow(dead_code)]`) in a Python-only repo.

An earlier revision also claimed a lens crash "actively votes to accept" while its
test input had no artifact; the `no_artifact` veto
(`adversarial_reviewer.py:466-479`) masked the crash and made the test pass on a
FAIL verdict. The claim is true — but only with an artifact present, which is what
the pinned test now uses.
