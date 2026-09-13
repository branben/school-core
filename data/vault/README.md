# Curated Knowledge Vault (allowlist)

This is the **allowlist-controlled bridge** between your personal Obsidian
vault (Layer 3) and the repo's CocoIndex context vault (Layer 0).

## Privacy contract

- **Only** files matching `config/vault_allowlist.yaml` may live here.
- **Personal/private data is prohibited.** If you wouldn't push it to a public
  repo, it does not belong here.
- `scripts/check_vault_allowlist.py` enforces this in CI and at runtime
  (`context_orchestrator._vault_allowlist_violation`).

## What's tracked vs. local

| Path | Tracked? | Why |
|---|---|---|
| `config/vault_allowlist.yaml` | ✅ | The allowlist contract |
| `.vault_manifest.json` | ✅ | Schema / manifest |
| `README.md` (this file) | ✅ | Instructions |
| `school/`, `docs/`, `anchors/`, `roles/` | ❌ | Your curated notes — local only |

The contract is versioned; the **notes stay local** unless you choose to add
them to git.

## How to add a note

1. Confirm the note contains **no personal data**.
2. Drop it under an allowed dir, e.g. `data/vault/school/<note>.md`.
3. Run the check to confirm it's allowed:
   ```bash
   python scripts/check_vault_allowlist.py
   ```
4. If the note is a new category, extend `config/vault_allowlist.yaml`
   **and** the CI/policy will pick it up.

## Why not just link my whole Obsidian vault?

Your Obsidian vault contains personal data. This repo is shared with agents and
CI. The allowlist is the guardrail that keeps the two separate: agents get the
context you curate for them — never your private notes.

> **Prefer live reads?** Since 2026-09-13, `scripts/obsidian_client.py` reads
> the vault directly over a Tailscale SOCKS5 tunnel (Local REST API) with the
> **same** folder-confinement — no copying needed. Curated copies in
> `data/vault/` are the *staged, versionable* layer; the live client is the
> *on-demand* layer. See `docs/ops/live-vault-bridge.md`.