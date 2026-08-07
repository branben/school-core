#!/usr/bin/env python3
"""
generate_activity_dashboard.py — Generate a standalone static activity dashboard.

Reads the activity log and scores, produces a self-contained HTML file
with all data baked in. Auto-refreshes by reloading the page.

Usage:
  python generate_activity_dashboard.py
  python generate_activity_dashboard.py --output docs/site/activity.html
  python generate_activity_dashboard.py --watch  # regenerate on changes
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SCORES_PATH = Path(__file__).parent / "data" / "scores.json"
ACTIVITY_LOG_PATH = Path(__file__).parent / "data" / "activity_log.json"
TRAJECTORY_DIR = Path(__file__).parent / "data" / "trajectories"
DEFAULT_OUTPUT = Path(__file__).parent / "docs" / "site" / "activity_dashboard.html"

AGENT_NAMES = {
    "foundry-coder-0.5b": "Ada",
    "foundry-coder-1.5b": "Turing",
    "foundry-coder-7b": "Grace",
    "foundry-smollm3-3b": "Min",
    "foundry-phi4": "Phi",
    "foundry-olmo-3-7b": "Olmo",
    "foundry-qwen2.5-coder-7b-empirical": "Empiric",
    "foundry-qwen3.5-2b-text": "Quip",
    "gemini-3-flash-preview": "Gemini",
    "gemma-4-31b-it:free": "Gemma",
    "owl-alpha": "Owl",
    "agy/gemini-3.5-flash-high": "Flash3.5",
    "kimi-k2.6:free": "Kimi",
    "always-on-max": "Max",
    "always-on-free": "Free",
    "north-coding": "North",
    "openhands": "Open",
    "qwen-coder-compiled": "Compiled",
    "qwen2.5-coder:7b": "Qwen7b",
    "qwen2.5:7b": "Qwen",
    "phi4": "Phi4",
    "smollm2:1.7b": "Smol",
}

GATES = {"easy": 0, "medium": 25, "hard": 50, "diploma": 75}
GATE_COLORS = {"easy": "#22c55e", "medium": "#f59e0b", "hard": "#5b8def", "diploma": "#a855f7"}
ROLE_ICONS = {"Faculty": "🎓", "Teacher": "📚", "Senior Student": "📖", "Student": "🌱", "Unenrolled": "⏳"}


def load_data():
    scores = {}
    if SCORES_PATH.exists():
        scores = json.loads(SCORES_PATH.read_text())

    activity = {"entries": []}
    if ACTIVITY_LOG_PATH.exists():
        activity = json.loads(ACTIVITY_LOG_PATH.read_text())

    # Build agent list
    agents = []
    for agent_id, domains in scores.items():
        if agent_id.startswith("ses_"):
            continue
        max_score = max(domains.values()) if domains else 0
        default_score = domains.get("_default", 0)
        gate = "easy"
        for gname, gthr in sorted(GATES.items(), key=lambda x: x[1]):
            if max_score >= gthr:
                gate = gname
        role = "Student"
        if max_score >= 75:
            role = "Faculty"
        elif max_score >= 50:
            role = "Teacher"
        elif max_score >= 25:
            role = "Senior Student"

        # Find latest activity
        latest = None
        for e in reversed(activity.get("entries", [])):
            if e.get("agent") == agent_id:
                latest = e
                break

        agents.append({
            "id": agent_id,
            "name": AGENT_NAMES.get(agent_id, agent_id),
            "score": default_score,
            "max_score": max_score,
            "gate": gate,
            "role": role,
            "domains": {k: v for k, v in domains.items() if k != "_default"},
            "latest_activity": latest,
        })

    agents.sort(key=lambda a: a["score"], reverse=True)
    return agents, activity.get("entries", [])


def generate_html(agents, entries, refresh_seconds=5):
    """Generate standalone HTML with embedded data and auto-refresh."""
    data = json.dumps({"agents": agents, "entries": entries}, ensure_ascii=False)

    # Build agent chips HTML
    role_groups = {}
    for a in agents:
        role = a["role"]
        if role not in role_groups:
            role_groups[role] = []
        role_groups[role].append(a)

    zones_html = ""
    for role in ["Faculty", "Teacher", "Senior Student", "Student", "Unenrolled"]:
        members = role_groups.get(role, [])
        if not members:
            continue
        icon = ROLE_ICONS.get(role, "👤")
        chips = ""
        for a in members:
            name = a["name"]
            initial = name[0]
            color = GATE_COLORS.get(a["gate"], "#8a8694")
            act = a.get("latest_activity") or {}
            status = act.get("status", "idle")
            desc = act.get("description", "Idle — waiting for tasks")
            ts = act.get("timestamp", "")
            time_str = ""
            if ts:
                from datetime import datetime
                try:
                    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    secs = int((datetime.now(d.tzinfo) - d).total_seconds())
                    if secs < 5: time_str = "just now"
                    elif secs < 60: time_str = f"{secs}s ago"
                    elif secs < 3600: time_str = f"{secs//60}m ago"
                    else: time_str = f"{secs//3600}h ago"
                except: pass

            action_class = ""
            if status == "in_progress": action_class = "working"
            elif status == "error": action_class = "error"
            elif status == "completed": action_class = "success"
            elif status == "milestone": action_class = "milestone"

            chips += f"""
        <div class="agent-chip role-{role.lower().replace(' ','-')} status-{status}">
          <div class="chip-avatar" style="background:{color}22;color:{color}">{initial}</div>
          <div class="chip-info">
            <div class="chip-name">{name}</div>
            <div class="chip-action {action_class}">{desc}</div>
          </div>
          <div class="chip-meta">
            <div class="chip-score" style="color:{color}">{a['score']:.1f}</div>
            <div class="chip-time">{time_str or '—'}</div>
          </div>
        </div>"""

        zones_html += f"""
      <div class="zone">
        <div class="zone-header">
          <span class="zone-title"><span class="zone-icon">{icon}</span> {role}s</span>
          <span class="zone-count">{len(members)}</span>
        </div>
        <div class="zone-agents">{chips}</div>
      </div>"""

    if not zones_html:
        zones_html = """
      <div class="empty-state" style="grid-column:1/-1;">
        <div class="icon">🏫</div>
        <h2>The schoolyard is empty</h2>
        <p>Run some tasks to see agents at work.</p>
      </div>"""

    # Build feed HTML
    feed_html = ""
    icons_map = {
        "task_start": "▶️", "task_finish": "✅", "task_error": "❌",
        "gate_cross": "🎓", "agent_sleep": "😴", "agent_wake": "☀️",
        "agent_idle": "💤", "staff_run": "🔧", "self_directed": "🧠",
    }
    for e in reversed(entries[-40:]):
        icon = icons_map.get(e.get("type", ""), "📌")
        desc = e.get("description", e.get("type", "unknown"))
        ts = e.get("timestamp", "")
        time_str = ""
        if ts:
            try:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                time_str = d.strftime("%H:%M:%S")
            except: pass
        feed_html += f"""
        <div class="feed-entry">
          <span class="feed-icon">{icon}</span>
          <span class="feed-text">{desc}</span>
          <span class="feed-time">{time_str}</span>
        </div>"""

    if not feed_html:
        feed_html = '<div style="padding:20px;color:var(--text-dim);font-size:12px;text-align:center;">No activity yet</div>'

    total = len(agents)
    working = sum(1 for a in agents if (a.get("latest_activity") or {}).get("status") == "in_progress")
    errors_5m = sum(1 for e in entries if e.get("type") == "task_error" and e.get("timestamp", "") > "")
    milestones = sum(1 for e in entries if e.get("type") == "gate_cross")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>Agent School — Live Activity</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700;900&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #0a0a0f; --surface: #111118; --surface-2: #1a1a24;
  --border: #2a2a38; --text: #e8e6e3; --text-dim: #8a8694;
  --accent: #c8ff00; --blue: #5b8def; --purple: #a855f7;
  --orange: #f59e0b; --red: #ef4444; --green: #22c55e;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Geist Mono','SF Mono','Fira Code',monospace; background:var(--bg); color:var(--text); line-height:1.6; -webkit-font-smoothing:antialiased; }}
.container {{ max-width:1400px; margin:0 auto; padding:0 32px; }}
nav {{ padding:14px 0; border-bottom:1px solid var(--border); background:rgba(10,10,15,0.92); backdrop-filter:blur(12px); }}
nav .container {{ display:flex; justify-content:space-between; align-items:center; }}
nav .logo {{ font-family:'Geist',sans-serif; font-size:18px; font-weight:700; color:var(--accent); letter-spacing:-0.5px; text-decoration:none; }}
nav .logo span {{ color:var(--text-dim); font-family:'Geist Mono',monospace; font-weight:400; font-size:9px; display:block; letter-spacing:2.5px; text-transform:uppercase; }}
nav .links a {{ margin-left:20px; font-size:11px; color:var(--text-dim); text-decoration:none; }}
nav .links a:hover {{ color:var(--accent); }}
nav .links a.active {{ color:var(--accent); }}
main {{ padding-top:24px; padding-bottom:60px; }}
.hero-stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(100px,1fr)); gap:10px; margin:16px 0 28px; }}
.hstat {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px; text-align:center; }}
.hstat .n {{ font-family:'Geist',sans-serif; font-size:22px; font-weight:700; display:block; }}
.hstat .l {{ font-size:9px; color:var(--text-dim); text-transform:uppercase; letter-spacing:1px; }}
.hstat.green .n {{ color:var(--green); }} .hstat.blue .n {{ color:var(--blue); }}
.hstat.purple .n {{ color:var(--purple); }} .hstat.orange .n {{ color:var(--orange); }}
.schoolyard {{ display:grid; grid-template-columns:1fr 320px; gap:20px; align-items:start; }}
@media(max-width:900px) {{ .schoolyard {{ grid-template-columns:1fr; }} }}
.zones {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:14px; }}
.zone {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px; transition:border-color 0.3s; }}
.zone:hover {{ border-color:#3a3a4e; }}
.zone-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }}
.zone-title {{ font-family:'Geist',sans-serif; font-size:13px; font-weight:600; display:flex; align-items:center; gap:6px; }}
.zone-icon {{ font-size:16px; }}
.zone-count {{ font-size:10px; color:var(--text-dim); background:var(--surface-2); padding:2px 7px; border-radius:10px; }}
.zone-agents {{ display:flex; flex-direction:column; gap:5px; }}
.agent-chip {{ display:flex; align-items:center; gap:8px; padding:7px 9px; background:var(--surface-2); border-radius:6px; border-left:3px solid transparent; }}
.agent-chip.role-faculty {{ border-left-color:var(--purple); }}
.agent-chip.role-teacher {{ border-left-color:var(--blue); }}
.agent-chip.role-senior-student {{ border-left-color:var(--orange); }}
.agent-chip.role-student {{ border-left-color:var(--green); }}
.agent-chip.role-unenrolled {{ border-left-color:var(--text-dim); opacity:0.6; }}
.agent-chip.status-working {{ background:rgba(91,141,239,0.08); }}
.agent-chip.status-error {{ background:rgba(239,68,68,0.08); }}
.chip-avatar {{ width:28px; height:28px; border-radius:6px; display:flex; align-items:center; justify-content:center; font-family:'Geist',sans-serif; font-weight:700; font-size:12px; flex-shrink:0; }}
.chip-info {{ flex:1; min-width:0; }}
.chip-name {{ font-weight:600; font-size:11px; }}
.chip-action {{ font-size:10px; color:var(--text-dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:160px; }}
.chip-action.working {{ color:var(--blue); }}
.chip-action.error {{ color:var(--red); }}
.chip-action.success {{ color:var(--green); }}
.chip-action.milestone {{ color:var(--accent); font-weight:600; }}
.chip-meta {{ text-align:right; flex-shrink:0; }}
.chip-score {{ font-size:10px; font-weight:600; }}
.chip-time {{ font-size:9px; color:var(--text-dim); }}
.activity-feed {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px; position:sticky; top:20px; max-height:calc(100vh - 40px); overflow-y:auto; }}
.feed-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }}
.feed-title {{ font-family:'Geist',sans-serif; font-size:13px; font-weight:600; }}
.feed-count {{ font-size:10px; color:var(--text-dim); }}
.feed-entry {{ display:flex; gap:7px; padding:5px 0; border-bottom:1px solid rgba(42,42,56,0.3); font-size:10px; }}
.feed-entry:last-child {{ border-bottom:none; }}
.feed-icon {{ flex-shrink:0; font-size:12px; width:18px; text-align:center; }}
.feed-text {{ flex:1; color:var(--text-dim); line-height:1.5; }}
.feed-time {{ flex-shrink:0; font-size:9px; color:var(--text-dim); opacity:0.6; }}
.empty-state {{ text-align:center; padding:40px 20px; color:var(--text-dim); }}
.empty-state .icon {{ font-size:40px; margin-bottom:12px; }}
.empty-state h2 {{ font-family:'Geist',sans-serif; font-size:16px; color:var(--text); margin-bottom:6px; }}
.empty-state p {{ font-size:12px; }}
.refresh-note {{ font-size:10px; color:var(--text-dim); text-align:right; margin-bottom:12px; }}
::-webkit-scrollbar {{ width:5px; }} ::-webkit-scrollbar-track {{ background:transparent; }} ::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:3px; }}
</style>
</head>
<body>
<nav>
  <div class="container">
    <a href="index.html" class="logo">School<span>Agent Framework</span></a>
    <div class="links">
      <a href="index.html">Overview</a>
      <a href="leaderboard.html">Leaderboard</a>
      <a href="activity_dashboard.html" class="active">Live Activity</a>
    </div>
  </div>
</nav>
<main><div class="container">
  <div class="hero-stats">
    <div class="hstat green"><span class="n">{total}</span><span class="l">Agents</span></div>
    <div class="hstat blue"><span class="n">{working}</span><span class="l">Working</span></div>
    <div class="hstat orange"><span class="n">{errors_5m}</span><span class="l">Errors</span></div>
    <div class="hstat purple"><span class="n">{milestones}</span><span class="l">Crossings</span></div>
  </div>
  <div class="refresh-note">Auto-refreshes every {refresh_seconds}s · Generated {time.strftime("%Y-%m-%d %H:%M:%S")}</div>
  <div class="schoolyard">
    <div class="zones">{zones_html}</div>
    <div class="activity-feed">
      <div class="feed-header">
        <span class="feed-title">📋 Activity Feed</span>
        <span class="feed-count">{len(entries)} events</span>
      </div>
      {feed_html}
    </div>
  </div>
</div></main>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate activity dashboard")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output HTML path")
    parser.add_argument("--watch", action="store_true", help="Watch for changes and regenerate")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds (default: 5)")
    args = parser.parse_args()

    agents, entries = load_data()
    html = generate_html(agents, entries, refresh_seconds=args.interval)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"✅ Dashboard written: {args.output}")
    print(f"   {len(agents)} agents · {len(entries)} activity entries")
    print(f"   Auto-refresh: every {args.interval}s")

    if args.watch:
        print("   Watching for changes… (Ctrl+C to stop)")
        last_mtime = 0
        try:
            while True:
                time.sleep(1)
                mtimes = []
                for p in [ACTIVITY_LOG_PATH, SCORES_PATH]:
                    if p.exists():
                        mtimes.append(p.stat().st_mtime)
                if TRAJECTORY_DIR.exists():
                    for f in TRAJECTORY_DIR.glob("*.json"):
                        mtimes.append(f.stat().st_mtime)
                current = max(mtimes) if mtimes else 0
                if current > last_mtime:
                    last_mtime = current
                    agents, entries = load_data()
                    html = generate_html(agents, entries, refresh_seconds=args.interval)
                    args.output.write_text(html, encoding="utf-8")
                    print(f"   🔄 Regenerated: {len(entries)} entries · {time.strftime('%H:%M:%S')}")
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
