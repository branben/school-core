#!/usr/bin/env python3
"""school_health_doc.py — generate interactive HTML health doc for School Core.

Reads real data and produces a self-contained HTML document with:
- Board state (NOW / NEXT / CUT lanes)
- Failure class analysis
- Score trends
- Clickable triggers (data-hermes-send) for common actions

Usage:
    python3 scripts/school_health_doc.py              # print to stdout
    python3 scripts/school_health_doc.py --open       # open in Hermes preview
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
SCORES_DB = DATA_DIR / "scores.db"
BOARD_FILE = DATA_DIR / "board.json"
LAST_RUN_FILE = DATA_DIR / "last_run.json"
CRON_JOBS_FILE = Path.home() / ".hermes" / "cron" / "jobs.json"


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_scores():
    """Load score trends from scores.db."""
    if not SCORES_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(SCORES_DB))
        cursor = conn.execute(
            "SELECT role, domain, score, updated_at FROM scores ORDER BY updated_at DESC LIMIT 50"
        )
        rows = cursor.fetchall()
        conn.close()
        scores = {}
        for role, domain, score, updated in rows:
            key = f"{role}/{domain}"
            if key not in scores:
                scores[key] = []
            scores[key].append({"score": score, "updated": updated})
        return scores
    except Exception:
        return {}


def load_last_runs():
    """Load recent runs from last_run.json."""
    data = load_json(LAST_RUN_FILE)
    if not isinstance(data, list):
        return []
    return data[-50:]  # last 50 runs


def classify_failures(runs):
    """Classify failures by type."""
    classes = {}
    for r in runs:
        status = r.get("status")
        if status in ("error", "school-failed", "blocked"):
            edge = r.get("failure_edge", "unknown")
            mode = r.get("failure_mode", "unknown")
            key = f"{edge}/{mode}"
            classes[key] = classes.get(key, 0) + 1
    return classes


def get_bridge_cron_status():
    """Check if school-core bridge cron is running."""
    jobs = load_json(CRON_JOBS_FILE)
    for job in jobs.get("jobs", []):
        if "school-core" in job.get("name", "").lower():
            return job.get("enabled", False), job.get("state", "unknown")
    return False, "not found"


def generate_html():
    """Generate the interactive HTML health doc."""
    board = load_json(BOARD_FILE)
    scores = load_scores()
    runs = load_last_runs()
    failures = classify_failures(runs)
    bridge_enabled, bridge_state = get_bridge_cron_status()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lanes = board.get("lanes", {})
    now_cards = lanes.get("now", [])
    next_cards = lanes.get("next", [])
    cut_cards = lanes.get("cut", [])

    # Score trends
    coder_scores = scores.get("coder/code-implementation", [])
    coder_trend = " → ".join([str(s["score"]) for s in coder_scores[:5][::-1]]) if coder_scores else "no data"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>School Core Health — {now}</title>
<style>
:root {{
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #c9d1d9;
  --muted: #8b949e;
  --accent: #58a6ff;
  --green: #3fb950;
  --yellow: #d29922;
  --red: #f85149;
}}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 24px;
  max-width: 800px;
}}
h1 {{ font-size: 20px; margin-bottom: 4px; }}
h2 {{ font-size: 16px; margin-top: 24px; margin-bottom: 8px; }}
.subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 20px; }}
.lane {{ margin-bottom: 16px; }}
.lane-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
.lane-count {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 2px 8px; font-size: 12px; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 8px; }}
.card-title {{ font-weight: 600; font-size: 14px; }}
.card-meta {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
.status-badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; margin-left: 8px; }}
.status-error {{ background: #3d1f1f; color: var(--red); }}
.status-retry {{ background: #3d2f1f; color: var(--yellow); }}
.status-blocked {{ background: #3d1f1f; color: var(--red); }}
.status-success {{ background: #1f3d1f; color: var(--green); }}
.trigger {{ display: inline-block; background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; margin-top: 8px; margin-right: 8px; }}
.trigger:hover {{ opacity: 0.9; }}
.trigger-secondary {{ background: var(--surface); border: 1px solid var(--border); color: var(--text); }}
.section {{ margin-bottom: 32px; }}
.metric {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border); }}
.metric-label {{ color: var(--muted); }}
.metric-value {{ font-weight: 600; }}
.alert {{ background: #3d1f1f; border: 1px solid var(--red); border-radius: 8px; padding: 12px; margin-bottom: 12px; }}
.alert-title {{ color: var(--red); font-weight: 600; margin-bottom: 4px; }}
.green {{ color: var(--green); }}
.yellow {{ color: var(--yellow); }}
.red {{ color: var(--red); }}
</style>
</head>
<body>

<h1>🏫 School Core Health</h1>
<div class="subtitle">Generated {now}</div>

<!-- SYSTEM STATUS -->
<div class="section">
<h2>System</h2>
<div class="metric">
  <span class="metric-label">Bridge cron</span>
  <span class="metric-value {'green' if bridge_enabled else 'red'}">{'RUNNING' if bridge_enabled else 'PAUSED'} ({bridge_state})</span>
</div>
<div class="metric">
  <span class="metric-label">Coder score trend</span>
  <span class="metric-value {'red' if len(coder_scores) > 1 and coder_scores[0]['score'] < coder_scores[-1]['score'] else 'green'}">{coder_trend}</span>
</div>
<div class="metric">
  <span class="metric-label">Total runs tracked</span>
  <span class="metric-value">{len(runs)}</span>
</div>
</div>

<!-- BOARD STATE -->
<div class="section">
<h2>📋 Board</h2>

<div class="lane">
<div class="lane-header"><strong>🔴 NOW</strong> <span class="lane-count">{len(now_cards)}</span></div>
{f''.join(f'<div class="card"><div class="card-title">{c.get("title", "?")}</div><div class="card-meta">[{c.get("status", "?")}] Score: {c.get("score", "n/a")}</div></div>' for c in now_cards[:3]) if now_cards else '<div class="card"><div class="card-meta">No active work</div></div>'}
</div>

<div class="lane">
<div class="lane-header"><strong>🟡 NEXT</strong> <span class="lane-count">{len(next_cards)}</span></div>
{f''.join(f'<div class="card"><div class="card-title">{c.get("title", "?")}</div><div class="card-meta">[{c.get("status", "?")}] {c.get("issue_type", "")}</div></div>' for c in next_cards[:5]) if next_cards else '<div class="card"><div class="card-meta">Queue empty</div></div>'}
</div>

<div class="lane">
<div class="lane-header"><strong>🟢 CUT</strong> <span class="lane-count">{len(cut_cards)}</span></div>
{f''.join(f'<div class="card"><div class="card-title">{c.get("title", "?")}</div><div class="card-meta">[{c.get("status", "?")}] ✓</div></div>' for c in cut_cards[:3]) if cut_cards else '<div class="card"><div class="card-meta">Nothing completed yet</div></div>'}
</div>
</div>

<!-- FAILURE ANALYSIS -->
<div class="section">
<h2>⚠️ Failure Classes (last 50 runs)</h2>
{f''.join(f'<div class="metric"><span class="metric-label">{k}</span><span class="metric-value red">{v}</span></div>' for k, v in sorted(failures.items(), key=lambda x: -x[1])) if failures else '<div class="card"><div class="card-meta">No failures detected</div></div>'}
</div>

<!-- TRIGGERS -->
<div class="section">
<h2>⚡ Triggers</h2>
<p style="color: var(--muted); font-size: 13px;">Click any action to execute it in Hermes.</p>

<button class="trigger" data-hermes-send="cd /Users/brandonbennett/school-core && python3 issue_bridge.py --once">Run bridge now</button>

<button class="trigger trigger-secondary" data-hermes-send="cd /Users/brandonbennett/school-core && python3 issue_bridge.py --once --dry-run">Preview next run</button>

<button class="trigger trigger-secondary" data-hermes-send="cd /Users/brandonbennett/school-core && python3 scripts/build_board_json.py && echo 'Board rebuilt'">Rebuild board</button>

<hr style="border: none; border-top: 1px solid var(--border); margin: 16px 0;">

<h3>Create bootstrap issue</h3>
<button class="trigger" data-hermes-send="gh issue create --repo branben/sound-royale-ny --title 'test: add hello world function' --label 'ready-for-agent' --label 'good first issue' --body 'Write a Python function `hello()` that returns \"hello world\". Add a test.'">Create easy issue</button>

<hr style="border: none; border-top: 1px solid var(--border); margin: 16px 0;">

<h3>Diagnostics</h3>
<button class="trigger trigger-secondary" data-hermes-send="cd /Users/brandonbennett/school-core && cat data/last_run.json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d[-3:], indent=2))'">Show last 3 runs</button>

<button class="trigger trigger-secondary" data-hermes-send="cd /Users/brandonbennett/school-core && sqlite3 data/scores.db 'SELECT role, domain, score FROM scores ORDER BY updated_at DESC LIMIT 10;'">Show scores</button>

</div>

<!-- DOCUMENTATION LINKS -->
<div class="section">
<h2>📖 Documentation</h2>
<div class="card">
<div class="card-title"><a href="docs/sdlc/gate-4-tdd.md" style="color: var(--accent);">Gate 4 — TDD</a></div>
<div class="card-meta">Compiler-before-critic contract</div>
</div>
<div class="card">
<div class="card-title"><a href="docs/sdlc/gate-5-precommit.md" style="color: var(--accent);">Gate 5 — Pre-commit</a></div>
<div class="card-meta">Code review + quality gates</div>
</div>
<div class="card">
<div class="card-title"><a href="docs/templates/TIER.md" style="color: var(--accent);">Tier Model</a></div>
<div class="card-meta">Documentation layer contract</div>
</div>
</div>

</body>
</html>"""

    return html


def main():
    if "--health" in sys.argv:
        html = generate_html()
        output = REPO / "docs" / "site" / "health.html"
    else:
        # Default: generate workspace
        html = generate_workspace_html()
        output = REPO / "docs" / "site" / "workspace.html"
    
    output.write_text(html)
    print(f"Written to {output}")
    
    if "--open" in sys.argv:
        try:
            subprocess.run(["open", str(output)], check=False)
        except Exception:
            pass

def generate_workspace_html():
    """Generate the three-column workspace with detail rail."""
    # Import the workspace generation logic
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    # For now, return a placeholder that points to the existing file
    # The full workspace is generated by the inline script above
    return Path(REPO / "docs" / "site" / "workspace.html").read_text()


if __name__ == "__main__":
    main()
