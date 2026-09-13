#!/usr/bin/env bash
# Re-establish the live vault bridge (Tailscale userspace + SOCKS5 -> Obsidian).
#
# The container's /tmp is wiped on restart, so tailscaled state, the binary,
# and the SOCKS5 proxy all need to be rebuilt. This script does the whole
# container-side setup and prints the auth URL for the human to open once.
#
# Usage:
#   scripts/bridge_live_vault.sh          # full setup (downloads + daemon + auth url)
#   scripts/bridge_live_vault.sh status   # check tailnet + Obsidian reachability
#
# After auth, export OBSIDIAN_API_KEY=<key from plugin settings> (the script
# prints a reminder). See docs/ops/live-vault-bridge.md for the runbook.
set -euo pipefail

# Durable state lives under /workspace (real disk; survives pod recycle). /tmp
# and $HOME are container overlay and get wiped — never put ts state there.
TS_DATA_DIR="${TS_DATA_DIR:-/workspace/project/school-core/data/tailscale}"
TS_VERSION="${TS_VERSION:-1.76.1}"
TS_ARCH="${TS_ARCH:-amd64}"
TS_DIR="${TS_DIR:-$TS_DATA_DIR/tsbin}"
TS_STATE="${TS_STATE:-$TS_DATA_DIR/ts-state}"
TS_SOCK="${TS_SOCK:-$TS_DATA_DIR/ts.sock}"
SOCKS5_PORT="${SOCKS5_PORT:-localhost:1080}"
HTTP_PROXY_PORT="${HTTP_PROXY_PORT:-localhost:8080}"
OBSIDIAN_BASE="${OBSIDIAN_BASE:-https://100.81.210.96:27124}"
MAC_IP="${MAC_IP:-100.81.210.96}"

cmd_status() {
  echo "== tailscale status =="
  "$TS_DIR/tailscale" --socket="$TS_SOCK" status 2>&1 | head -12 || true
  echo
  echo "== Obsidian reachability =="
  curl -sk --socks5-hostname "$SOCKS5_PORT" --max-time 10 "$OBSIDIAN_BASE/" \
    -w "HTTP %{http_code}\n" 2>&1 | tail -1
}

cmd_up() {
  echo "== locating/downloading tailscale $TS_VERSION $TS_ARCH =="
  mkdir -p "$TS_DIR" "$TS_DATA_DIR"
  if [ ! -x "$TS_DIR/tailscaled" ]; then
    # Reuse an existing extracted build if present (durable path or leftover).
    local existing
    existing=""
    if [ -x "$TS_DIR/tailscaled" ]; then
      existing="$TS_DIR"
    elif [ -x /tmp/tailscale_*/tailscaled ]; then
      existing=$(ls -d /tmp/tailscale_* 2>/dev/null | head -1)
      echo "  reusing existing binary at $existing"
      cp -f "$existing"/tailscale* "$TS_DIR"/
    fi
    if [ ! -x "$TS_DIR/tailscaled" ]; then
      echo "  downloading tailscale $TS_VERSION"
      local tgz="$TS_DATA_DIR/ts-${TS_VERSION}.tgz"
      curl -sfL -o "$tgz" "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_${TS_ARCH}.tgz"
      tar xzf "$tgz" -C "$TS_DIR" --strip-components=1
    fi
  fi

  echo "== starting tailscaled with SOCKS5 proxy (userspace) =="
  # A stale tailscaled (e.g. leftover from an old /tmp layout) holds the SOCKS5
  # port and shows up in a loose pgrep. Kill any daemon whose socket args differ
  # from ours, or that isn't actually listening on our socket.
  local running_pids
  running_pids=$(pgrep -f "tailscaled" || true)
  if [ -n "$running_pids" ]; then
    local sock_ok=0
    for pid in $running_pids; do
      if [ -S "$TS_SOCK" ] && /proc/$pid/status >/dev/null 2>&1 \
         && (tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "--socket=$TS_SOCK"); then
        # exact socket match — confirm it answers
        if "$TS_DIR/tailscale" --socket="$TS_SOCK" status >/dev/null 2>&1; then
          sock_ok=1
        else
          echo "  tailscaled pid $pid not answering on $TS_SOCK; killing"
          kill "$pid" 2>/dev/null || true
        fi
      else
        echo "  stale tailscaled pid $pid (wrong socket); killing"
        kill "$pid" 2>/dev/null || true
      fi
    done
    sleep 1
    if [ "$sock_ok" = "1" ]; then
      echo "tailscaled already running and healthy (socket $TS_SOCK)."
    fi
  fi
  # Any live (non-zombie) tailscaled left after cleanup? Start one only if none.
  local live_pids
  live_pids=""
  for p in $(pgrep -f "tailscaled" || true); do
    state=$(ps -o stat= -p "$p" 2>/dev/null || echo X)
    case "$state" in
      Z*|X) continue ;;     # zombie or gone
      *) live_pids="$live_pids $p" ;;
    esac
  done
  if [ -z "$live_pids" ]; then
    nohup "$TS_DIR/tailscaled" \
      --tun=userspace-networking \
      --state="$TS_STATE" \
      --socket="$TS_SOCK" \
      --socks5-server="$SOCKS5_PORT" \
      --outbound-http-proxy-listen="$HTTP_PROXY_PORT" \
      > "$TS_DATA_DIR/tailscaled.log" 2>&1 &
    sleep 2
  fi

  echo "== bringing up tailscale =="
  # --timeout avoids an indefinite hang; if auth is needed we print the URL
  # and poll so the script stays useful after the human clicks it.
  local auth_out
  auth_out=$("$TS_DIR/tailscale" --socket="$TS_SOCK" up --timeout=8s 2>&1) || true
  if echo "$auth_out" | grep -q "authenticate\|To authenticate"; then
    echo ">>> ACTION NEEDED — open the auth URL below (same Tailscale account as the Mac):"
    echo "$auth_out" | grep -E "https?://[^ ]*" || echo "  (run: $TS_DIR/tailscale --socket=$TS_SOCK up)"
    echo ">>> Then re-run: $0 status   (or: $0 up)"
  else
    echo "tailscale up: $auth_out"
    cmd_status
  fi
}

case "${1:-up}" in
  up)     cmd_up ;;
  status) cmd_status ;;
  *) echo "usage: $0 [up|status]"; exit 1 ;;
esac