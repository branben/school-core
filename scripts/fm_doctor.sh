#!/usr/bin/env bash
# scripts/fm_doctor.sh — one-command diagnostic for the FirstMate → Orca → Hermes
# crew-spawn chain. Run this when a spawn fails or before dispatching a crew task;
# it checks every external dependency that fm-spawn.sh (and the spawned hermes
# agent) relies on, so failures are diagnosable in one shot instead of from a
# cryptic mid-spawn error.
#
# What it checks:
#   1. Persistent FirstMate clone        — ~/.local/share/firstmate (+ fm-spawn.sh,
#                                          fm-teardown.sh, the orca backend adapter)
#   2. Orca daemon                       — `orca status --json` reports ok + ready runtime
#   3. FM_HOME config                    — backend=orca recorded, state/ + data/ exist
#   4. Hermes launch wrapper + runtime   — wrapper executable, hermes CLI present
#   5. Hermes model auth                 — the configured provider has a token; the
#                                          known blocker is an empty nous credential pool
#                                          ("No access token found for Nous Portal login")
#
# Exit codes (cron/friendly):
#   0  all checks pass
#   1  one or more checks FAILED (fixable; see the hint lines)
#   2  invocation error (missing python3 / orca / required file)
#
# All telemetry goes to STDOUT in plain "PASS/FAIL/INFO" lines (stderr reserved
# for fatal invocation errors).

set -uo pipefail

# Invocation guard: the script's parsers need python3 and orca. Missing tooling
# is an invocation error (exit 2), not a check failure — mirroring the repo's
# existing scripts/terminal_drift_check.sh contract.
for _tool in python3 orca; do
  command -v "$_tool" >/dev/null 2>&1 || {
    echo "error: required tool '$_tool' is not on PATH (fm_doctor needs python3 + orca)" >&2
    exit 2
  }
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Overridable paths (env) so the doctor works on another machine/clone ---
FM_CLONE="${FM_CLONE:-$HOME/.local/share/firstmate}"
FM_HOME="${FM_HOME:-$HOME/.hermes/school-core-fm-config}"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
FM_WRAPPER="${FM_WRAPPER:-$HOME/.local/bin/hermes-fm-wrapper}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

PASS=0
FAIL=0
WARN=0

note() { printf '%s\n' "$*"; }
pass() { note "PASS  $1"; PASS=$((PASS + 1)); }
fail() { note "FAIL  $1"; FAIL=$((FAIL + 1)); }
warn() { note "WARN  $1"; WARN=$((WARN + 1)); }

# ---- 1. Persistent FirstMate clone ----
note "── FirstMate clone (persistent) ──────────────────────────────"
if [ -d "$FM_CLONE" ]; then
  pass "clone exists: $FM_CLONE"
  if [ -x "$FM_CLONE/bin/fm-spawn.sh" ]; then
    pass "fm-spawn.sh present + executable"
  else
    fail "fm-spawn.sh missing or not executable — re-clone: git clone --depth 1 https://github.com/kunchenguid/firstmate.git $FM_CLONE"
  fi
  if [ -x "$FM_CLONE/bin/fm-teardown.sh" ]; then
    pass "fm-teardown.sh present + executable"
  else
    fail "fm-teardown.sh missing"
  fi
  if [ -f "$FM_CLONE/bin/backends/orca.sh" ]; then
    pass "orca backend adapter present"
  else
    fail "orca backend adapter missing (bin/backends/orca.sh)"
  fi
else
  fail "clone NOT found at $FM_CLONE — the school's crew dispatch can't run. Re-clone: git clone --depth 1 https://github.com/kunchenguid/firstmate.git $FM_CLONE"
fi

# ---- 2. Orca daemon ----
note "── Orca daemon ────────────────────────────────────────────────"
if ! command -v orca >/dev/null 2>&1; then
  fail "orca CLI not on PATH — Orca must be installed + the daemon running"
else
  if out=$(orca status --json 2>/dev/null); then
    ok=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok", False))' 2>/dev/null || echo false)
    if [ "$ok" = "True" ]; then
      pass "orca status --json → ok"
    else
      fail "orca status reports not-ok — is the Orca app running?"
    fi
    # runtime reachability (what the orca backend adapter actually gates on)
    runtime=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    r = (json.load(sys.stdin).get("result") or {}).get("runtime") or {}
    print("reachable=%s state=%s" % (r.get("reachable"), r.get("state", "?")))
except Exception:
    print("unknown")
' 2>/dev/null || echo unknown)
    case "$runtime" in
      reachable=True\ state=ready) pass "orca runtime ready ($runtime)" ;;
      *) warn "orca runtime state: $runtime (spawn may refuse until ready)" ;;
    esac
  else
    fail "orca status failed to run — start the Orca daemon first"
  fi
fi

# ---- 3. FM_HOME config ----
note "── FM_HOME (crew config) ──────────────────────────────────────"
if [ -d "$FM_HOME" ]; then
  pass "FM_HOME exists: $FM_HOME"
  if [ -f "$FM_HOME/backend" ] && grep -q 'orca' "$FM_HOME/backend" 2>/dev/null; then
    pass "backend = $(cat "$FM_HOME/backend")"
  else
    warn "backend file missing/not orca — spawn may auto-detect tmux instead"
  fi
  [ -d "$FM_HOME/state" ] && pass "state/ dir present" || warn "state/ missing (created on first spawn)"
  [ -d "$FM_HOME/data" ]  && pass "data/ dir present"  || warn "data/ missing (created on first spawn)"
else
  fail "FM_HOME not found: $FM_HOME — see campus.md 'Ops note'"
fi

# ---- 4. Hermes wrapper + runtime ----
note "── Hermes wrapper + runtime ───────────────────────────────────"
if [ -x "$FM_WRAPPER" ]; then
  pass "wrapper executable: $FM_WRAPPER"
else
  fail "wrapper missing/not executable: $FM_WRAPPER — see skill firstmate-orca-spawn-hermes (Launch Wrapper)"
fi
if [ -x "$HERMES_BIN" ] || command -v hermes >/dev/null 2>&1; then
  pass "hermes CLI available"
else
  fail "hermes CLI not found ($HERMES_BIN)"
fi

# ---- 5. Hermes model auth ----
note "── Hermes model auth ──────────────────────────────────────────"
if [ -f "$HERMES_HOME/auth.json" ]; then
  auth_state=$(python3 - "$HERMES_HOME/auth.json" <<'PY'
import json, sys

try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("ERR:unreadable-or-malformed")
    raise SystemExit(0)
provider = d.get("active_provider", "")
pool = (d.get("credential_pool") or {}).get(provider)
n = len(pool) if isinstance(pool, list) else (1 if pool else 0)
print("%s:%d" % (provider, n))
PY
  )
  if [ "$auth_state" = "ERR:unreadable-or-malformed" ]; then
    fail "auth.json is unreadable or malformed ($HERMES_HOME/auth.json) — inspect it, then run 'hermes model' to re-configure"
  else
    provider="${auth_state%%:*}"
    n="${auth_state##*:}"
    if [ -n "$provider" ] && [ "$n" -ge 1 ]; then
      pass "active provider '$provider' has $n credential(s)"
    else
      fail "active provider '$provider' has NO credential — spawns launch but the agent dies with 'No access token found' (fix: hermes model)"
    fi
  fi
else
  fail "auth.json missing ($HERMES_HOME/auth.json) — run 'hermes model' to configure a provider"
fi

# ---- Summary ----
note ""
note "── Summary ────────────────────────────────────────────────────"
note "  ${PASS} passed, ${WARN} warned, ${FAIL} failed"
if [ "$FAIL" -gt 0 ]; then
  note "  Fix the FAIL items above, then re-run."
  exit 1
fi
exit 0
