"""Weekly Agent School report — conversational HTML with trajectory + score data.

Usage:
    python docs/weekly_report.py

Generates: docs/weekly/<YYYY-WW>.html  (open in browser)

The report reads trajectory JSONs from data/trajectories/ and live scores
from data/scores.json, then writes a natural-language recap of the week's
improvements, regressions, gate crossings, and interesting failures.
"""

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAJECTORY_DIR = REPO_ROOT / "data" / "trajectories"
SCORES_PATH = REPO_ROOT / "data" / "scores.json"
OUTPUT_DIR = REPO_ROOT / "docs" / "weekly"

# Gates + their score thresholds — keep in sync with scoring.py
GATES = {"easy": 0, "medium": 25, "hard": 50, "diploma": 75}


def ensure_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_trajectories(days: int = 7) -> list:
    """Load all trajectories from the last `days` days."""
    if not TRAJECTORY_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = []
    for f in sorted(TRAJECTORY_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ts_str = data.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        results.append(data)
    return results


def load_scores() -> dict:
    """Load current scores.json."""
    if not SCORES_PATH.exists():
        return {}
    return json.loads(SCORES_PATH.read_text())


def gate_for_score(score: float) -> str:
    """Return the name of the highest gate a score qualifies for."""
    qualified = [name for name, thr in GATES.items() if score >= thr]
    return max(qualified, key=lambda n: GATES[n]) if qualified else "none"


def score_trend(trajs: list, agent: str, domain: str) -> list:
    """Extract chronological score changes for an agent/domain from trajectories."""
    points = []
    for t in trajs:
        if t.get("agent") == agent and t.get("domain") == domain:
            old = t.get("old_score")
            new = t.get("new_score")
            if old is not None and new is not None:
                points.append((t.get("timestamp", ""), old, new))
    points.sort(key=lambda x: x[0])
    return points


def build_report(trajs: list, scores: dict) -> str:
    """Build the conversational HTML report."""
    # --- Aggregate data ---
    total = len(trajs)
    success_count = sum(1 for t in trajs if not t.get("error"))
    fail_count = total - success_count
    success_rate = (success_count / total * 100) if total else 0

    # By domain
    domain_counts = Counter(t.get("domain", "unknown") for t in trajs)

    # By difficulty
    diff_counts = Counter(t.get("difficulty", "unknown") for t in trajs)

    # By agent (both total and successful)
    agent_total = Counter(t.get("agent", "unknown") for t in trajs)
    agent_success = Counter(
        t.get("agent", "unknown") for t in trajs if not t.get("error")
    )

    # Worst failures (with error messages)
    failures = [t for t in trajs if t.get("error")]
    failures.sort(key=lambda x: (x.get("old_score") or 0) - (x.get("new_score") or 0))

    # Score changes
    score_deltas = defaultdict(lambda: defaultdict(list))
    for t in trajs:
        agent = t.get("agent", "?")
        domain = t.get("domain", "?")
        old = t.get("old_score")
        new = t.get("new_score")
        if old is not None and new is not None:
            score_deltas[agent][domain].append(new - old)

    # Gate crossings
    gate_crossings = []
    for t in trajs:
        old_gate = gate_for_score(t.get("old_score") or 0)
        new_gate = gate_for_score(t.get("new_score") or 0)
        if old_gate != new_gate:
            gate_crossings.append({
                "agent": t.get("agent") or "?",
                "domain": t.get("domain") or "?",
                "from": old_gate,
                "to": new_gate,
                "new_score": t.get("new_score") or 0,
            })

    # Task_score distribution
    task_scores = [
        t.get("task_score") for t in trajs if t.get("task_score") is not None
    ]

    now = datetime.now()
    week_num = now.isocalendar()[1]
    year = now.year

    # --- Build HTML ---
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent School — Week {week_num}, {year}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    max-width: 780px; margin: 2em auto; padding: 0 1.5em;
    color: #1a1a2e; background: #faf9f6;
    line-height: 1.6; font-size: 16px;
  }}
  h1 {{ font-size: 2em; font-weight: 700; margin-bottom: 0.2em; color: #16213e; }}
  h2 {{ font-size: 1.4em; font-weight: 600; margin-top: 2em; color: #0f3460; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.3em; }}
  h3 {{ font-size: 1.15em; font-weight: 600; margin-top: 1.5em; color: #1a1a2e; }}
  .subtitle {{ color: #64748b; font-size: 0.95em; margin-bottom: 2em; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.8em; margin: 1em 0; }}
  .stat-card {{ background: white; border-radius: 10px; padding: 1em; box-shadow: 0 1px 3px rgba(0,0,0,0.06); text-align: center; }}
  .stat-card .number {{ font-size: 1.8em; font-weight: 700; color: #0f3460; display: block; }}
  .stat-card .label {{ font-size: 0.85em; color: #64748b; }}
  .stat-card.good .number {{ color: #059669; }}
  .stat-card.bad .number {{ color: #dc2626; }}
  .stat-card.warn .number {{ color: #d97706; }}
  .narrative {{ background: white; border-radius: 10px; padding: 1.2em 1.5em; margin: 1em 0; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .narrative p {{ margin: 0.6em 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1em 0; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  th {{ background: #1e293b; color: white; padding: 0.6em 0.8em; text-align: left; font-weight: 600; font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.03em; }}
  td {{ padding: 0.5em 0.8em; border-bottom: 1px solid #f1f5f9; font-size: 0.95em; }}
  tr:last-child td {{ border-bottom: none; }}
  .delta-up {{ color: #059669; font-weight: 600; }}
  .delta-down {{ color: #dc2626; font-weight: 600; }}
  .delta-flat {{ color: #64748b; }}
  .gate-badge {{ display: inline-block; padding: 0.15em 0.5em; border-radius: 4px; font-size: 0.8em; font-weight: 600; }}
  .gate-easy {{ background: #e0f2fe; color: #0369a1; }}
  .gate-medium {{ background: #fef3c7; color: #92400e; }}
  .gate-hard {{ background: #fce7f3; color: #9d174d; }}
  .gate-diploma {{ background: #d1fae5; color: #065f46; }}
  .failure-box {{ background: #fef2f2; border-left: 4px solid #dc2626; padding: 0.8em 1em; margin: 0.5em 0; border-radius: 0 8px 8px 0; font-size: 0.9em; }}
  .failure-box .agent {{ font-weight: 600; }}
  .up-arrow::before {{ content: "↑ "; color: #059669; }}
  .down-arrow::before {{ content: "↓ "; color: #dc2626; }}
  .footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid #e2e8f0; font-size: 0.85em; color: #94a3b8; }}
  .tag {{ display: inline-block; background: #e2e8f0; padding: 0.1em 0.4em; border-radius: 3px; font-size: 0.8em; color: #475569; }}
</style>
</head>
<body>

<h1>Week {week_num}, {year}</h1>
<p class="subtitle">{now.strftime("%B %d, %Y")} &middot; Agent School weekly pulse</p>
"""

    # --- Opening narrative ---
    if total == 0:
        html += """
<div class="narrative">
  <p>No task data this week. Either the system hasn't been running, or the trajectory directory is empty.</p>
  <p>If this is the first week, try running some tasks first:</p>
  <p><code>python cli.py run "write a pytest test for this function" --domain python-testing --difficulty easy</code></p>
</div>
"""
    else:
        # Score direction
        all_deltas = []
        for agent_data in score_deltas.values():
            for domain_deltas in agent_data.values():
                all_deltas.extend(domain_deltas)
        avg_delta = sum(all_deltas) / len(all_deltas) if all_deltas else 0

        # Determine tone
        if avg_delta > 3:
            direction = "climbing"
            tone = "good"
        elif avg_delta > 0.5:
            direction = "edging up"
            tone = "good"
        elif avg_delta > -0.5:
            direction = "flat"
            tone = "warn"
        elif avg_delta > -3:
            direction = "dipping"
            tone = "bad"
        else:
            direction = "dropping"
            tone = "bad"

        html += f"""
<div class="narrative">
  <p><strong>{total} tasks</strong> this week across {len(domain_counts)} domains.
  {success_rate:.0f}% success rate ({success_count} good, {fail_count} bad).
  Scores are {direction} <strong>({avg_delta:+.1f} avg)</strong>.</p>
</div>

<div class="stat-grid">
  <div class="stat-card">
    <span class="number">{total}</span>
    <span class="label">tasks</span>
  </div>
  <div class="stat-card {'good' if success_rate >= 70 else 'warn' if success_rate >= 50 else 'bad'}">
    <span class="number">{success_rate:.0f}%</span>
    <span class="label">success rate</span>
  </div>
  <div class="stat-card {'good' if avg_delta > 0 else 'bad'}">
    <span class="number">{avg_delta:+.1f}</span>
    <span class="label">avg score delta</span>
  </div>
  <div class="stat-card">
    <span class="number">{len(score_deltas)}</span>
    <span class="label">agents active</span>
  </div>
</div>
"""

    # --- Current Scoreboard (all agents) ---
    if scores:
        html += """
<h2>Current Scoreboard</h2>
<p class="subtitle">Every agent's current score and gate across all domains.</p>

<table>
<tr><th>Agent</th><th>Domain</th><th>Score</th><th>Gate</th><th>Source</th></tr>
"""
        rows = []
        for agent, domains in sorted(scores.items()):
            for domain, score in sorted(domains.items()):
                gate = gate_for_score(score)
                gate_class = f"gate-{gate}" if gate != "none" else ""
                # Source: detect model origin from agent name pattern
                if agent.startswith("foundry"):
                    source = "foundry"
                elif agent in ("openhands", "a2a-agent"):
                    source = "a2a"
                else:
                    source = "cloud"
                rows.append((score, agent, domain, gate, gate_class, source))
        rows.sort(key=lambda r: r[0], reverse=True)
        for score, agent, domain, gate, gate_class, source in rows:
            html += f"""<tr>
  <td>{agent}</td>
  <td><span class="tag">{domain}</span></td>
  <td>{score:.1f}</td>
  <td><span class="gate-badge {gate_class}">{gate}</span></td>
  <td><span class="tag">{source}</span></td>
</tr>"""
        html += "</table>"

    # --- Score changes table (agents with deltas this week) ---
    if score_deltas:
        html += """
<h2>Score Changes</h2>
<p class="subtitle">Who gained, who lost, and what that means.</p>

<table>
<tr><th>Agent</th><th>Domain</th><th>Delta</th><th>Gate</th><th>Trend</th></tr>
"""
        # Flatten and sort by delta
        rows = []
        for agent, domains in score_deltas.items():
            for domain, deltas in domains.items():
                avg = sum(deltas) / len(deltas)
                trend = "up" if avg > 2 else ("down" if avg < -2 else "flat")
                n = len(deltas)
                rows.append((avg, agent, domain, avg, trend, n))
        rows.sort(key=lambda r: r[0], reverse=True)

        for _, agent, domain, avg_d, trend, n in rows:
            delta_class = "delta-up" if avg_d > 0 else ("delta-down" if avg_d < 0 else "delta-flat")
            gate = gate_for_score(scores.get(agent, {}).get(domain, 0))
            gate_class = f"gate-{gate}" if gate != "none" else ""
            arrow = "up-arrow" if avg_d > 0 else ("down-arrow" if avg_d < 0 else "")
            html += f"""<tr>
  <td>{agent}</td>
  <td><span class="tag">{domain}</span></td>
  <td class="{delta_class}">{avg_d:+.1f}</td>
  <td><span class="gate-badge {gate_class}">{gate}</span></td>
  <td class="{arrow}">{n}x</td>
</tr>"""
        html += "</table>"

    # --- Gate crossings ---
    if gate_crossings:
        html += """
<h2>Gate Crossings</h2>
<p class="subtitle">Agents that leveled up or dropped down this week.</p>
"""
        for g in gate_crossings:
            icon = "🎓" if g["to"] == "diploma" else "⬆" if GATES.get(g["to"], 0) > GATES.get(g["from"], 0) else "⬇"
            html += f"""<div class="narrative">
  <p><strong>{icon} {g['agent']}</strong>
  <span class="tag">{g['domain']}</span>
  <span class="gate-badge gate-{g['from']}">{g['from']}</span>
  → <span class="gate-badge gate-{g['to']}">{g['to']}</span>
  (score: {g['new_score']:.1f})</p>
</div>"""

    # --- Task volume by domain ---
    if domain_counts:
        html += """
<h2>Task Volume</h2>
<p class="subtitle">What the system actually worked on.</p>

<table>
<tr><th>Domain</th><th>Tasks</th><th>% of Total</th></tr>
"""
        for domain, count in domain_counts.most_common():
            pct = count / total * 100
            bar_w = max(pct * 2, 2)
            html += f"""<tr>
  <td><span class="tag">{domain}</span></td>
  <td>{count}</td>
  <td>{pct:.0f}% <span style="display:inline-block;height:8px;width:{bar_w}px;background:#0f3460;border-radius:4px;vertical-align:middle;"></span></td>
</tr>"""
        html += "</table>"

    # --- Agent reliability ---
    if agent_total:
        html += """
<h2>Agent Reliability</h2>
<p class="subtitle">Who you can count on and who keeps dropping the ball.</p>

<table>
<tr><th>Agent</th><th>Attempts</th><th>Successes</th><th>Rate</th></tr>
"""
        agents_sorted = sorted(agent_total.keys(), key=lambda a: agent_total[a], reverse=True)
        for agent in agents_sorted:
            total_a = agent_total[agent]
            good_a = agent_success.get(agent, 0)
            rate = good_a / total_a * 100 if total_a else 0
            rate_class = "good" if rate >= 80 else ("warn" if rate >= 50 else "bad")
            rate_label = "great" if rate >= 90 else ("reliable" if rate >= 70 else ("shaky" if rate >= 50 else "unreliable"))
            html += f"""<tr>
  <td>{agent}</td>
  <td>{total_a}</td>
  <td>{good_a}</td>
  <td class="{rate_class}">{rate:.0f}% ({rate_label})</td>
</tr>"""
        html += "</table>"

    # --- Worst failures ---
    worst_failures = failures[:5]
    if worst_failures:
        html += """
<h2>Worst Failures</h2>
<p class="subtitle">The most instructive breakdowns of the week.</p>
"""
        for f in worst_failures:
            old = f.get("old_score") or 0
            new = f.get("new_score") or 0
            delta = new - old
            agent = f.get("agent") or "?"
            domain = f.get("domain") or "?"
            difficulty = f.get("difficulty") or "?"
            error_text = (f.get("error") or "no error")[:200]
            html += f"""<div class="failure-box">
  <p><span class="agent">{agent}</span>
  <span class="tag">{domain}</span>
  <span class="tag">{difficulty}</span>
  score {old:.0f} → {new:.0f} <span class="delta-down">{delta:+.0f}</span></p>
  <p style="font-family:monospace;font-size:0.85em;color:#991b1b;background:#fef2f2;padding:0.5em;border-radius:4px;">{error_text}</p>
</div>"""

    # --- Task score trend ---
    if len(task_scores) >= 5:
        avg_ts = sum(task_scores) / len(task_scores)
        first_half = task_scores[:len(task_scores)//2]
        second_half = task_scores[len(task_scores)//2:]
        first_avg = sum(first_half) / len(first_half) if first_half else 0
        second_avg = sum(second_half) / len(second_half) if second_half else 0
        quality_trend = "improving" if second_avg > first_avg + 5 else ("declining" if second_avg < first_avg - 5 else "stable")

        html += f"""
<h2>Quality Trend</h2>
<p class="subtitle">Are tasks getting better or worse over the week?</p>

<div class="narrative">
  <p>Average task score this week: <strong>{avg_ts:.1f}/100</strong>.</p>
  <p>First half: <strong>{first_avg:.1f}</strong> — Second half: <strong>{second_avg:.1f}</strong>.
  Quality is <strong>{quality_trend}</strong>.</p>
</div>
"""

    # --- Closing narrative ---
    if total > 0:
        best_agent = max(agent_success.keys(), key=lambda a: agent_success.get(a, 0) / max(agent_total.get(a, 1), 1) if agent_total.get(a, 0) > 2 else 0) if agent_success else "nobody"
        worst_agent = min(agent_success.keys(), key=lambda a: agent_success.get(a, 0) / max(agent_total.get(a, 1), 1)) if agent_total else "nobody"

        # Figure out best improver
        improvers = []
        for agent, domains in score_deltas.items():
            total_d = sum(sum(d) for d in domains.values())
            cnt = sum(len(d) for d in domains.values())
            improvers.append((total_d / cnt if cnt else 0, agent))
        improvers.sort(reverse=True)
        top_improver = improvers[0][1] if improvers else None

        html += f"""
<h2>Bottom Line</h2>
<div class="narrative">
"""
        if top_improver and improvers[0][0] > 2:
            html += f"  <p><strong>{top_improver}</strong> had the strongest week, gaining <strong>{improvers[0][0]:.1f} points</strong> on average. This model is trending in the right direction and should be prioritized for routing in its proven domains.</p>\n"

        if success_rate < 60:
            html += f"  <p>Success rate ({success_rate:.0f}%) is below the 70% target. The most reliable agent was <strong>{best_agent}</strong>. Consider routing more tasks there and giving the weaker models more easy practice.</p>\n"

        if avg_delta < -1:
            html += "  <p>Scores are declining overall. This could mean tasks are getting harder, the models are drifting, or the evaluation rubric has become stricter. Worth investigating before next week.</p>\n"

        if gate_crossings:
            crossed_agents = set(g["agent"] for g in gate_crossings)
            if len(crossed_agents) > 0:
                html += f"  <p>{len(crossed_agents)} agent(s) crossed gate thresholds this week. Each gate crossing represents real, measured improvement — not just a model swap but a genuine capability gain.</p>\n"

        html += """</div>"""

    # --- Footer ---
    html += f"""
<div class="footer">
  <p>Generated {now.strftime("%Y-%m-%d %H:%M")} &middot; Agent School weekly report</p>
  <p>Data: {TRAJECTORY_DIR} &middot; Scores: {SCORES_PATH}</p>
  <p><a href="file://{SCORES_PATH}">View live scores</a></p>
</div>

</body>
</html>
"""
    return html


def find_discoveries(days: int = 7) -> list:
    """Scan vault for discovery notes created in the last N days."""
    vault_base = Path.home() / "Documents" / "Knowledge Core"
    feedback_dir = vault_base / "01-Projects" / "Agent School" / "Feedback"
    discoveries = []
    if not feedback_dir.exists():
        return discoveries

    cutoff = datetime.now() - timedelta(days=days)

    for f in sorted(feedback_dir.glob("*.md"), reverse=True):
        if f.name in ("_index.md", "Discovery-Log-Template.md"):
            continue
        try:
            text = f.read_text()
        except OSError:
            continue

        # Try to parse frontmatter
        import re
        m = re.search(r"^---\s*$.*?^---\s*$", text, re.MULTILINE | re.DOTALL)
        if m:
            frontmatter = m.group(0)
            # Check created date
            date_m = re.search(r"created:\s*(\d{4}-\d{2}-\d{2})", frontmatter)
            if date_m:
                try:
                    created = datetime.strptime(date_m.group(1), "%Y-%m-%d")
                    if created < cutoff:
                        continue
                except ValueError:
                    pass
            # Extract severity and title
            title_m = re.search(r"title:\s*(.+)", frontmatter)
            severity_m = re.search(r"severity:\s*(\w+)", frontmatter)
            title = title_m.group(1).strip() if title_m else f.stem
            severity = severity_m.group(1) if severity_m else "insight"
            discoveries.append({
                "title": title,
                "severity": severity,
                "file": str(f),
                "link": f.name.replace(".md", ""),
            })
    return discoveries


def build_discovery_section(discoveries: list) -> str:
    """Build an HTML section for discoveries found this week."""
    if not discoveries:
        return ""
    sev_icons = {"insight": "💡", "caution": "⚠️", "bug": "🐛", "surprise": "🤔"}
    rows = ""
    for d in discoveries:
        icon = sev_icons.get(d["severity"], "📝")
        rows += f"<tr><td>{icon}</td><td>{d['title']}</td><td><span class=\"tag\">{d['severity']}</span></td></tr>\n"
    return f"""
<h2>Discoveries This Week</h2>
<p class="subtitle">Notable findings logged in the vault.</p>
<table>
<tr><th></th><th>Discovery</th><th>Severity</th></tr>
{rows}
</table>
"""


def inject_discoveries(html: str, disc_section: str) -> str:
    """Inject discoveries section before 'Bottom Line'."""
    if not disc_section:
        return html
    return html.replace(
        "<h2>Bottom Line</h2>",
        disc_section + "\n<h2>Bottom Line</h2>"
    )


def main():
    ensure_dir()
    trajs = load_trajectories(days=7)
    scores = load_scores()
    discoveries = find_discoveries(days=7)

    html = build_report(trajs, scores)
    disc_section = build_discovery_section(discoveries)
    html = inject_discoveries(html, disc_section)

    now = datetime.now()
    week_num = now.isocalendar()[1]
    year = now.year

    out_path = OUTPUT_DIR / f"{year}-W{week_num:02d}.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"✅ Report written: {out_path}")
    print(f"   {len(trajs)} trajectories, {len(scores)} agents, {len(discoveries)} discoveries")

    # Try to open in browser
    try:
        subprocess.run(["open", str(out_path)], timeout=5)
        print("   Opened in browser.")
    except Exception:
        pass


if __name__ == "__main__":
    main()
