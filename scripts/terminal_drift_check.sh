#!/usr/bin/env bash
# scripts/terminal_drift_check.sh — drift check for empty-title Orca terminals.
#
# Designed to be invoked by cron (e.g. every 15 minutes); this script is
# ONE-SHOT. Each invocation:
#   1. Runs ``python3 conductor.py --gc-terminals --gc-terminals-dry-run``
#      inside the school-core checkout.
#   2. Parses the count of ``(no-title)`` candidates from the dry-run output.
#   3. Alerts via ``curl`` POST to ``$DRIFT_NOTIFY_URL`` when that count
#      exceeds ``$DRIFT_EMPTY_THRESHOLD`` (default 5).
#   4. Records the alert + timestamp in ``$DRIFT_STATE_PATH`` so a sustained
#      drift does NOT spam the alert channel — re-alerts only when the
#      count grew past the prior alerted count, OR the cooldown window
#      (``$DRIFT_ALERT_COOLDOWN_SEC``, default 3600s) has elapsed since
#      the last alert.
#
# Exit codes (cron-friendly):
#   0  count within budget → silent
#   1  count over threshold → silent (within cooldown & steady) OR alert sent
#   2  invocation error (jq/python3/curl missing, conductor.py missing,
#      dry-run subprocess failed, alert POST failed)
#
# All telemetry goes to STDERR so cron STDOUT capture stays empty on success.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-~/school-core}"
DRIFT_EMPTY_THRESHOLD="${DRIFT_EMPTY_THRESHOLD:-5}"
DRIFT_ALERT_COOLDOWN_SEC="${DRIFT_ALERT_COOLDOWN_SEC:-3600}"
DRIFT_NOTIFY_URL="${DRIFT_NOTIFY_URL:-}"
DRIFT_STATE_PATH="${DRIFT_STATE_PATH:-$HOME/.school-core/drift_alert_state.json}"
DRY_RUN=0

usage() {
    cat >&2 <<'EOF'
Usage: scripts/terminal_drift_check.sh [--dry-run] [--repo-root PATH] [--threshold N]

Env vars (overridden by flags):
  REPO_ROOT                    school-core checkout (default above)
  DRIFT_EMPTY_THRESHOLD        empty-title count to alert at (default 5)
  DRIFT_ALERT_COOLDOWN_SEC     min seconds between re-alerts (default 3600)
  DRIFT_NOTIFY_URL             POST endpoint for {kind:"drift_alert",...} JSON
  DRIFT_STATE_PATH             state file with last alerted count/timestamp
Flags:
  --dry-run                    print the count; never alert, never update state
  --repo-root PATH             override REPO_ROOT
  --threshold N                override DRIFT_EMPTY_THRESHOLD
  --help / -h                  this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=1; shift ;;
        --repo-root) REPO_ROOT="$2"; shift 2 ;;
        --threshold) DRIFT_EMPTY_THRESHOLD="$2"; shift 2 ;;
        --help|-h)   usage; exit 0 ;;
        *)           usage; exit 2 ;;
    esac
done

# ── Pre-flight: every dep must be present before we touch state ───────────
command -v jq     >/dev/null 2>&1 || { echo "ERROR: jq not in PATH"     >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not in PATH" >&2; exit 2; }
command -v curl   >/dev/null 2>&1 || { echo "ERROR: curl not in PATH"   >&2; exit 2; }
[[ -f "$REPO_ROOT/conductor.py" ]] || {
    echo "ERROR: conductor.py missing at $REPO_ROOT" >&2; exit 2;
}

# ── Run conductor dry-run, capture full output ────────────────────────────
if ! conductor_out="$(
    cd "$REPO_ROOT"
    python3 conductor.py --gc-terminals --gc-terminals-dry-run 2>&1
)"; then
    echo "ERROR: conductor.py --gc-terminals-dry-run failed" >&2
    echo "--- captured output ---" >&2
    printf '%s\n' "$conductor_out" >&2 || true
    exit 2
fi

# ── Sanity-check: dry-run output must contain the conductor summary line.
# If the shape changes (gc-terminals refactor, emoji swap, repr change) we
# MUST fail loud instead of silently counting 0 forever. ────────────────
if [[ "$conductor_out" != *"gc-terminals: closed"* ]]; then
    echo "ERROR: conductor dry-run output shape unknown (expected 'gc-terminals: closed' summary line)" >&2
    echo "--- captured output ---" >&2
    printf '%s\n' "$conductor_out" >&2 || true
    exit 2
fi

# ── Parse empty-title count from the dry-run ──────────────────────────────
# Each match line looks like:
#   ◌ 🔍 would close: '(no-title)' (handle=term_af4e9c42…)
# We grep on the literal title repr to avoid false-matches on tabs that
# merely have a "Terminal 1" or "agent-school-*" title (those have their
# own lines under a different prefix, handled separately by gc itself).
empty_count="$(printf '%s\n' "$conductor_out" \
    | grep -c "🔍 would close: '(no-title)'" || true)"

# Structured log line so an ops dashboard can ingest this from journald/syslog.
log_line="$(jq -nc \
    --argjson empty     "$empty_count" \
    --argjson threshold "$DRIFT_EMPTY_THRESHOLD" \
    --arg     when      "$(date -u +%FT%TZ)" \
    '{empty_count:$empty, threshold:$threshold, checked_at:$when}')"
echo "drift-check: $log_line" >&2

# ── Within budget → silent exit ───────────────────────────────────────────
if [[ "$empty_count" -le "$DRIFT_EMPTY_THRESHOLD" ]]; then
    exit 0
fi

# ── Over threshold → bounded alerting ─────────────────────────────────────
# Read prior state (defensive: missing file → empty object, jq tolerates it).
mkdir -p "$(dirname "$DRIFT_STATE_PATH")" 2>/dev/null || true
prev_state="{}"
[[ -f "$DRIFT_STATE_PATH" ]] && prev_state="$(cat "$DRIFT_STATE_PATH")" || true

prev_count="$(jq -r '.alerted_count // 0'      <<<"$prev_state")"
prev_when="$(jq -r  '.last_alerted_at // ""'   <<<"$prev_state")"

now_epoch="$(date -u +%s)"
cooldown_elapsed=1
if [[ -n "$prev_when" ]]; then
    # Python's fromisoformat handles both "...Z" and "+00:00" suffixes; the
    # outer try/except prints 0 when the timestamp is corrupted so the rest
    # of the script treats it as a fresh first-time alert (safer than
    # silently inheriting a stale "1 second ago" cooldown).
    prev_epoch="$(python3 -c "
import datetime, sys
try:
    print(int(datetime.datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00')).timestamp()))
except Exception:
    print(0)
" "$prev_when")"
    elapsed=$((now_epoch - prev_epoch))
    cooldown_elapsed=$((elapsed >= DRIFT_ALERT_COOLDOWN_SEC ? 1 : 0))
fi

# Silent re-entry when (a) we have prior history, (b) count is steady OR
# lower, and (c) we're still inside the cooldown. Otherwise we fall through
# to the alert path below.
if [[ "$prev_count" != "0" \
   && "$empty_count" -le "$prev_count" \
   && "$cooldown_elapsed" -eq 0 ]]; then
    echo "drift-check: empty=$empty_count steady (≤prior $prev_count) within ${DRIFT_ALERT_COOLDOWN_SEC}s cooldown → silent" >&2
    exit 1
fi

# --dry-run short-circuit: never alert, never update state.
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "drift-check: WOULD alert (empty=$empty_count > $DRIFT_EMPTY_THRESHOLD); exit 1, state unchanged" >&2
    exit 1
fi

# ── Send alert ────────────────────────────────────────────────────────────
alert_payload="$(jq -nc \
    --argjson empty     "$empty_count" \
    --argjson threshold "$DRIFT_EMPTY_THRESHOLD" \
    --argjson prev      "$prev_count" \
    --arg     when      "$(date -u +%FT%TZ)" \
    '{
        kind:"drift_alert",
        empty_count:$empty,
        threshold:$threshold,
        prev_alerted_count:$prev,
        checked_at:$when,
        hint:"run `python3 conductor.py --gc-terminals` to clean up"
    }')"

if [[ -z "$DRIFT_NOTIFY_URL" ]]; then
    echo "drift-check ALERT (no DRIFT_NOTIFY_URL — logged only): $alert_payload" >&2
else
    if ! curl -fsS -X POST -H 'Content-Type: application/json' \
        --data "$alert_payload" "$DRIFT_NOTIFY_URL" >/dev/null 2>&1; then
        echo "ERROR: alert POST failed for $DRIFT_NOTIFY_URL" >&2
        echo "payload was: $alert_payload" >&2
        exit 2
    fi
    echo "drift-check: alert sent to $DRIFT_NOTIFY_URL (empty=$empty_count)" >&2
fi

# ── Persist state for next invocation (atomic write + per-invocation tmp) ─
# ``jq ... > file`` truncates BEFORE jq writes; with ``set -e`` a crash
# mid-write leaves an empty or partial JSON file that the next cron pass
# silently treats as fresh-state (losing cooldown history). Writing to a
# sibling .tmp and ``mv`` keeps the rename atomic on POSIX/macOS so the
# existing state file is never observed in a torn state.
#
# mktemp (vs hardcoded ``${PATH}.tmp``) ensures two concurrent cron
# invocations don't overwrite each other's tmp file mid-rename. The EXIT
# trap reaps the tmp on every exit path — success, error, SIGTERM mid-
# write — so a throttled/crashed pass never leaves stale tmp files in
# $HOME/.school-core/.
state_tmp="$(mktemp "${DRIFT_STATE_PATH}.XXXXXX")"
trap 'rm -f "$state_tmp" 2>/dev/null || true' EXIT
if ! jq -n \
        --argjson empty "$empty_count" \
        --arg when "$(date -u +%FT%TZ)" \
        '{alerted_count:$empty, last_alerted_at:$when}' \
        > "$state_tmp"; then
    echo "ERROR: failed to write state tmp at $state_tmp" >&2
    exit 2  # trap reaps $state_tmp
fi
if ! mv "$state_tmp" "$DRIFT_STATE_PATH"; then
    echo "ERROR: failed to rename $state_tmp -> $DRIFT_STATE_PATH" >&2
    exit 2  # trap reaps $state_tmp
fi

exit 1
