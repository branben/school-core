#!/usr/bin/env bash
# scripts/orca_pair.sh — live Orca bridge to your Mac's remote Orca runtime.
#
# One-time (or on container restart):
#   scripts/orca_pair.sh up [--code orca://pair?code=...]
#   # OR: ORCA_PAIRING_CODE='orca://pair?code=...' scripts/orca_pair.sh up
#
# Orca's CLI dials ws://<mac-tailnet-ip>:6768 directly (no proxy env support),
# so we:
#   1. start a local SOCKS5 forwarder (127.0.0.1:6768 -> mac:6768 via the
#      Obsidian SOCKS5 tunnel at localhost:1080),
#   2. register a saved Orca environment whose endpoint is the *local* forwarder.
#
# Requires: the Obsidian bridge live (scripts/bridge_live_vault.sh status → 200).
# Persists: saved environment in data/orca-config/ (via ORCA_USER_DATA_PATH,
# set by scripts/orca_cli.sh); forwarder must be re-run on container restart,
# but no re-pairing needed.

set -euo pipefail

MAC_IP="${ORCA_MAC_IP:-100.81.210.96}"
PORT="${ORCA_PORT:-6768}"
SOCKS5="${ORCA_SOCKS5:-localhost:1080}"
LOCAL_PORT="${ORCA_LOCAL_PORT:-$PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORWARDER="$SCRIPT_DIR/socks5_tcp_forward.py"
CLI="$SCRIPT_DIR/orca_cli.sh"
UP_DIR="/workspace/project/school-core/data/orca-config"
# Orca user-data is persisted under /workspace by orca_cli.sh (ORCA_USER_DATA_PATH).
# The saved environment store lives there; this script only needs to discover it.
ENV_FILE="$UP_DIR/orca-environments.json"

# Parse: up [--code <pairing-code>]
PAIRING_CODE="${ORCA_PAIRING_CODE:-}"
if [ "${1:-}" = "up" ] && [ "${2:-}" = "--code" ] && [ -n "${3:-}" ]; then
    PAIRING_CODE="$3"
fi

log() { printf '[orca] %s\n' "$*" >&2; }

find_env_name() {
    "$CLI" environment list --json 2>/dev/null | node -e '
        let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
            try { const j=JSON.parse(s);
                const e=(j.environments||j.result?.environments||[]);
                e.forEach(x=>console.log(x.name));
            } catch(_){}
        })' | grep -v '^$' | head -1
}

forwarder_pid() {
    pgrep -f "python3? $FORWARDER|$FORWARDER --listen" | head -1
}

ensure_forwarder() {
    local pid
    pid=$(forwarder_pid) || true
    if [ -n "$pid" ]; then
        log "forwarder already running (pid $pid)"
        return
    fi
    log "starting SOCKS5 forwarder on 127.0.0.1:$LOCAL_PORT -> $MAC_IP:$PORT"
    ("$FORWARDER" --listen "127.0.0.1:$LOCAL_PORT" --target "$MAC_IP:$PORT" --socks5 "$SOCKS5" > "$UP_DIR/orca_forward.log" 2>&1 &)
    sleep 1.5
    pid=$(forwarder_pid) || true
    if [ -n "$pid" ]; then
        log "forwarder up (pid $pid)"
    else
        log "forwarder FAILED to start — see $UP_DIR/orca_forward.log"
    fi
}

case "${1:-help}" in
    up)
        ensure_forwarder
        # re-point saved env to the local forwarder (idempotent)
        if [ -n "$PAIRING_CODE" ]; then
            if [ -f "$ENV_FILE" ] && grep -q '"name": *"mac"\|"name":"mac"' "$ENV_FILE" 2>/dev/null; then
                log "environment 'mac' already saved — reusing (code ignored)"
                MAC_ENV_SAVED=1
            else
                log "registering environment 'mac' from pairing code"
                if "$CLI" environment add --name mac --pairing-code "$PAIRING_CODE" >&2; then
                    MAC_ENV_SAVED=1
                else
                    log "environment add failed; will try existing store"
                    [ -f "$ENV_FILE" ] && MAC_ENV_SAVED=1 || MAC_ENV_SAVED=""
                fi
            fi
        elif [ -f "$ENV_FILE" ]; then
            log "using saved environment"
            MAC_ENV_SAVED=1
        else
            log "no saved environment and no --code given"
            MAC_ENV_SAVED=""
        fi
        # re-point endpoint ws://100... -> ws://127.0.0.1  (input JSON is minified)
        if [ -n "$MAC_ENV_SAVED" ]; then
            python3 - "$ENV_FILE" "$MAC_IP" "$LOCAL_PORT" <<'PY'
import json, sys
p, old_ip, new_port = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(p))
for e in d.get("environments", []):
    for ep in e.get("endpoints", []):
        if ep.get("endpoint", "").startswith("ws://"):
            ep["endpoint"] = f"ws://127.0.0.1:{new_port}"
json.dump(d, open(p, "w"))
print(f"  endpoint -> ws://127.0.0.1:{new_port}")
PY
        fi
        log "verifying (mac provider list)…"
        timeout 30 "$CLI" host list --json 2>/dev/null | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s);(j.result?.hosts||j.hosts||[]).forEach(h=>console.log("  ",h.name||h.id,"->",h.kind||""))})' || true
        # forwarder sanity check
        pid=$(forwarder_pid) || true
        if [ -n "$pid" ]; then
            log "forwarder up (pid $pid)"
        else
            log "forwarder NOT running — see $UP_DIR/orca_forward.log"
        fi
        echo "Orca bridge: forwarder on 127.0.0.1:$LOCAL_PORT -> $MAC_IP:$PORT. Run: scripts/orca_cli.sh worktree list --environment mac"
        ;;
    stop)
        log "killing forwarder"
        pid=$(forwarder_pid) || true
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
        log "stopped"
        ;;
    status)
        pgrep -af "$FORWARDER" 2>/dev/null | head -3 || echo "forwarder: not running"
        "$CLI" host list --json 2>/dev/null | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const j=JSON.parse(s);console.log("hosts:",JSON.stringify(j.result?.hosts||j.hosts||[]))}catch(_){console.log("hosts: (n/a)")}})' || echo "hosts: cli unavailable"
        ;;
    *)
        echo "usage: $0 up|stop|status"
        ;;
esac