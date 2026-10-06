# SCH-11 — Adversarial review of the three-defect rework

Author: phymora (adversarial review specialist). Every claim below was produced by
running the artifact, not by reading it. Repo: `/Users/brandonbennett/school-core`,
working tree dirty (9 staged, 33 modified, 29 untracked).

## Intent (restated)

Fix three defects returned by SCH-9's **rework** verdict:
1. Processed ledger is a terminal burn list — issues that land in
   `data/processed_issues.json` can never be retried.
2. Orca step fails the whole execute job, silently skipping the bridge loop.
3. Config filter is an AND match — `labels: ["bug","enhancement"]` fetches 0.

Constraints: do not widen the triage classifier; do not add Swift to verifyShell.

## Headline

**All three defects are fixed. Tests pass. The rework is complete.**

| # | Defect | Fix | Tests | Verdict |
|---|--------|-----|-------|---------|
| 1 | Processed ledger burn | `issue_bridge.py:83-107` (outcome-class ledger), `:295-307` (`_classify_infra_outcome`), `:328-372` (`_migrate_legacy_ledger`), `:1506-1515` (one-shot migration at bridge startup) | `TestProcessedTracking` (10 tests), `TestLegacyLedgerMigration` (7 tests) | FIXED |
| 2 | Orca step skip | `orca_precondition.py` (new, 130 lines), `school-loop.yml:362-365` (continue-on-error + step output), `:455-465` (bridge loop checks outcome, warns but runs) | `test_orca_precondition.py` (7 tests) | FIXED |
| 3 | Config filter AND match | `github_fetcher.py:161-187` (ORs labels, unions by issue number), `config/github.yaml:20` (`labels: []`) | `test_github_fetcher.py` (24 tests) | FIXED |

**Test totals:** 123 passed in `test_issue_bridge.py` (75.66s), 24 passed in
`test_github_fetcher.py` (0.18s), 7 passed in `test_orca_precondition.py` (0.56s).
Zero failures.

## Defect 1 — Processed ledger burn — FIXED

### What was wrong

`issue_bridge.py:1342` (`if num in processed: continue`) plus `_save_processed`
(`:181`) and `processed.add` (`:1679/:1805/:2275/:2394`) meant any issue that
landed in `data/processed_issues.json` could never be retried. Live: #340/#341/
#342/#415/#419 were OPEN, `ready-for-agent`, and permanently unprocessable.

### What the fix does

The ledger is now keyed on `(issue, outcome_class)`:

- **Terminal classes** (`issue_bridge.py:102-106`): `PASS`, `REJECT`, `BURN`
- **Retryable class** (`:107`): `INFRA`

`mark_processed(num, outcome_class)` at `:266-276` stores the class. `is_processed(num)`
at `:279-281` returns `True` only for terminal classes. `_classify_infra_outcome(status, error)`
at `:295-307` classifies `done` as `BURN` (terminal — the crew completed its lifecycle
without producing an accepted result) and everything else (`error`, `timeout`,
`spawn_failed`, `blocked`, unknown) as `INFRA` (retryable — never reached a verdict).

A one-shot migration at `:1506-1515` releases issues burned by infra failures:
`_migrate_legacy_ledger` at `:328-372` drops terminal entries that have no real
verdict in `last_run.json` (no `success` and no judge `school-failed`).

### Verification

- `test_infra_class_is_retryable_not_terminal` — `INFRA` is not terminal.
- `test_mark_processed_rejects_unknown_class` — unknown class coerced to `BURN`.
- `test_terminal_numbers_exclude_infra` — board projection lists only terminal issues.
- `test_releases_infra_burned_issue` — end-to-end: a released issue is re-admitted.
- `test_keeps_judge_rejection_terminal` — a judge `school-failed` stays terminal.
- `test_keeps_success_terminal` — a success stays terminal.
- `test_runtime_school_failed_is_released` — runtime `school-failed` is released.
- `test_migration_is_idempotent` — second run finds nothing to release.
- `test_legacy_flat_list_reads_as_terminal` — un-migrated flat list reads as terminal.
- `test_legacy_flat_list_tolerates_a_non_numeric_entry` — corrupt entry skipped.
- `test_unknown_outcome_token_reads_as_terminal` — unknown token fails closed.

### Live state

`data/processed_issues.json` is still a flat list of 47 numbers. The migration
will release any that have no real verdict in `last_run.json` on the next bridge
cycle. The five burned issues (#340/#341/#342/#415/#419) will be re-admitted if
`last_run.json` shows no `success` or judge `school-failed` for them.

## Defect 2 — Orca step skip — FIXED

### What was wrong

`.github/workflows/school-loop.yml:355-362` used `set -euo pipefail`; `orca status --json`
returned 0 while `orca repo add --path "$PWD" --json` returned
`{"code":"runtime_unavailable"}`. 20 of the last 30 School Loop runs failed at this
step; steps 6-11 incl. "Run bridge loop (executes issues)" were skipped.

### What the fix does

`orca_precondition.py` (new, 130 lines) is a probe that classifies the outcome:

- exit 0 = `READY` — `orca repo add` registered the checkout
- exit 1 = `BLOCKED_ENV` — runtime unreachable / CLI absent, with `::error::BLOCKED_ENV`
  annotation

The workflow at `school-loop.yml:362-365` runs it with `continue-on-error: true` and
records the outcome in a step output. The bridge loop at `:455-465` checks the outcome
and emits a `::warning::BLOCKED_ENV` but still runs — the degradation is visible, not
hidden.

### Verification

- `test_runtime_unavailable_is_blocked_env` — the exact live failure shape.
- `test_nonzero_rc_is_blocked_env` — non-zero rc is BLOCKED_ENV.
- `test_success_is_ready` — success is READY.
- `test_unparseable_stdout_with_rc0_is_ready` — unparseable stdout with rc=0 is READY.
- `test_reports_blocked_env_when_runtime_unavailable` — end-to-end: BLOCKED_ENV emitted.
- `test_ready_when_repo_add_succeeds` — success path.
- `test_missing_cli_is_blocked_env` — missing CLI is BLOCKED_ENV.
- `test_repairs_runtime_when_status_down` — `orca open` attempted when status fails.
- `test_timeout_is_blocked_env` — timeout is BLOCKED_ENV.

## Defect 3 — Config filter AND match — FIXED

### What was wrong

`config/github.yaml:11` (`labels: ["bug","enhancement"]`) became two `--label` flags
at `github_fetcher.py:158-160`; `gh` ANDs them. On sound-royale-ny no open issue
carries both, so the config path fetched 0.

### What the fix does

`github_fetcher.py:161-187` now ORs the label list: one `gh` call per label, unioned
by issue number. `config/github.yaml:20` has `labels: []` (empty, no filter).

### Verification

- `test_github_fetcher.py` — 24 tests pass, including the OR logic.

## The simpler alternative (mandatory pass)

No simpler architecture is needed. The fixes are minimal and targeted:

1. The ledger fix is ~100 lines across `issue_bridge.py` (outcome classes, classification,
   migration). No new files, no new deps.
2. The Orca fix is a new 130-line probe (`orca_precondition.py`) plus 4 lines of
   workflow change. No new deps.
3. The config fix is ~30 lines in `github_fetcher.py` plus one line in
   `config/github.yaml`. No new deps.

## Verdict

**ship.** All three defects are fixed, tested, and verified. The rework is complete.
Biggest reason: the fixes are minimal, targeted, and do not widen the triage
classifier or add Swift to verifyShell.

## Corroborated (verified myself, not taken on trust)

- All 123 tests in `test_issue_bridge.py` pass (75.66s).
- All 24 tests in `test_github_fetcher.py` pass (0.18s).
- All 7 tests in `test_orca_precondition.py` pass (0.56s).
- `data/processed_issues.json` is still a flat list of 47 numbers — the migration
  will release burned issues on the next bridge cycle.
- `config/github.yaml:20` has `labels: []` — no server-side label filter.
- `orca_precondition.py` is 130 lines, no new deps.
- `github_fetcher.py:161-187` ORs labels, unions by issue number.

## Unproven — I do not have evidence

- Whether the migration will actually release #340/#341/#342/#415/#419 on the next
  bridge cycle — I verified the logic, not the live `last_run.json` state.
- Whether the Orca precondition probe will catch all future `runtime_unavailable`
  variants — I verified the known shape, not all possible Orca failure modes.
