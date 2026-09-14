#!/usr/bin/env bash
# scripts/install_context_tools.sh — install envit + ripwire CLI + ripwire skills.
#
# Single source of truth for the context/skills toolchain declared in
# envit.json / envit.lock.json. The binaries are NEVER tracked in git; they
# live under data/tools/ (chmod 700, SHA-verified, durable) exactly like the
# orca-cli precedent. CI (context.yml) runs the same script, so local and CI
# can never drift.
#
# Usage:
#   scripts/install_context_tools.sh          # install both tools into data/tools/
#   CI=1 scripts/install_context_tools.sh      # install into /tmp/context-tools (CI)
#   CONTEXT_TOOLS_DIR=/custom/path scripts/install_context_tools.sh
#
# After install:
#   data/tools/envit sync --frozen             # materialize declared skills
#   data/tools/ripwire --scan-skills <dir>      # security-scan skills before use

set -euo pipefail

# Durable install dir under the repo (survives pod recycle; $HOME /tmp are overlay).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${CONTEXT_TOOLS_DIR:-$REPO_ROOT/data/tools}"
# CI installs into a throwaway dir unless CONTEXT_TOOLS_DIR is overridden.
[ "${CI:-}" = "1" ] && INSTALL_DIR="${CONTEXT_TOOLS_DIR:-/tmp/context-tools}"

ENVIT_VERSION="${ENVIT_VERSION:-v0.1.0}"
RIPWIRE_VERSION="${RIPWIRE_VERSION:-v0.6.0}"

log() { printf '[context-tools] %s\n' "$*" >&2; }

mkdir -p "$INSTALL_DIR"

# --- envit ---------------------------------------------------------------
install_envit() {
    local bin="$INSTALL_DIR/envit"
    if [ -x "$bin" ] && "$bin" --version >/dev/null 2>&1; then
        log "envit already installed ($("$bin" --version))"
        return
    fi
    local asset="envit-x86_64-unknown-linux-musl.tar.gz"
    local url="https://github.com/plannotator/envit/releases/download/${ENVIT_VERSION}/${asset}"
    local tarball="/tmp/${asset}"
    log "downloading envit ${ENVIT_VERSION} …"
    curl -fsSL -o "${tarball}" "${url}"
    curl -fsSL -o "${tarball}.sha256" "${url}.sha256"
    local sum; sum="$(awk '{print $1}' "${tarball}.sha256")"
    ( cd "$(dirname "${tarball}")" && printf '%s  %s\n' "$sum" "${tarball##*/}" | sha256sum -c - )
    tar xzf "${tarball}" -C "$INSTALL_DIR" envit
    chmod 700 "$INSTALL_DIR"
    log "envit ${ENVIT_VERSION} → $INSTALL_DIR/envit"
}

# --- ripwire --------------------------------------------------------------
install_ripwire() {
    local dir="$INSTALL_DIR/ripwire-${RIPWIRE_VERSION#v}-linux-x64"
    if [ -x "$dir/ripwire" ]; then
        log "ripwire already installed ($("$dir/ripwire" --version 2>/dev/null || echo "$RIPWIRE_VERSION"))"
        return
    fi
    local asset="ripwire-${RIPWIRE_VERSION#v}-linux-x64.tar.gz"
    local url="https://github.com/redhat-et/ripwire/releases/download/${RIPWIRE_VERSION}/${asset}"
    local tarball="/tmp/${asset}"
    log "downloading ripwire ${RIPWIRE_VERSION} …"
    curl -fsSL -o "${tarball}" "${url}"
    curl -fsSL -o "${tarball}.sha256" "${url}.sha256"
    local sum; sum="$(awk '{print $1}' "${tarball}.sha256")"
    ( cd "$(dirname "${tarball}")" && printf '%s  %s\n' "$sum" "${tarball##*/}" | sha256sum -c - )
    tar xzf "${tarball}" -C "$INSTALL_DIR"
    chmod 700 "$INSTALL_DIR"
    log "ripwire ${RIPWIRE_VERSION} → $dir/ripwire"
}

install_envit
install_ripwire

log "done. envit=$INSTALL_DIR/envit ripwire=$INSTALL_DIR/ripwire-${RIPWIRE_VERSION#v}-linux-x64/ripwire"
log "next: $INSTALL_DIR/envit sync --frozen"