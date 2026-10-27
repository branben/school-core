# The mechanical gate (the `code-quality-gates` skill is a separate thing)

> **Read this before searching for a skill named `code-quality-gates` *in this
> repo*.** It is not here. It lives in the Hermes profile at
> `~/.hermes/profiles/<name>/skills/school-core/code-quality-gates/SKILL.md`.
>
> ~~Measured 2026-10-24: `find . -name SKILL.md | xargs grep -l code-quality-gates`
> returns nothing.~~ **This was a wrong-tree search and is corrected.** Run from the
> repo root, the command genuinely returns nothing — but that measures the repo, not
> the profile where the skill is installed. The skill is real; it is not wired into
> this repo. Its canonical artifact, `docs/quality-gates-checklist.md`, was absent
> until 2026-10-24 and is now written and consumed by
> `src/review/adversarial_reviewer.py`.
>
> The gate this page documents is still mechanical code, not a skill.

---

## Why "no model is involved" is the point

`scripts/review_gate.py:5` states it outright:

> Deliberately does NOT ask a model "does this look good?". An LLM opinion is
> not a merge criterion. Every assertion here is mechanically checkable, so the
> gate is deterministic, cheap, and cannot be rate-limited.

A gate that asks a model is a gate that can be talked out of. A gate that checks
properties cannot. That is the whole design.

---

## The three pieces

### 1. `scripts/review_gate.py` — the PR gate (CI `review-gate` job)

Runs on the diff. All checks must pass:

1. **No secrets** in the diff — high-signal patterns only, on ADDED lines.
2. **Is this tested?** — changed Python source must have a corresponding changed
   test, *or* the PR touches only tests/docs/config.
3. **No junk** — no VCS / editor / build artifacts in the changed-file list.

Deterministic, cheap, no network, no model.

### 2. `src/review/verify_gate.py` — the execution gate (sandbox)

`campus.md` principle #3: *"the compiler runs before the critic speaks."* This is
the stage that actually **runs** untrusted student code, inside an OS sandbox
(macOS Seatbelt) with writes confined to scratch and network denied.

Safety model (`verify_gate.py:8-20`):

- Only declared repository verification commands run.
- Repository symlinks are checked before manifests are read; targets outside the
  repo or inside excluded paths are rejected before the scratch copy.
- The cached clone is copied to a writable temp scratch dir; dependency files are
  **copied, never hardlinked**, so verification cannot mutate the shared clone.
- **Unsupported hosts fail closed** and report a skipped verification.
- Timeouts bound every command; non-zero exit ⇒ failure finding.

`VERIFY_GATE_STRICT` promotes a skipped/unsupported verification to a failure
rather than a pass — the fail-closed knob.

### 3. `.beads/hooks/pre-commit` — the outer-loop gate

Added 2026-10-24 (watch-out W3). The outer loop's last phase is "ticket"; a ticket
was created and accepted silently, with nothing checking it against the phases
before it. This hook runs `bd lint` on the work types (`task`, `bug`, `feature`)
and exits non-zero when an open work item is missing a required section.

```sh
bd lint                 # list what is missing
bd update <id> --acceptance "..."   # the fix
```

**Why a hook and not a wrapper:** the store is written by two paths — the `bd` CLI
and the browser UI (`src/bridge/beads_workspace.py`). Both commit through git and
`core.hooksPath` is `.beads/hooks`, so a hook here runs on **every** path. A guard
inside `beads_workspace.py` would gate the browser only; a wrapper around `bd`
would gate the CLI only. Either is theater.

**Why `decision` and `epic` are excluded:** they are containers, not work. A
decision that is still open is not a defect — it is a question awaiting human
input. Requiring `## Decision` on an open question would fabricate a decision that
has not been made.

---

## WARNING: the L0 hook gate is NOT wired (measured 2026-10-24)

`code-quality-gates` rule 3 claimed L0 is wired via `.beads/hooks/pre-commit` chaining
`.beads/hooks/l0-gate.sh`. **It is not.** Six checks, all failing:

| Check | Result |
|---|---|
| `.beads/hooks/pre-commit` contents | beads shim (v1.1.0) + **the `bd lint` outer-loop gate** (added 2026-10-25, below the managed markers) |
| `.beads/hooks/l0-gate.sh` exists | no |
| `grep -rn l0-gate .beads/` | no matches |
| `git ls-files \| grep l0-gate` | no matches (never tracked) |
| `git config core.hooksPath` | `.beads/hooks` (so the hook IS live) |
| `bd hooks list` | `pre-commit: installed (shim 1.1.0)` |

**Consequence:** a commit today runs `bd hooks run pre-commit` (which runs **no
chained hooks** — `bd config get hooks` → *not set*) **and then the outer-loop
gate**, which lints every gated work type. It still does **not** run
`compileall`, `mypy --strict`, or a **secret scan**.

**This corrects an earlier version of this section** (and of §3 above), which
claimed the hook carried "the `bd lint` gate". Measured 2026-10-25: at that time
the hook was a pure beads shim — 33 lines, all inside the BEADS markers — and
`bd lint` was a separate CLI command that nothing on the commit path invoked. The
gate was **absent**, not merely unwired. It has since been added.

**The gate is now measured, not asserted.** Run against the repo as committed:

```
$ sh .beads/hooks/pre-commit; echo rc=$?
rc=0                                   # bd lint clean: 18 issues checked

$ PATH=<stub that fails bd lint>:$PATH sh .beads/hooks/pre-commit; echo rc=$?
outer-loop gate: bd lint failed for work type 'task':
missing Acceptance Criteria
outer-loop gate: refusing the commit. ...
rc=1                                   # a real failure is a real refusal

$ BEADS_SKIP_LINT=1 sh .beads/hooks/pre-commit; echo rc=$?
rc=0                                   # the bypass is explicit, never silent
```

`tests/test_outer_loop_gate.py` pins the gate's shape (the `for _t in` loop over
`task bug feature`, `bd lint` present, no query-exit-code or pipe-to-grep guard).
It is tracked and green as of 2026-10-25.

Do not cite L0 as a reason a commit is safe until it is restored and re-measured.
The likely cause (UNPROVEN) is a `bd hooks install` / shim upgrade replacing the file:
only the beads-managed markers are preserved across that, so content **outside** the
markers survives and content **inside** is replaced.

**Corollary for hook authors:** append outside the `BEGIN/END BEADS INTEGRATION`
markers, or the next `bd hooks install` will silently drop your gate. That is exactly
why the outer-loop `bd lint` gate survived and this one may not have.

## Hook scripts are POSIX `sh`, not bash

`.beads/hooks/*` use `#!/usr/bin/env sh`, which is **dash** on Debian/Ubuntu. Measured
2026-10-24: `"${_out}${_o}"$'\n'` under dash appended a literal `$`, `\`, `n` rather
than a newline — macOS `sh` is bash so it looked correct locally. Use
`printf '%s\n' "$x" >&2`; verify with **both** `dash -n` and `bash -n`.

## The trap this gate must not fall into

`bd query "spec=none"` is **fail-open**. Measured 2026-10-24:

```sh
$ bd query 'spec=nonexistent-xyz' >/dev/null; echo $?
0          # rc=0 with NOTHING matched
```

`bd query` returns exit code 0 and *"No issues found"* when nothing matches, and
exit code 1 only for a malformed expression. The exit code cannot distinguish
"found" from "not found" — a guard on it passes while guaranteeing nothing.

`bd lint` is used instead because it **reads the description text** and exits
non-zero on a missing section. That is a real signal, not an absence.

This is pinned by `tests/test_outer_loop_gate.py`, which fails if the hook ever
starts gating on `bd query`'s exit code.

---

## How to run them

| Gate | Command |
|---|---|
| PR gate | `python scripts/review_gate.py` (or the CI `review-gate` job) |
| Execution gate | via the review pipeline; `VERIFY_GATE_STRICT=1` to fail closed |
| Outer-loop gate | `bd lint`, or any `git commit` (the hook runs it) |

---

## Related

- `docs/sdlc/loops.md` — the two loops; the outer loop's last phase is "ticket".
- `docs/sdlc/context-skills.md` — the declaration policy, and why declared ≠ invoked.
- `KnowledgeCore/01-Projects/School Core/School Core watch-outs as comparable improvements 2026-10-24.md` — W2.
