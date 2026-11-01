#!/usr/bin/env bash
# fresh_device_check.sh — clean-device recovery path for school-core.
#
# From a clean checkout this script:
#   1. materializes .env from .env.example
#   2. creates a Python venv and installs requirements.txt
#   3. enters the Nix verifyShell (flake.nix)
#   4. runs bd prime to restore durable state
#   5. runs the pytest suite
#   6. runs verify_gate against a known public repo
#
# Usage:
#   bash scripts/fresh_device_check.sh
#   bash scripts/fresh_device_check.sh --target owner/repo   # override default
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_REPO="${1:-branben/school-core}"

echo "[fresh-device] root: ${REPO_ROOT}"
echo "[fresh-device] target: ${TARGET_REPO}"

# ── 1. .env materialization ──────────────────────────────────────────
if [[ ! -f "${REPO_ROOT}/.env" && -f "${REPO_ROOT}/.env.example" ]]; then
    echo "[fresh-device] materializing .env from .env.example"
    cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
    echo "[fresh-device] .env created — fill in real values before production use"
fi

# ── 2. venv + deps ───────────────────────────────────────────────────
if [[ ! -d "${REPO_ROOT}/.venv" ]]; then
    echo "[fresh-device] creating venv"
    python3 -m venv "${REPO_ROOT}/.venv"
fi
# shellcheck disable=SC1091
source "${REPO_ROOT}/.venv/bin/activate"
python -m pip install --upgrade pip >/dev/null 2>&1 || true
pip install -r "${REPO_ROOT}/requirements.txt" -q
# Make the src/ package importable without installation.
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
echo "[fresh-device] dependencies installed"

# ── 3. Nix develop ───────────────────────────────────────────────────
if ! command -v nix >/dev/null 2>&1; then
    echo "[fresh-device] WARNING: nix not found — verify_gate will skip"
else
    echo "[fresh-device] entering Nix verifyShell for verify_gate"
    # We do NOT stay in nix develop; we just confirm the shell builds.
    nix build "${REPO_ROOT}#verifyShell" --no-link 2>/dev/null || \
        nix develop "${REPO_ROOT}#verifyShell" -c true
fi

# ── 4. bd prime ─────────────────────────────────────────────────────
if command -v bd >/dev/null 2>&1; then
    echo "[fresh-device] running bd prime"
    bd prime 2>&1 | head -5 || true
else
    echo "[fresh-device] bd not installed — skipping prime"
fi

# ── 5. pytest ───────────────────────────────────────────────────────
# Pytest is a sanity check, not a gate for recovery — live integration
# tests may fail on a clean device without external services. Continue
# to verify_gate regardless.
echo "[fresh-device] running pytest"
pytest -q --basetemp=/tmp/school-core-pytest || echo "[fresh-device] pytest had failures (see above) — continuing to verify_gate"

# ── 6. verify_gate on known repo ────────────────────────────────────
echo "[fresh-device] running verify_gate on ${TARGET_REPO}"
python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, '${REPO_ROOT}')
from src.core.repo_reader import clone_repo
from src.review.verify_gate import run_verify_gate, freeze_verification_contract
repo = clone_repo('${TARGET_REPO}')
if repo is None:
    print('[fresh-device] clone failed — cannot run verify_gate')
    sys.exit(1)
contract = freeze_verification_contract(repo)
result = run_verify_gate(repo, trusted_contract=contract)
print(result)
sys.exit(0 if result['passed'] else 1)
"

echo "[fresh-device] all stages complete"
