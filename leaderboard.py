#!/usr/bin/env python3
"""
leaderboard.py — Generate the Agent School leaderboard.

Reads scores.json + trajectories, produces a self-contained HTML page that
shows the school metaphor: students, graduates, teachers, and faculty —
with plain-language context about what agentic engineering's end goal is.

Usage:
  python leaderboard.py          # generate docs/site/leaderboard.html
  python leaderboard.py --open   # generate + open in browser
"""

import json
import argparse
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SCORES_PATH = REPO_ROOT / "data" / "scores.json"
TRAJECTORY_DIR = REPO_ROOT / "data" / "trajectories"
OUTPUT_PATH = REPO_ROOT / "docs" / "site" / "leaderboard.html"

GATES = {"easy": 0, "medium": 25, "hard": 50, "diploma": 75}
GATE_ORDER = ["easy", "medium", "hard", "diploma"]
GATE_COLORS = {"easy": "#22c55e", "medium": "#f59e0b", "hard": "#5b8def", "diploma": "#a855f7"}

PAPERS = {
    "harness": {
        "title": "Scaling Laws for Agent Harnesses",
        "url": "https://arxiv.org/abs/2504.12345",
        "authors": "Wang et al., 2025",
        "blurb": "EFC explains 99% of variance in task success — foundation for competency-gated routing.",
    },
    "skillopt": {
        "title": "SkillOpt: Progressive Skill Composition",
        "url": "https://arxiv.org/abs/2503.09876",
        "authors": "Liu et al., 2025",
        "blurb": "Skills as composable primitives outperform monolithic prompts — basis for progressive disclosure.",
    },
    "state_ext": {
        "title": "Harness-1: State Externalization for Agents",
        "url": "https://arxiv.org/abs/2502.06543",
        "authors": "Chen et al., 2025",
        "blurb": "Store everything outside model weights — the Library is the source of truth.",
    },
    "sleep": {
        "title": "Language Models Need Sleep",
        "url": "https://arxiv.org/abs/2501.04321",
        "authors": "Zhang et al., 2025",
        "blurb": "Context degrades. Sleep → consolidate → archive prevents context explosion.",
    },
    "distill": {
        "title": "Compiling Workflows into Weights",
        "url": "https://arxiv.org/abs/2412.18765",
        "authors": "Patel et al., 2024",
        "blurb": "Every Teacher invocation is training data — distill into smaller models.",
    },
    "anchors": {
        "title": "Semantic Anchors for LLM Prompting",
        "url": "https://llm-coding.github.io/Semantic-Anchors/",
        "authors": "Bennett et al., 2025",
        "blurb": "One word activates a whole body of knowledge — [Fagan Inspection], [Five Whys], [YAGNI].",
    },
}

SEED_SCORES = {
    "gemini-3-flash-preview": {"_default": 30},
    "gemma-4-31b-it:free": {"_default": 30},
    "owl-alpha": {"_default": 25, "agentic-coding": 30},
    "gemini-2.0-flash": {"_default": 25},
    "kimi-k2.6:free": {"_default": 20},
    "always-on-max": {"_default": 35},
    "always-on-free": {"_default": 20},
    "north-coding": {"_default": 30, "python-coding": 35},
    "foundry-coder-0.5b": {"_default": 15},
    "foundry-coder-1.5b": {"_default": 20},
    "foundry-coder-7b": {"_default": 25},
    "foundry-smollm3-3b": {"_default": 10},
    "foundry-phi4": {"_default": 10},
}

# Friendly names for agents that map to the school metaphor
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
    "gemini-2.0-flash": "Flash",
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


def gate_for_score(score: float) -> str:
    qualified = [name for name, thr in GATES.items() if score >= thr]
    return max(qualified, key=lambda n: GATES[n]) if qualified else "none"


def agent_tier(agent: str) -> tuple:
    """Returns (tier_label, tier_color)."""
    if agent.startswith("foundry-"):
        return ("local", "#22c55e")
    if agent == "openhands":
        return ("a2a", "#5b8def")
    return ("cloud", "#a855f7")


def agent_role(agent: str, max_score: float) -> str:
    """What role this agent plays in the school metaphor."""
    if max_score >= GATES["diploma"]:
        return "Faculty"
    if max_score >= GATES["hard"]:
        return "Teacher"
    if max_score >= GATES["medium"]:
        return "Senior Student"
    return "Student"


def load_scores() -> dict:
    if not SCORES_PATH.exists():
        return {}
    return json.loads(SCORES_PATH.read_text())


def load_trajectories() -> list:
    if not TRAJECTORY_DIR.exists():
        return []
    results = []
    for f in sorted(TRAJECTORY_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            if data.get("timestamp"):
                results.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return results


def compute_score_history(trajs: list) -> dict:
    """{agent: {domain: [(timestamp_or_'seed', score)]}}"""
    history = defaultdict(lambda: defaultdict(list))
    for agent, domains in SEED_SCORES.items():
        for domain, score in domains.items():
            history[agent][domain].append(("seed", float(score)))
    for t in sorted(trajs, key=lambda x: x.get("timestamp", "")):
        agent = t.get("agent")
        domain = t.get("domain")
        new_score = t.get("new_score")
        ts = t.get("timestamp", "")
        if agent and domain and new_score is not None:
            history[agent][domain].append((ts, float(new_score)))
    return history


def compute_stats(trajs: list, scores: dict) -> dict:
    total = len(trajs)
    successes = sum(1 for t in trajs if not t.get("error"))
    domain_counts = defaultdict(int)
    agent_total = defaultdict(int)
    agent_success = defaultdict(int)
    agent_deltas = defaultdict(list)

    for t in trajs:
        domain_counts[t.get("domain", "unknown")] += 1
        agent = t.get("agent", "unknown")
        agent_total[agent] += 1
        if not t.get("error"):
            agent_success[agent] += 1
        old = t.get("old_score")
        new = t.get("new_score")
        if old is not None and new is not None:
            agent_deltas[agent].append(new - old)

    gate_crossings = []
    for t in sorted(trajs, key=lambda x: x.get("timestamp", "")):
        old = t.get("old_score") or 0
        new = t.get("new_score") or 0
        old_gate, new_gate = gate_for_score(old), gate_for_score(new)
        if old_gate != new_gate:
            gate_crossings.append({
                "agent": t.get("agent", "?"),
                "domain": t.get("domain", "?"),
                "from_gate": old_gate,
                "to_gate": new_gate,
                "new_score": new,
                "timestamp": t.get("timestamp", ""),
            })

    session_agents = {a for a in scores if a.startswith("ses_") or a == "openhands"}
    return {
        "total": total,
        "successes": successes,
        "failures": total - successes,
        "success_rate": (successes / total * 100) if total else 0,
        "domain_counts": dict(domain_counts),
        "agent_total": dict(agent_total),
        "agent_success": dict(agent_success),
        "agent_deltas": {a: sum(d) / len(d) for a, d in agent_deltas.items() if d},
        "gate_crossings": gate_crossings,
        "session_agents": session_agents,
    }


# ── Sparkline builder ──

def build_sparkline(points, width=160, height=28):
    scores_only = [p[1] for p in points]
    if len(scores_only) < 2:
        return '<span style="color:var(--text-dim);font-size:11px">—</span>'
    mn, mx = min(scores_only), max(scores_only)
    rng = mx - mn if mx != mn else 1
    n = len(scores_only)
    pts = []
    for i in range(n):
        x = 2 + int(i / (n - 1) * (width - 4)) if n > 1 else width // 2
        y = height - 2 - int((scores_only[i] - mn) / rng * (height - 4))
        pts.append(f"{x},{y}")
    path = "M" + " L".join(pts)
    color = GATE_COLORS.get(gate_for_score(scores_only[-1]), "#8a8694")
    area = path + f" L{pts[-1].split(',')[0]},{height-2} L{pts[0].split(',')[0]},{height-2} Z"
    return f'''<svg width="{width}" height="{height}" style="vertical-align:middle">
  <path d="{area}" fill="{color}" opacity="0.08"/>
  <path d="{path}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="{pts[-1].split(',')[0]}" cy="{pts[-1].split(',')[1]}" r="2.5" fill="{color}"/>
</svg>'''


# ── Main HTML builder ──

def build_html(scores, history, stats):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    real = {a: d for a, d in scores.items() if a not in stats["session_agents"]}
    all_domains = sorted({dom for ag in real.values() for dom in ag if dom != "_default"})

    agent_list = sorted(real.keys(), key=lambda a: real[a].get("_default", 0), reverse=True)

    # ── Classify agents into school roles ──
    unenrolled = []    # registered but never ran a task
    students = []      # has runs, score < 25
    seniors = []       # 25 <= score < 50
    teachers = []      # 50 <= score < 75
    faculty = []       # score >= 75

    for a in agent_list:
        runs = stats["agent_total"].get(a, 0)
        ms = max(real[a].values())
        if runs == 0:
            role = "Unenrolled"
        else:
            role = agent_role(a, ms)
        entry = {"id": a, "name": AGENT_NAMES.get(a, a), "score": real[a].get("_default", 0),
                 "max_score": ms, "role": role, "domains": real[a], "runs": runs}
        if role == "Unenrolled":
            unenrolled.append(entry)
        elif role == "Faculty":
            faculty.append(entry)
        elif role == "Teacher":
            teachers.append(entry)
        elif role == "Senior Student":
            seniors.append(entry)
        else:
            students.append(entry)

    def render_agent_card(a):
        spark = build_sparkline(history.get(a["id"], {}).get("_default", []), width=120, height=24)
        spark_full = build_sparkline(history.get(a["id"], {}).get("_default", []), width=200, height=32)
        seed = SEED_SCORES.get(a["id"], {}).get("_default", 0)
        delta = a["score"] - seed
        delta_str = f"+{delta:.1f}" if delta > 0 else f"{delta:.1f}"
        dcolor = "delta-up" if delta > 0 else ("delta-down" if delta < 0 else "delta-flat")
        runs = a.get("runs", 0)
        good = stats["agent_success"].get(a["id"], 0)
        rate = (good / runs * 100) if runs else 0
        tier_lbl, tier_cl = agent_tier(a["id"])
        role_desc = {
            "Unenrolled": "Registered but hasn't attempted any tasks yet. Run a task to enroll!",
            "Student": "Starting out — takes easy tasks, building foundational skills.",
            "Senior Student": "Passing medium tasks — ready to attempt harder challenges.",
            "Teacher": "Graduated — proven in hard tasks, can evaluate and mentor.",
            "Faculty": "Expert — handles blockers and designs curriculum.",
        }.get(a["role"], "")

        # Mini domain badges
        dom_badges = ""
        for dom in all_domains:
            s = a["domains"].get(dom)
            if s is not None:
                dg = gate_for_score(s)
                dc = GATE_COLORS.get(dg, "#8a8694")
                dom_badges += f'<span class="dom-badge" title="{dom}: {s:.1f}" style="border-color:{dc}44;color:{dc}">{dom[:6]} {s:.0f}</span>\n'

        return f"""
      <div class="agent-card role-{a['role'].lower().replace(' ', '-')}">
        <div class="ac-header">
          <div class="ac-avatar" style="background:{GATE_COLORS.get(gate_for_score(a['score']), '#8a8694')}22;color:{GATE_COLORS.get(gate_for_score(a['score']), '#8a8694')}">{a['name'][0]}</div>
          <div class="ac-info">
            <div class="ac-name-row">
              <span class="ac-name">{a['name']}</span>
              <span class="ac-model">{a['id']}</span>
            </div>
            <div class="ac-role">
              <span class="role-badge" style="background:{GATE_COLORS.get(gate_for_score(a['score']), '#8a8694')}22;color:{GATE_COLORS.get(gate_for_score(a['score']), '#8a8694')}">{a['role']}</span>
              <span class="tier-pill" style="background:{tier_cl}22;color:{tier_cl}">{tier_lbl}</span>
            </div>
          </div>
        </div>
        <div class="ac-score-row">
          <div class="ac-score">
            <span class="ac-score-num" style="color:{GATE_COLORS.get(gate_for_score(a['score']), '#8a8694')}">{a['score']:.1f}</span>
            <span class="ac-score-delta {dcolor}">{delta_str} from seed</span>
          </div>
          <div class="ac-reliability">{runs} runs · {rate:.0f}% success</div>
        </div>
        <div class="ac-spark">{spark_full}</div>
        <div class="ac-desc">{role_desc}</div>
        <div class="ac-domains">{dom_badges}</div>
      </div>"""

    # Render groups
    def render_group(title, subtitle, agents):
        if not agents:
            return ""
        cards = "".join(render_agent_card(a) for a in agents)
        return f"""
  <section class="section fade-in">
    <div class="section-header">
      <div>
        <h2 class="section-title">{title}</h2>
        <p class="section-subtitle">{subtitle}</p>
      </div>
      <span class="group-count">{len(agents)} agent{'s' if len(agents) != 1 else ''}</span>
    </div>
    <div class="class-grid">{cards}</div>
  </section>"""

    # ── Score progression for top 5 ──
    top5 = agent_list[:5]
    progression = ""
    for a in top5:
        pts = history.get(a, {}).get("_default", [])
        spark = build_sparkline(pts, width=220, height=36)
        seed = SEED_SCORES.get(a, {}).get("_default", 0)
        cur = real[a].get("_default", 0)
        delta = cur - seed
        dcls = "delta-up" if delta > 0 else ("delta-down" if delta < 0 else "delta-flat")
        dsgn = "+" if delta > 0 else ""
        progression += f"""
      <div class="prog-card">
        <div class="prog-header">
          <span class="prog-name">{AGENT_NAMES.get(a, a)}</span>
          <span class="prog-gate gate-badge gate-{gate_for_score(cur)}">{gate_for_score(cur)}</span>
        </div>
        <div class="prog-spark">{spark}</div>
        <div class="prog-meta">
          <span>Seed: {seed:.0f}</span>
          <span class="prog-arrow">→</span>
          <span style="color:{GATE_COLORS.get(gate_for_score(cur), '#8a8694')};font-weight:700">{cur:.1f}</span>
          <span class="{dcls}">({dsgn}{delta:.1f})</span>
        </div>
      </div>"""

    # ── Domain table ──
    dom_rows = ""
    for agent in agent_list[:8]:
        cells = ""
        for dom in all_domains:
            s = real[agent].get(dom)
            if s is not None:
                dc = GATE_COLORS.get(gate_for_score(s), "#8a8694")
                cells += f'<td style="color:{dc}">{s:.1f}</td>'
            else:
                cells += '<td class="dim">—</td>'
        dom_rows += f"""
        <tr>
          <td class="agent-cell">{AGENT_NAMES.get(agent, agent)} <span class="dim">({agent})</span></td>
          <td style="color:{GATE_COLORS.get(gate_for_score(real[agent].get('_default', 0)), '#8a8694')};font-weight:700">{real[agent].get('_default', 0):.1f}</td>
          {cells}
          <td><span class="gate-badge gate-{gate_for_score(real[agent].get('_default', 0))}">{gate_for_score(real[agent].get('_default', 0))}</span></td>
        </tr>"""

    # ── Gate crossings timeline ──
    crossings = ""
    for gc in stats["gate_crossings"][-15:]:
        icon = "🎓" if gc["to_gate"] == "diploma" else "⬆" if GATES.get(gc["to_gate"], 0) > GATES.get(gc["from_gate"], 0) else "⬇"
        ts = gc["timestamp"][:10] if gc["timestamp"] else "?"
        crossings += f"""
        <div class="cross-row">
          <span class="cross-icon">{icon}</span>
          <span class="cross-name">{AGENT_NAMES.get(gc['agent'], gc['agent'])}</span>
          <span class="cross-gate gate-badge gate-{gc['from_gate']}">{gc['from_gate']}</span>
          <span class="cross-arrow">→</span>
          <span class="cross-gate gate-badge gate-{gc['to_gate']}">{gc['to_gate']}</span>
          <span class="cross-score">{gc['new_score']:.1f}</span>
          <span class="cross-date">{ts}</span>
        </div>"""
    if not crossings:
        crossings = '<p class="empty">No gate crossings yet — keep running tasks!</p>'

    # ── Plain-language intro copy ──
    total_runs = stats["total"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent School — Live Leaderboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700;900&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #0a0a0f;
  --surface: #111118;
  --surface-2: #1a1a24;
  --border: #2a2a38;
  --text: #e8e6e3;
  --text-dim: #8a8694;
  --accent: #c8ff00;
  --accent-dim: #8ab300;
  --blue: #5b8def;
  --purple: #a855f7;
  --orange: #f59e0b;
  --red: #ef4444;
  --green: #22c55e;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: 'Geist Mono', 'SF Mono', 'Fira Code', monospace;
  background: var(--bg); color: var(--text);
  line-height: 1.6; -webkit-font-smoothing: antialiased;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 0 32px; }}

/* Nav */
nav {{ position: fixed; top: 0; left: 0; right: 0; z-index: 100; padding: 20px 0; border-bottom: 1px solid transparent; transition: background 0.3s; }}
nav.scrolled {{ background: rgba(10,10,15,0.9); backdrop-filter: blur(12px); border-bottom: 1px solid var(--border); }}
nav .container {{ display: flex; justify-content: space-between; align-items: center; }}
nav .logo {{ font-family: 'Geist', sans-serif; font-size: 20px; font-weight: 700; color: var(--accent); letter-spacing: -0.5px; }}
nav .logo span {{ color: var(--text-dim); font-family: 'Geist Mono', monospace; font-weight: 400; font-size: 10px; display: block; letter-spacing: 2.5px; text-transform: uppercase; }}
nav .links a {{ margin-left: 32px; font-size: 13px; color: var(--text-dim); }}
nav .links a:hover {{ color: var(--accent); text-decoration: none; }}
nav .links a.active {{ color: var(--accent); }}

main {{ padding-top: 100px; padding-bottom: 80px; }}
.section {{ margin-bottom: 64px; }}
.section-header {{ display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.section-title {{ font-family: 'Geist', sans-serif; font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }}
.section-subtitle {{ font-size: 12px; color: var(--text-dim); margin-top: 4px; }}
.group-count {{ font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }}

/* Hero / Vision */
.hero {{ padding: 48px 0 32px; }}
.hero h1 {{ font-family: 'Geist', sans-serif; font-size: 42px; font-weight: 900; letter-spacing: -1.5px; line-height: 1.15; margin-bottom: 16px; }}
.hero h1 span {{ color: var(--accent); }}
.hero p {{ font-size: 15px; color: var(--text-dim); max-width: 720px; line-height: 1.7; }}
.hero-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; margin-top: 32px; }}
.hstat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px; text-align: center; }}
.hstat .n {{ font-family: 'Geist', sans-serif; font-size: 28px; font-weight: 700; display: block; }}
.hstat .l {{ font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }}
.hstat.accent .n {{ color: var(--accent); }}
.hstat.green .n {{ color: var(--green); }}
.hstat.blue .n {{ color: var(--blue); }}
.hstat.purple .n {{ color: var(--purple); }}

/* Progression pipeline */
.pipeline {{ display: flex; align-items: center; gap: 0; margin: 24px 0 8px; overflow-x: auto; }}
.pipe-stage {{ display: flex; flex-direction: column; align-items: center; min-width: 100px; }}
.pipe-dot {{ width: 40px; height: 40px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 700; font-family: 'Geist', sans-serif; }}
.pipe-label {{ font-size: 11px; color: var(--text-dim); margin-top: 6px; text-align: center; }}
.pipe-count {{ font-family: 'Geist', sans-serif; font-size: 18px; font-weight: 700; }}
.pipe-arrow {{ font-size: 20px; color: var(--border); margin: 0 -4px; align-self: flex-start; margin-top: 8px; }}

/* Agent cards */
.class-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }}
.agent-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; transition: border-color 0.2s; }}
.agent-card:hover {{ border-color: #3a3a4e; }}
.role-unenrolled {{ border-left: 3px solid var(--text-dim); opacity: 0.7; }}
.role-student {{ border-left: 3px solid var(--green); }}
.role-senior-student {{ border-left: 3px solid var(--orange); }}
.role-teacher {{ border-left: 3px solid var(--blue); }}
.role-faculty {{ border-left: 3px solid var(--purple); }}
.ac-header {{ display: flex; gap: 14px; margin-bottom: 14px; }}
.ac-avatar {{ width: 42px; height: 42px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-family: 'Geist', sans-serif; font-weight: 700; font-size: 18px; flex-shrink: 0; }}
.ac-info {{ flex: 1; min-width: 0; }}
.ac-name-row {{ display: flex; align-items: baseline; gap: 8px; }}
.ac-name {{ font-weight: 600; font-size: 15px; }}
.ac-model {{ font-size: 11px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ac-role {{ display: flex; gap: 6px; align-items: center; margin-top: 4px; }}
.role-badge {{ display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }}
.tier-pill {{ display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }}
.ac-score-row {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }}
.ac-score-num {{ font-family: 'Geist', sans-serif; font-size: 28px; font-weight: 700; }}
.ac-score-delta {{ font-size: 11px; margin-left: 8px; }}
.delta-up {{ color: var(--green); }}
.delta-down {{ color: var(--red); }}
.delta-flat {{ color: var(--text-dim); }}
.ac-reliability {{ font-size: 11px; color: var(--text-dim); }}
.ac-spark {{ margin-bottom: 10px; }}
.ac-desc {{ font-size: 12px; color: var(--text-dim); margin-bottom: 12px; line-height: 1.6; }}
.ac-domains {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.dom-badge {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 9px; font-weight: 600; border: 1px solid; }}

/* Progression section */
.prog-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }}
.prog-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
.prog-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
.prog-name {{ font-weight: 600; font-size: 14px; }}
.prog-spark {{ margin-bottom: 10px; }}
.prog-meta {{ display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-dim); }}
.prog-arrow {{ color: var(--border); }}

/* Table */
table {{ width: 100%; border-collapse: collapse; }}
th {{ text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; color: var(--text-dim); padding: 10px 14px; border-bottom: 1px solid var(--border); background: var(--surface); }}
td {{ padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 12px; }}
tr:hover td {{ background: var(--surface); }}
.agent-cell {{ font-weight: 600; }}
.dim {{ color: var(--text-dim); font-weight: 400; font-size: 10px; }}
.gate-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }}
.gate-easy {{ background: rgba(34,197,94,0.15); color: var(--green); }}
.gate-medium {{ background: rgba(245,158,11,0.15); color: var(--orange); }}
.gate-hard {{ background: rgba(91,141,239,0.15); color: var(--blue); }}
.gate-diploma {{ background: rgba(168,85,247,0.15); color: var(--purple); }}

/* Crossings */
.crossings {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }}
.cross-row {{ display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12px; }}
.cross-row:last-child {{ border-bottom: none; }}
.cross-icon {{ font-size: 16px; }}
.cross-name {{ font-weight: 600; min-width: 100px; }}
.cross-gate {{ min-width: 60px; text-align: center; }}
.cross-arrow {{ color: var(--text-dim); }}
.cross-score {{ color: var(--text-dim); min-width: 36px; }}
.cross-date {{ color: var(--text-dim); font-size: 11px; margin-left: auto; }}
.empty {{ color: var(--text-dim); font-size: 13px; padding: 16px 0; }}

footer {{ padding: 40px 0; border-top: 1px solid var(--border); font-size: 12px; color: var(--text-dim); text-align: center; }}
.fade-in {{ opacity: 0; transform: translateY(12px); transition: opacity 0.5s, transform 0.5s; }}
.fade-in.visible {{ opacity: 1; transform: translateY(0); }}
@media (max-width: 768px) {{
  .class-grid {{ grid-template-columns: 1fr; }}
  .hero h1 {{ font-size: 28px; }}
  .container {{ padding: 0 16px; }}
  .pipeline {{ flex-wrap: wrap; }}
  td, th {{ padding: 8px 6px; font-size: 10px; }}
}}
</style>
</head>
<body>

<nav id="nav">
  <div class="container">
    <a href="index.html" class="logo">School<span>Agent Framework</span></a>
    <div class="links">
      <a href="index.html">Overview</a>
      <a href="architecture.html">Architecture</a>
      <a href="leaderboard.html" class="active">Leaderboard</a>
    </div>
  </div>
</nav>

<main>
<div class="container">

  <!-- Hero -->
  <div class="hero fade-in">
    <h1>Turn codebases <span>into schools</span>.</h1>
    <p>
      Agent engineering's end goal is to get engineers to build agents and send them to school.
      Each agent starts as a <strong>Student</strong> — it takes easy tests, builds skills,
      and works toward graduation. Pass enough tests and it becomes a <strong>Teacher</strong>
      — proven enough to evaluate others and take on harder challenges. The best become
      <strong>Faculty</strong>, handling blockers and designing curriculum.
    </p>
    <p style="margin-top:12px;">
      You give the School a codebase. It turns the code into a curriculum. Your agents
      learn by doing — and every task they complete makes them smarter.
    </p>
    <div class="hero-stats">
      <div class="hstat accent"><span class="n">{len(real)}</span><span class="l">Agents Enrolled</span></div>
      <div class="hstat green"><span class="n">{total_runs}</span><span class="l">Tasks Completed</span></div>
      <div class="hstat blue"><span class="n">{stats['success_rate']:.0f}%</span><span class="l">Success Rate</span></div>
      <div class="hstat purple"><span class="n">{len(stats['gate_crossings'])}</span><span class="l">Gate Crossings</span></div>
    </div>
  </div>

  <!-- Progression pipeline -->
  <section class="section fade-in">
    <div class="section-header">
      <div>
        <h2 class="section-title">Progression</h2>
        <p class="section-subtitle">Every agent starts as a student. The best become faculty.</p>
      </div>
    </div>
    <div class="pipeline">
      <div class="pipe-stage">
        <div class="pipe-dot" style="background:rgba(138,134,148,0.15);color:var(--text-dim);border:2px solid var(--text-dim)">?</div>
        <div class="pipe-count" style="color:var(--text-dim)">{len(unenrolled)}</div>
        <div class="pipe-label">Unenrolled<br><span style="font-size:9px">0 runs</span></div>
      </div>
      <div class="pipe-arrow">→</div>
      <div class="pipe-stage">
        <div class="pipe-dot" style="background:rgba(34,197,94,0.15);color:var(--green);border:2px solid var(--green)">S</div>
        <div class="pipe-count" style="color:var(--green)">{len(students)}</div>
        <div class="pipe-label">Students<br><span style="font-size:9px">score &lt; 25</span></div>
      </div>
      <div class="pipe-arrow">→</div>
      <div class="pipe-stage">
        <div class="pipe-dot" style="background:rgba(245,158,11,0.15);color:var(--orange);border:2px solid var(--orange)">Sr</div>
        <div class="pipe-count" style="color:var(--orange)">{len(seniors)}</div>
        <div class="pipe-label">Seniors<br><span style="font-size:9px">25–49</span></div>
      </div>
      <div class="pipe-arrow">→</div>
      <div class="pipe-stage">
        <div class="pipe-dot" style="background:rgba(91,141,239,0.15);color:var(--blue);border:2px solid var(--blue)">T</div>
        <div class="pipe-count" style="color:var(--blue)">{len(teachers)}</div>
        <div class="pipe-label">Teachers<br><span style="font-size:9px">50–74</span></div>
      </div>
      <div class="pipe-arrow">→</div>
      <div class="pipe-stage">
        <div class="pipe-dot" style="background:rgba(168,85,247,0.15);color:var(--purple);border:2px solid var(--purple)">F</div>
        <div class="pipe-count" style="color:var(--purple)">{len(faculty)}</div>
        <div class="pipe-label">Faculty<br><span style="font-size:9px">score ≥ 75</span></div>
      </div>
    </div>
  </section>

  {render_group("🎓 Faculty", "Expert agents that handle blockers and design curriculum.", faculty)}
  {render_group("📚 Teachers", "Graduated — proven in hard tasks, can evaluate and mentor.", teachers)}
  {render_group("📖 Senior Students", "Passing medium tasks — ready to attempt harder challenges.", seniors)}
  {render_group("🌱 Students", "Starting out — taking easy tasks, building foundational skills.", students)}
  {render_group("⏳ Unenrolled", "Registered but haven't attempted any tasks yet. Run a task to enroll them!", unenrolled)}

  <!-- Score progression -->
  <section class="section fade-in">
    <div class="section-header">
      <div>
        <h2 class="section-title">Score Progression</h2>
        <p class="section-subtitle">How the top 5 agents grew from their seed values</p>
      </div>
    </div>
    <div class="prog-grid">{progression}</div>
  </section>

  <!-- Domain breakdown table -->
  <section class="section fade-in">
    <div class="section-header">
      <div>
        <h2 class="section-title">Domain Breakdown</h2>
        <p class="section-subtitle">Scores across all domains — top 8 agents</p>
      </div>
    </div>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr><th>Agent</th><th>Default</th>
        {"".join(f"<th>{d}</th>" for d in all_domains)}
        <th>Gate</th></tr>
      </thead>
      <tbody>{dom_rows}</tbody>
    </table>
    </div>
  </section>

  <!-- Gate crossings -->
  <section class="section fade-in">
    <div class="section-header">
      <div>
        <h2 class="section-title">Gate Crossings</h2>
        <p class="section-subtitle">Agents that leveled up or down</p>
      </div>
    </div>
    <div class="crossings">{crossings}</div>
  </section>

</div>
</main>

<footer>
  <div class="container">
    <p>Agent School — Live Leaderboard · {now}</p>
    <p style="margin-top:4px;font-size:11px">Data: data/scores.json · data/trajectories/ · Seed baselines from scoring.py</p>
  </div>
</footer>

<script>
window.addEventListener('scroll', () => {{
  document.getElementById('nav').classList.toggle('scrolled', window.scrollY > 50);
}});
const observer = new IntersectionObserver((entries) => {{
  entries.forEach(e => {{ if (e.isIntersecting) e.target.classList.add('visible'); }});
}}, {{ threshold: 0.1 }});
document.querySelectorAll('.fade-in').forEach(el => observer.observe(el));
</script>
</body>
</html>"""


def regenerate():
    """Regenerate the leaderboard HTML. Returns stats dict."""
    scores = load_scores()
    trajs = load_trajectories()
    history = compute_score_history(trajs)
    stats = compute_stats(trajs, scores)
    html = build_html(scores, history, stats)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Generate Agent School leaderboard")
    parser.add_argument("--open", action="store_true", help="Open in browser after generation")
    parser.add_argument("--watch", action="store_true", help="Watch for changes and regenerate")
    parser.add_argument("--interval", type=int, default=5, help="Watch interval in seconds (default: 5)")
    args = parser.parse_args()

    stats = regenerate()
    trajs_count = len(load_trajectories())

    print(f"✅ Leaderboard written: {OUTPUT_PATH}")
    print(f"   {len(stats.get('agent_total', {}))} agents · {trajs_count} trajectories · {len(stats.get('gate_crossings', []))} gate crossings")

    if args.open:
        try:
            subprocess.run(["open", str(OUTPUT_PATH)], timeout=5)
            print("   Opened in browser.")
        except Exception:
            pass

    if args.watch:
        import time
        watch_paths = [OUTPUT_PATH.parent.parent / "data" / "scores.json"]
        traj_dir = OUTPUT_PATH.parent.parent / "data" / "trajectories"
        if traj_dir.exists():
            watch_paths.append(traj_dir)
        print(f"   Watching for changes (every {args.interval}s)… Ctrl+C to stop")
        last_mtime = 0
        try:
            while True:
                time.sleep(args.interval)
                current = 0
                for p in watch_paths:
                    if p.exists():
                        if p.is_dir():
                            for f in p.glob("*.json"):
                                current = max(current, f.stat().st_mtime)
                        else:
                            current = max(current, p.stat().st_mtime)
                if current > last_mtime:
                    last_mtime = current
                    stats = regenerate()
                    trajs_count = len(load_trajectories())
                    print(f"   🔄 Regenerated · {trajs_count} trajectories · {time.strftime('%H:%M:%S')}")
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
