#!/usr/bin/env bash
# run_agentmail_poller.sh — launchd wrapper for the AgentMail inbound poller.
# Sources the repo .env, then executes a single poller run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ -f "${ENV_FILE}" ]]; then
    set -a
    source "${ENV_FILE}"
    set +a
fi

exec "${REPO_ROOT}/.venv/bin/python" "${REPO_ROOT}/src/agentmail_poller.py"
