# Manual Crew Spawn — See the Crew's Actual Last Words

When a crew dies silent and you can't tell *why* (config? wrapper? harness?
provider?), spawn it by hand with stdout captured. This bypasses FirstMate's
`spawn_silent` classification and shows exactly what Hermes did.

## Critical: which wrapper file?
`crew_dispatch.py` prefers the **repo** copy:
```python
repo_wrapper = Path(__file__).resolve().parent / "scripts" / "hermes-fm-wrapper"
default_wrapper = repo_wrapper if repo_wrapper.is_file() else Path.home() / ".local/bin/hermes-fm-wrapper"
wrapper = os.environ.get("FM_WRAPPER", str(default_wrapper))
```
→ Edit / test **`school-core/scripts/hermes-fm-wrapper`**, NOT `~/.local/bin/`.

## Instrumented spawn

```bash
cd /Users/brandonbennett/school-core
WORK=/tmp/instr_crew && rm -rf "$WORK" && mkdir -p "$WORK"
STATUS="$WORK/crew.status"; REPORT="$WORK/report.md"
BRIEF="$WORK/brief.md"
# real brief from a past run, or a tiny test:
cat > "$BRIEF" <<'EOF'
Append exactly the line "working: instrumented spawn OK" to /tmp/instr_crew/crew.status
using your file tool. Then append "done: branch=none commit=none base=none". Then stop.
EOF

# replicate the bridge's env (see crew_dispatch.py ~line 928)
export FM_AGENT_PROFILE=student-executor
export FM_AGENT_TASK_ROLE=executor
export FM_AGENT_TOOLSETS="file,memory,skills,terminal,todo"
export FM_AGENT_CAPABILITY_VERSION=1.0.0
export FM_STATUS_FILE="$STATUS"
export FM_REPORT_FILE="$REPORT"
export FM_HOME=/Users/brandonbennett/.hermes/school-core-fm-config
export OMNIROUTE_API_KEY="<key>"

WRAP=$(pwd)/scripts/hermes-fm-wrapper
"$WRAP" "$(cat "$BRIEF")" > "$WORK/out.txt" 2>&1
echo "exit=$?"
echo "--- status file ---"; cat "$STATUS" 2>&1
echo "--- crew stdout tail (last words) ---"; tail -40 "$WORK/out.txt" | tr -d '\r'
```

## What this proved (2026-08-24)
- A trivial brief → crew wrote `working:` + `done:` to `FM_STATUS_FILE`, exit 0.
  **The status mechanism, profile, key injection, and `-p` passthrough all work.**
- The **real** `#204` brief → crew executed (created branch, diagnosed missing
  bot branch), but its closing verb arrived as streamed text that OmniRoute
  shredded → wrapper appended `failed: hermes-exit-0-no-terminal-status` →
  FirstMate scored `silent_agent`. **Harness/pipeline not broken; provider
  streaming corruption is.** See `omniroute-streaming-debug.md`.
