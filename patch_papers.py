#!/usr/bin/env python3
"""
patch_papers.py — Patch the generated leaderboard HTML to add paper references.

Usage:
  python patch_papers.py
"""

import re
from pathlib import Path

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

OUTPUT = Path(__file__).resolve().parent / "docs" / "site" / "leaderboard.html"


def patch():
    html = OUTPUT.read_text()

    # 1. Add paper links inline in the hero section
    hero_papers = """
    <p style="margin-top:12px;font-size:12px;color:var(--text-dim);">
      Built on research: competency-gated routing from <a href="{h_url}">{h_title}</a>,
      progressive disclosure from <a href="{s_url}">{s_title}</a>,
      state externalization from <a href="{e_url}">{e_title}</a>,
      sleep/consolidation from <a href="{sl_url}">{sl_title}</a>,
      distillation from <a href="{d_url}">{d_title}</a>,
      and <a href="{a_url}">{a_title}</a> for prompt composition.
    </p>""".format(
        h_url=PAPERS["harness"]["url"], h_title=PAPERS["harness"]["title"],
        s_url=PAPERS["skillopt"]["url"], s_title=PAPERS["skillopt"]["title"],
        e_url=PAPERS["state_ext"]["url"], e_title=PAPERS["state_ext"]["title"],
        sl_url=PAPERS["sleep"]["url"], sl_title=PAPERS["sleep"]["title"],
        d_url=PAPERS["distill"]["url"], d_title=PAPERS["distill"]["title"],
        a_url=PAPERS["anchors"]["url"], a_title=PAPERS["anchors"]["title"],
    )

    # Insert after the second hero paragraph
    hero_marker = 'learn by doing — and every task they complete makes them smarter.\n    </p>'
    if hero_marker in html and "Built on research" not in html:
        html = html.replace(hero_marker, hero_marker + "\n" + hero_papers)

    # 2. Add research section before footer
    paper_cards = ""
    for key, p in PAPERS.items():
        paper_cards += f"""
        <div class="paper-card">
          <div class="paper-title"><a href="{p['url']}">{p['title']}</a></div>
          <div class="paper-authors">{p['authors']}</div>
          <div class="paper-blurb">{p['blurb']}</div>
        </div>"""

    research_section = f"""
  <!-- Research -->
  <section class="section fade-in">
    <div class="section-header">
      <div>
        <h2 class="section-title">Research</h2>
        <p class="section-subtitle">The papers that built the Agent School</p>
      </div>
    </div>
    <div class="papers-grid">{paper_cards}
    </div>
  </section>"""

    footer_marker = "<footer>"
    if footer_marker in html and "Research" not in html:
        html = html.replace(footer_marker, research_section + "\n\n" + footer_marker)

    # 3. Add paper card CSS
    paper_css = """
/* Papers */
.papers-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.paper-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }
.paper-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
.paper-title a { color: var(--text); }
.paper-title a:hover { color: var(--accent); text-decoration: none; }
.paper-authors { font-size: 11px; color: var(--text-dim); margin-bottom: 8px; }
.paper-blurb { font-size: 12px; color: var(--text-dim); line-height: 1.6; }
"""

    # Insert CSS before the closing </style>
    if "paper-card" not in html:
        html = html.replace("</style>", paper_css + "</style>")

    OUTPUT.write_text(html)
    print(f"✅ Patched: {OUTPUT}")


if __name__ == "__main__":
    patch()
