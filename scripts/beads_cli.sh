#!/usr/bin/env bash
# scripts/beads_cli.sh — make the bd (beads) CLI available on PATH, durably.
#
# bd is a Go binary (~154 MB) that must NOT live in $HOME: $HOME is a
# container overlay and is wiped on pod recycle. The durable copy lives
# under the repo's data/ (real disk, gitignored):
#     data/beads-cli/bd
# If the binary is missing, re-download it from the official GitHub release.
#
# Usage (source once per session, or from ~/.profile-like bootstrap):
#     eval "$(bash scripts/beads_cli.sh env)"   # prints PATH export
#   or just run it:                             # idempotent, installs if missing
#     scripts/beads_cli.sh ensure
#
# Requires: curl, gzip, sha256sum. No Go toolchain needed (prebuilt release).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEADS_DIR="${BEADS_DIR:-$REPO_ROOT/data/beads-cli}"
BIN="$BEADS_DIR/bd"
VERSION="${BEADS_VERSION:-v1.3.4}"
URL="https://github.com/gastownhall/beads/releases/download/$VERSION/beads_${VERSION#v}_linux_amd64.tar.gz"

# Durable bootstrap: we make a PATH entry for the BEADS_DIR itself.
add_to_path() {
  case ":$PATH:" in
    *":$BEADS_DIR:"*) ;;
    *) export PATH="$BEADS_DIR:$PATH" ;;
  esac
}

# Official published sha256 of beads_<ver>_linux_amd64.tar.gz (checksums.txt).
checksum_for() {
  case "$VERSION" in
    v1.3.4) echo "175c26904bde3ff9168d2282ff6b56f17b17f60c22fa5acb3dacc9fb3465303a" ;;
    *) echo "" ;;
  esac
}

ensure() {
  if [ -x "$BIN" ]; then
    add_to_path
    return 0
  fi
  echo "bd not found at $BIN — downloading official release $VERSION ..." >&2
  mkdir -p "$BEADS_DIR"
  local tmp sha tarball
  tmp="$(mktemp -d)"
  tarball="$tmp/beads.tar.gz"
  curl -fsSL -o "$tarball" "$URL"
  sha="$(checksum_for)"
  if [ -n "$sha" ]; then
    echo "$sha  $tarball" | sha256sum -c - >/dev/null || { echo "checksum mismatch for $VERSION" >&2; rm -rf "$tmp"; exit 1; }
  else
    echo "no pinned checksum for $VERSION — skipping verification" >&2
  fi
  tar -xzf "$tarball" -C "$tmp" bd
  mv -f "$tmp/bd" "$BIN"
  chmod +x "$BIN"
  rm -rf "$tmp"
  add_to_path
  echo "installed: $BIN" >&2
}

case "${1:-ensure}" in
  env)
    ensure
    echo "export PATH=\"\$PATH:$BEADS_DIR\""
    ;;
  ensure)
    ensure
    bd --version >/dev/null 2>&1 && echo "ok: $(bd --version 2>&1 | head -1)"
    ;;
  *)
    echo "usage: $0 [ensure|env]" >&2
    exit 2
    ;;
esac