# Runner liveness token rotation

## Purpose

`RUNNER_ADMIN_TOKEN` is used only by the `integration-gate` job in `ci.yml` and
the `gate` job in `school-loop.yml` to query:

```text
GET /repos/branben/school-core/actions/runners
```

The token is not used for model calls, Git pushes, issue updates, or board
publishing. Those jobs use their own job-scoped `GITHUB_TOKEN` permissions.

## Required token shape

Create a **fine-grained GitHub personal access token** with:

- Repository access: only `branben/school-core`.
- Repository permission: **Administration — Read-only**.
- No write permissions.
- The shortest practical expiration for the operating cadence.

Do not use a classic PAT or a personal token with broad `repo` scope. Do not
paste the token into this repository, a workflow file, a shell script, or a
chat message.

## Rotation procedure

1. In GitHub, open **Settings → Developer settings → Fine-grained personal
   access tokens** and create the token with the scope above.
2. Before changing the repository secret, verify the new token locally without
   printing it or putting it in shell history:

   ```bash
   set -euo pipefail
   read -rsp 'New runner token: ' RUNNER_ADMIN_TOKEN
   printf '\n'
   ONLINE=$(GH_TOKEN="$RUNNER_ADMIN_TOKEN" gh api \\
     repos/branben/school-core/actions/runners \\
     --jq '[.runners[] | select(.name == "school-core-mac" and .status == "online")] | length')
   printf 'runner API works; online runners: %s\n' "$ONLINE"
   ```

   A successful response proves the permission is sufficient. The count may be
   zero when the Mac is asleep; that is different from a 403 permission error.
3. Replace the repository secret through GitHub's secret UI, or pipe the
   value from the still-live hidden shell variable so it is never a command
   argument:

   ```bash
   printf '%s' "$RUNNER_ADMIN_TOKEN" | gh secret set RUNNER_ADMIN_TOKEN \\
     --repo branben/school-core
   unset RUNNER_ADMIN_TOKEN
   ```
4. Verify the secret name exists without revealing its value:

   ```bash
   gh secret list --repo branben/school-core | grep -E '^RUNNER_ADMIN_TOKEN[[:space:]]'
   ```
5. **Only with explicit operator approval**, run the **CI** workflow manually
   from `main` and inspect the `integration-gate` job. This checks runner
   liveness without dispatching a school issue, but the same run also executes
   live Orca integration tests on the Mac.

   ```bash
   gh workflow run ci.yml --repo branben/school-core --ref main
   ```

   Confirm that the gate reports `school-core-mac online` when the runner is
   awake, and that the live integration job is selected. If the runner is
   asleep, confirm the gate reports a clean offline skip rather than a 403.
6. After the replacement has passed verification, revoke the old token in the
   GitHub token settings page. Do not revoke the old token first: that creates
   an avoidable gap in runner-liveness checks.
7. Confirm the old token is no longer valid by testing it only through the
   GitHub UI or a protected local environment. Never write it to a file or
   include it in a diagnostic command.

## Scope verification in the repository

The workflows intentionally separate permissions:

| Workflow/job | Default or job token | Purpose |
|---|---|---|
| CI default | `contents: read` | Checkout and test code |
| CI runner gate | `RUNNER_ADMIN_TOKEN` | Read runner liveness only |
| School Loop default | `contents: read` | Safe baseline |
| School Loop `execute` | `contents: write`, `issues: write` | Checkpoint sanitized state and close/label issues |
| School Loop `loop` | `contents: write`, `issues: read` | Publish board state and read issue data |
| School Loop runner gate | `RUNNER_ADMIN_TOKEN` | Read runner liveness only |

The runner token must not be added to `execute`, `loop`, or any model/API
request. The repository should contain only the token name, never the value.

## Failure and rollback

- **403 from the runners API:** the PAT is missing, expired, attached to the
  wrong repository owner, or lacks `Administration: Read`. Create a corrected
  replacement and update the same secret name.
- **Runner count is zero with HTTP success:** the token works; the Mac is
  offline or the runner is not connected. Do not widen token permissions.
- **CI gate works but School Loop gate fails:** compare workflow edits and
  repository secret configuration; both jobs must use the same secret name.
- **If replacement is invalid:** restore a known-valid replacement token in
  `RUNNER_ADMIN_TOKEN`, then revoke the invalid token after the gate is green.

## Safety check

This runbook does not authorize dispatching a live School Loop cycle. A manual
School Loop dispatch can process real open issues. Use a scheduled cycle or get
explicit approval before running it for end-to-end verification.
