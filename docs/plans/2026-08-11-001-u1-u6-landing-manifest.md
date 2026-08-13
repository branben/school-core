---
title: U1-U6 Landing Manifest
type: ops
status: archived
date: 2026-08-11
scope: school-core U1-U6 only
---

> **Archived 2026-08-12.** This manifest proposed a landing boundary for the
> U1–U6 implementation set. That work is now implemented, committed, and
> live-proven; this document is historical. The current landing contract is
> `docs/plans/2026-08-12-002-refactor-dirty-tree-product-slices-plan.md`.
> No further staging should be driven from this file.

# U1-U6 Landing Manifest

> Canonical manifest path: `docs/plans/2026-08-11-001-u1-u6-landing-manifest.md`.
> This is a proposed commit boundary. It is intentionally not staged,
> committed, or pushed. Files outside the include lists remain in the working
> tree untouched.

## Proof state

- No files were staged when this manifest was created.
- The U1-U6-focused proof run passed: 196 tests, Python compilation, YAML
  parsing, and `git diff --check`.
- A later policy-focused proof run passed: 87 tests, Python compilation, both
  workflow YAML parses, and `git diff --check`.
- The working tree contains unrelated AgentMail, hooks/settings, and CI
  changes. Do not use `git add -A` for the U1-U6 landing.

## Scope

| Unit | Meaning | Landing status |
|---|---|---|
| U1 | Thread `session_id` through the bridge/director so Layer 3 is consulted | Include |
| U2 | Sanitize, trim, and checkpoint trajectories across fresh checkouts | Include |
| U3 | Loud reusable verify gate plus production Nix/`verifyShell` preflight policy | Include |
| U4 | FirstMate issue-path dispatch | **Exclude** — U7-U9 are not implemented |
| U5 | Reconcile `campus.md` operational truth | Include |
| U6 | Rename/wire Entire as a non-blocking pre-merge sensor | Include |

## Include path candidates (not whole-file staging instructions)

These paths contain U1-U6 material, subject to the shared-file hunk rules
below. Clean files may be staged by path; shared files still require `git add
-p` selection. Do not stage this list wholesale.

```text
.github/workflows/school-loop.yml
campus.md
conductor.py
director.py
issue_bridge.py
README.md                         # selected U6 Entire hunks only
scripts/sanitize_data.py
src/entire_review.py
src/qodo_pre_merge.py                  # deletion paired with src/entire_review.py
verify_gate.py
project_verify.yaml
data/sessions/consolidation/index.md

docs/plans/2026-08-11-001-feat-pipeline-context-verify-dispatch-plan.md
.scratch/wayfinder-map-pipeline-gaps.md

tests/test_context_orchestrator.py
tests/test_issue_bridge.py
tests/test_sanitize_data.py
tests/test_spec_gate.py
tests/test_verify_gate.py
tests/test_entire_review.py
tests/test_school_loop_workflow.py
```

### Include-list notes

- `context_orchestrator.py` is already tracked and unchanged in the current
  working tree. It is not part of this landing; U1 consumes its existing
  `session_id` contract.
- `src/qodo_pre_merge.py` must be deleted in the same change that adds
  `src/entire_review.py`; otherwise both competing shims remain.
- `data/sessions/consolidation/index.md` is the U1 seed and is ignored by the
  broad `data/` rule. It contains only repository-relative layout documentation,
  no machine-specific paths, and should be force-added with U1 so a fresh
  checkout has the Layer 3 write-path home. Do not include per-session YAMLs or
  the local `data/trajectories/` corpus in this code/docs landing.
- `project_verify.yaml` is required by the active U3 gate and contains no
  machine-specific paths. Include it as a clean U3 file.

## Shared-file hunk rules

These files contain U1-U6 changes mixed with other work. Do not stage them
wholesale until the hunk boundary is reviewed:

### `.github/workflows/school-loop.yml`

**Include U1-U6 hunks:**

- Trajectory sanitization/trim and forced `git add -f data/trajectories/` (U2).
- Nix/`verifyShell` hard preflight and `VERIFY_GATE_STRICT` policy comments
  (U3).
- Hosted board independence / `loop: if: always()` if this is part of the
  production safety repair being landed with U3.

**Manual-review hunks:**

- Top-level concurrency and `pipeline-alert` are loop hardening completed
  alongside this workflow but are not U1-U6 core. Include them only if the
  landing explicitly absorbs the loop-repair change; otherwise leave those
  hunks for a separate workflow commit.
- Do not include unrelated CI notification changes from `.github/workflows/ci.yml`.

### `issue_bridge.py`

Include these U1/U3/U6 hunks:

- Per-cycle `loop-*` `session_id` generation and passing it to `run_task` (U1).
- `_strict_gate_failure`, flake-path pinning, soft-skip/strict merge handling
  (U3).
- `_run_entire_sensor`, Entire result surfacing, and durable summary fields
  (U6).

Do not include future U7-U9 crew-dispatch code; none is currently present.

### `tests/test_issue_bridge.py`

Include the focused classes/hunks for:

- Cycle `session_id` threading (U1).
- Verify-gate skip/real-failure/strict escalation and flake-path pinning (U3).
- Entire sensor availability/result surfacing (U6).

Leave unrelated or pre-existing bridge tests intact if they are already part of
this file; do not delete them merely to make the patch smaller.

### `conductor.py`

Include only the Qodo-to-Entire import/call/field rename required for U6. Do not
include unrelated conductor behavior changes if the diff shows any.

### `README.md`

The current diff mixes Entire documentation with AgentMail environment
material. Include only the Entire file-tree/status/documentation hunks if
landing U1-U6. Leave AgentMail env documentation for its own commit.

### `campus.md`

Include the operational-truth rows for U1-U3 and U6, including the reconciled
production-preflight versus reusable-soft-skip Nix policy. The FirstMate row
may be included only as a truth correction stating that U4 remains unwired;
that is U5 documentation, not U4 implementation.

## Explicit exclude list

Do not include these files in the U1-U6 landing:

```text
.beads/hooks/pre-push
.beads/hooks/prepare-commit-msg
.beads/hooks/commit-msg
.beads/hooks/post-commit
.beads/hooks/post-rewrite
.beads/hooks/pre-push.pre-entire
.beads/hooks/prepare-commit-msg.pre-entire
.claude/settings.json
.env.example
.github/workflows/ci.yml
school_mail.py
agentmail_client.py
scripts/school_inbound.py
src/agentmail_poller.py
tests/test_school_mail.py
tests/test_agentmail_poller.py
docs/notification-style-guide.md
```

These are AgentMail observability, Entire hook installation/settings, or
separate CI/environment work. They need their own reviewed landing boundary.

Also exclude all U7-U9 files because they do not exist yet:

```text
crew_dispatch.py
tests/test_crew_dispatch.py
data/crew_runs.json
```

## Conditional files requiring a final diff check

```text
README.md                         # selected U6 Entire hunks only
.github/workflows/school-loop.yml # selected U2/U3 hunks; loop hardening optional
project_verify.yaml               # include after path/command review
data/sessions/consolidation/index.md # force-add U1 seed; review before staging
conductor.py                      # selected Entire hunks only
issue_bridge.py                   # selected U1/U3/U6 hunks only
tests/test_issue_bridge.py        # selected U1/U3/U6 additions; preserve rest
```

## Proposed staging procedure (not executed)

After reviewing this manifest and resolving conditional files:

```bash
# inspect first; do not use git add -A
git diff -- <each selected path>
git diff --check

# stage only the approved exact paths/hunks
git add <approved clean paths>
git add -p <shared paths>

git diff --cached --name-status
git diff --cached --check

# run the focused tests against the staged tree
.venv/bin/python -m pytest \
  tests/test_issue_bridge.py \
  tests/test_sanitize_data.py \
  tests/test_spec_gate.py \
  tests/test_verify_gate.py \
  tests/test_entire_review.py \
  tests/test_school_loop_workflow.py -q
```

The manifest file itself (`docs/plans/2026-08-11-001-u1-u6-landing-manifest.md`)
was created for planning and is **excluded** from the U1-U6 implementation
commit unless explicitly added as a separate documentation change. No command
in this section was executed by creating this manifest.
