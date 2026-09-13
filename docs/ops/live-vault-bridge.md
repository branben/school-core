# OPS: Live Vault Bridge — Tailscale userspace + SOCKS5 → Obsidian REST API

**Status:** Live (proven 2026-09-13)
**Owner:** mac (brandon) + this container
**Last verified:** HTTP 200 on `https://100.81.210.96:27124/`

## What this is

The container reads the KnowledgeCore (Obsidian vault) **live** — no manual
copying — over the tailnet:

```text
school-core container
  └─ tailscaled (userspace networking, --socks5-server localhost:1080)
       └─ SOCKS5 -> tailscale IP of Mac (100.81.210.96)
            └─ Obsidian Local REST API plugin, bound to 0.0.0.0:27124
                 └─ scripts/obsidian_client.py (read-only, folder-confined)
                      └─ context_orchestrator "Live Vault" probe
```

Two parts make it work:
1. **Tailscale userspace** `tailscaled` with `--socks5-server localhost:1080`.
   This is the piece that was missing before — userspace mode CAN proxy TCP
   through SOCKS5 (no TUN/kernel needed).
2. **Obsidian Local REST API** bound to `0.0.0.0` (not `127.0.0.1`), with an
   API key, on the Mac.

## Prerequisites

- Tailscale **on the Mac** with a node named `brandons-macbook-air`
  (IP `100.81.210.96` — check with `tailscale status`).
- Obsidian plugin **Local REST API v4.1.3+** installed and enabled.
- **Bind address = `0.0.0.0`** in Settings → Local REST API (this is what lets
  tailnet traffic reach it — 127.0.0.1 blocks it).
- API key copied from the plugin settings.

## Container setup (runbook — durable, survives pod recycle)

State now lives under **`/workspace`** (real disk, `/dev/nvme0n2`), not `/tmp`:
`data/tailscale/` holds the tailscale binary, persistent state, and socket.
After a restart, tailscale is already authenticated and `bridge_live_vault.sh up`
just starts the daemon — no re-auth URL required.

```bash
# 1. One command: download (if cache missing), start tailscaled with SOCKS5,
#    and bring tailscale up. Tailscale state is durable, so no auth prompt
#    unless the node was recreated.
scripts/bridge_live_vault.sh up

# 2. Verify
scripts/bridge_live_vault.sh status   # "mac" online + Obsidian HTTP 200
curl -sk --socks5-hostname localhost:1080 https://100.81.210.96:27124/  # HTTP 200

# 3. Export the key for the agent session (not persisted — it's a secret)
export OBSIDIAN_API_KEY="<from plugin settings>"
```

Paths (override via env):
- `TS_DATA_DIR` — default `/workspace/project/school-core/data/tailscale`
- `TS_VERSION` / `TS_ARCH` — default `1.76.1` / `amd64`

If you see an "ACTION NEEDED — open the auth URL" line, the tailscale node lost
its identity (rare, only if `data/tailscale/` was deleted). Open the URL with
the same Tailscale account as the Mac, then re-run `scripts/bridge_live_vault.sh status`.

## Using the client

```bash
# List a safe dir
python scripts/obsidian_client.py list 03-Skills

# Read a specific note
python scripts/obsidian_client.py read "01-Projects/School/school-core/docs/agents/issue-tracker.md"

# Search the whole safe vault
python scripts/obsidian_client.py search school-core

# Bootstrap (CONTEXT.md → Welcome.md → _MATRIX.md)
python scripts/obsidian_client.py bootstrap
```

Env overrides:
- `OBSIDIAN_API_KEY` (or `AGENT_SCHOOL_OBSIDIAN_KEY`) — required.
- `OBSIDIAN_BASE_URL` — default `https://100.81.210.96:27124`.
- `OBSIDIAN_SOCKS5` — default `localhost:1080`.

## Safety (implicit allowlist)

The client is **read-only** and **folder-confined**:
- Allowed: `01-Projects` (except `Brandon Career`), `02-Agents`, `03-Skills`,
  `04-Reference`, `99-Templates`, `Bases`, `docs`, `engram`, `job-targets`,
  `omniroute`, `scripts`, and whitelisted root files
  (`AGENTS.md`, `_CLAUDE.md`, `CONTEXT.md`, `Welcome.md`, `index.md`, etc.).
- **Hard-blocked (personal):** `00-Inbox`, `05-Daily`, `06-Archive`,
  `01-Projects/Brandon Career`.

The write surface (`vault_write`, `vault_patch`, `vault_delete`, commands) is
**never exposed** by this client. Adding a write path later is a deliberate,
separate decision.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ConnectionError` to 100.81.210.96:27124 | tailscaled not running, or `--socks5-server` missing | Restart with the flag; verify `tailscale status` shows mac online |
| SOCKS5 `cannot complete connection` (code 1) | tailnet cannot route to the Mac IP:port | Verify mac IP matches; check macOS firewall / app bound to `0.0.0.0` |
| HTTPS cert errors | self-signed plugin cert | client uses unverified TLS context (built-in) |
| HTTP 401 | wrong/expired API key | Re-copy from plugin settings |
| Obsidian HTTP 200 but grep shows nothing | The `00-Inbox`/`05-Daily` gate (blocked by design) | Use an allowed path |
| `curl` works but `python` client fails | client is pure stdlib | no pip install needed — run `python scripts/obsidian_client.py doctor` |

## Why not... (decisions)

- **Reverse SSH / relay host** — extra moving parts; needs another always-on
  machine. Tailscale is the mesh.
- **Direct TLS to the Mac over the Internet** — no public exposure; tailnet is
  encrypted.

## Orca remote runtime bridge (same tailnet)

The container can also reach the **Orca runtime on the Mac** over the same
SOCKS5 tunnel, giving live visibility into in-flight worktrees without running
the Electron desktop here.

```text
school-core container
  └─ data/orca-cli/<ver>/            (plain-node Orca CLI, extracted from .deb once)
       ├─ scripts/orca_cli.sh        (boots/uses the CLI; sets ORCA_USER_DATA_PATH)
       └─ scripts/socks5_tcp_forward.py  (127.0.0.1:6768 -> Mac:6768 via SOCKS5)
            └─ scripts/orca_pair.sh down   (one-shot: forwarder + saved env)
                 └─ Orca (Mac) runtime — ws://<mac>:6768
```

Why the forwarder is needed: the Orca CLI dials its remote WebSocket endpoint
directly and ignores proxy env vars, so we make the endpoint look local
(`127.0.0.1:6768`) and carry the bytes over the same SOCKS5 proxy Obsidian uses.

### Setup (one time, then durable)

```bash
# 1. Tailnet must be up (see above; same bridge).
# 2. Pair once. The pairing code comes from Pairing > mac server on the Mac's
#    Orca IDE. It registers the env and persists to data/orca-config/.
export ORCA_PAIRING_CODE='orca://pair?code=...'
scripts/orca_pair.sh up

# 3. Use it (both are durable after this):
scripts/orca_cli.sh worktree list --environment mac   # live worktrees on the Mac
scripts/orca_cli.sh status --environment mac
```

### Durable paths + secrets

- CLI bundle: `data/orca-cli/<version>/` (auto-downloads `.deb`, extracts to
  `out/` + `app.asar` + `node_modules`; ~182MB).
- Paired env (contains the **E2EE device token** for the Mac): 
  `data/orca-config/orca-environments.json`, written by the CLI which respects
  `ORCA_USER_DATA_PATH`. **Do not commit, keep chmod 600.**
- The forwarder is `chmod 700` state and binds `127.0.0.1` only.

### Troubleshooting (Orca)

| Symptom | Likely cause | Fix |
|---|---|---|
| `Could not connect to the remote Orca runtime` | Tailnet down, or forwarder not running | `scripts/bridge_live_vault.sh status`; then `scripts/orca_pair.sh up` |
| `Use either --pairing-code or --environment, not both` | `ORCA_PAIRING_CODE` still exported in the shell | `unset ORCA_PAIRING_CODE` |
| `Cannot find module 'zod'` | `orca_cli.sh` was run without its bundled node_modules | Use `scripts/orca_cli.sh`, not bare `node .../index.js` |
| Client dials `ws://100.x` directly | stale endpoint in the saved env | `orca_pair.sh up` re-points it to `127.0.0.1:6768` |
| `orca` on PATH is the GNOME screen reader | Linux name clash | always use `scripts/orca_cli.sh` |