# Security

## Threat model

school-core is a public repo that runs CI on a self-hosted runner and dispatches
AI agents through model APIs. The secrets that matter:

- **`RUNNER_ADMIN_TOKEN`** — used only by the `integration-gate` job in `ci.yml`
  to query the GitHub Actions runners API. Currently a full personal token; the
  hardening item (swap to a fine-grained PAT) is tracked in
  `docs/security/runner-token-rotation.md`.
- **Model API keys** (`OMNIROUTE_API_KEY`, `NOUS_API_KEY`) — loaded from `.env`
  at runtime, never committed. Redacted in logs via regex in `crew_dispatch.py`
  and `teacher_feedback.py`.
- **AgentMail keys** (`AGENTMAIL_API_KEY`) — optional; used for verdict cards
  and issue alerts.
- **`GITHUB_TOKEN`** — loaded from `.env` for PR creation; CI uses the default
  `GITHUB_TOKEN` scoped to `contents: read`.

## What is already in place

- `.env` and `.env.*` are gitignored (`.gitignore:78-80`); only `.env.example`
  (placeholder-only) is committed.
- `.secrets/` is age-encrypted and gitignored; `recipient.txt` (public key) is
  the only committed file.
- Token redaction is asserted by tests (`test_teacher_feedback.py`).
- CI default token is `contents: read`; the runner gate uses a separate secret.
- ShipSafe security scan runs on every PR and push (`.github/workflows/ship-safe.yml`).

## Open hardening item

`RUNNER_ADMIN_TOKEN` is a full personal token. The runbook at
`docs/security/runner-token-rotation.md` describes the correct fine-grained PAT
shape (repo: `branben/school-core`, Administration: Read-only). Swapping it in
is the remaining action.

## Docker socket caveat

`docker-compose.yml:14` mounts the host Docker socket into the `orca-shim`
container. This is a container-escape vector. The compose file is for local
development only — do not use it for shared or production deployments.

## Reporting a security issue

Open a GitHub issue or contact the maintainers directly. Do not include secrets
in issue descriptions.
