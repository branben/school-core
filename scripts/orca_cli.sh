#!/usr/bin/env bash
# scripts/orca_cli.sh — run the Orca CLI (plain node) without the Electron desktop.
#
# The Orca desktop app bundles its CLI at out/cli/index.js, run via
# ELECTRON_RUN_AS_NODE. We run that same JS with the system `node` — no Electron,
# no desktop, works headless. The CLI talks to a *remote* Orca runtime (your Mac)
# over WebSocket, so no local Orca daemon is needed either.
#
# The CLI needs `out/` + `app.asar` (~124MB vendored in ~/.cache/orca-cli). If
# missing, it boots by downloading the Orca .deb and extracting just those two.
#
# Usage:  scripts/orca_cli.sh <command> [args...]
#         ORCA_CLI_VERSION=v1.4.200 scripts/orca_cli.sh worktree list

set -euo pipefail

VERSION="${ORCA_CLI_VERSION:-v1.4.200}"
# Durable cache lives under /workspace (real disk, survives pod recycle);
# $HOME and /tmp are overlay and get wiped.
CACHE_DIR="${ORCA_CLI_CACHE:-/workspace/project/school-core/data/orca-cli}"
INSTALL="$CACHE_DIR/$VERSION"
CLI_JS="$INSTALL/resources/out/cli/index.js"
DEB_URL="https://github.com/stablyai/orca/releases/download/${VERSION}/orca-ide_${VERSION#v}_amd64.deb"

log() { printf '[orca-cli] %s\n' "$*" >&2; }

if [ ! -f "$CLI_JS" ]; then
    log "Orca CLI $VERSION not found at $INSTALL — bootstrapping…"
    mkdir -p "$INSTALL/resources"
    DEB="$CACHE_DIR/orca-$VERSION.deb"

    if [ ! -f "$DEB" ]; then
        log "downloading $VERSION …"
        command -v curl >/dev/null 2>&1 || { log "curl required"; exit 1; }
        curl -fsSL -o "$DEB.part" "$DEB_URL"
        mv -f "$DEB.part" "$DEB"
    fi

    log "extracting …"
    command -v dpkg-deb >/dev/null 2>&1 || { log "dpkg-deb required (dpkg)"; exit 1; }
    rm -rf "$INSTALL/.tmp"
    mkdir -p "$INSTALL/.tmp"
    dpkg-deb -x "$DEB" "$INSTALL/.tmp"
    mv -f "$INSTALL/.tmp/opt/Orca/resources/app.asar.unpacked/out" "$INSTALL/resources/out"
    mv -f "$INSTALL/.tmp/opt/Orca/resources/app.asar" "$INSTALL/resources/app.asar"
    # CLI deps (zod, ws, …) live in resources/node_modules in the real app.
    mv -f "$INSTALL/.tmp/opt/Orca/resources/node_modules" "$INSTALL/resources/node_modules"
    rm -rf "$INSTALL/.tmp"
    log "installed to $INSTALL"
fi

# Make the CLI resolve its bundled third-party deps (same layout as the app).
export NODE_PATH="$INSTALL/resources/node_modules${NODE_PATH:+:$NODE_PATH}"

# Persist Orca user-data (environment store, E2EE keys, device tokens, metadata)
# under /workspace — the real disk. Default is ~/.config/orca (overlay, wiped).
: "${ORCA_USER_DATA_PATH:=/workspace/project/school-core/data/orca-config}"
export ORCA_USER_DATA_PATH

exec node "$CLI_JS" "$@"