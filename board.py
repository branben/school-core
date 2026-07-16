#!/usr/bin/env python3
"""
board.py — Self-contained kanban board HTML generator for the Durable Cloud Board.

Generates a fully self-contained HTML page with embedded CSS (Lavish dark-editorial
design system) and vanilla JS that polls ``/api/board.json`` every 15 seconds.

Usage::

    from board import build_board_html
    html = build_board_html(issues, processed, last_run)
    Path("board.html").write_text(html)
"""

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List, Optional


# ── Column-assignment helpers ───────────────────────────────────────────────


def _build_last_run_map(last_run: list[dict]) -> dict[int, dict]:
    """Map each issue number to its **latest** last_run entry.

    Parameters
    ----------
    last_run : list[dict]
        Append-only list of ``{issue, status, agent, score, timestamp}`` dicts,
        most recently appended last.

    Returns
    -------
    dict[int, dict]
        ``{issue_number: latest_entry}``.
    """
    result: dict[int, dict] = {}
    for entry in reversed(last_run):
        num = entry.get("issue")
        if num is not None and num not in result:
            result[num] = entry
    return result


def assign_column(
    issue: dict,
    processed: set[int],
    last_run_map: dict[int, dict],
) -> str:
    """Return the kanban column key for *issue*.

    Priority order:

    1. **Last-run status** overrides:
       - ``"in_progress"`` → ``"in_progress"``
       - ``"review"`` / ``"in_review"`` → ``"in_review"``
       - ``"done"`` / ``"success"`` → ``"done"``
    2. **Processed** (number in *processed* set) → ``"done"``
    3. **Open issue** (``state != "done"``) → ``"todo"``
    4. **Fallback** (closed / unknown) → ``"done"``

    Parameters
    ----------
    issue : dict
        Issue record, must contain ``"issue_number"``.  May include ``"state"``
        (defaults to ``"open"``).
    processed : set[int]
        Set of issue numbers already processed (Done).
    last_run_map : dict[int, dict]
        Issue-number → latest entry map from :func:`_build_last_run_map`.

    Returns
    -------
    str
        One of ``"todo"``, ``"in_progress"``, ``"in_review"``, ``"done"``.
    """
    num = issue["issue_number"]

    # Priority 1: last-run status overrides
    if num in last_run_map:
        status = last_run_map[num].get("status", "")
        if status == "in_progress":
            return "in_progress"
        if status in ("review", "in_review"):
            return "in_review"
        if status in ("done", "success"):
            return "done"
        # "blocked", "error", "dry_run" — fall through

    # Priority 2: processed → Done
    if num in processed:
        return "done"

    # Priority 3: open issue → To Do
    issue_state = (issue.get("state") or "open").lower()
    if issue_state != "done":
        return "todo"

    # Priority 4: closed / done state → Done
    return "done"


# ── Card renderer ────────────────────────────────────────────────────────────


def _render_card(issue: dict, lr_entry: Optional[dict]) -> str:
    """Render a single kanban card HTML snippet.

    Parameters
    ----------
    issue : dict
        Issue dict with keys ``issue_number``, ``title``, ``domain``,
        ``difficulty``.
    lr_entry : dict or None
        The latest last-run entry for this issue (if any); may carry ``agent``
        and ``score``.

    Returns
    -------
    str
        HTML ``<div class="card">…</div>`` (no newline at end).
    """
    num = issue["issue_number"]
    title = escape(issue.get("title", ""))
    domain = escape(issue.get("domain", ""))
    difficulty = escape(issue.get("difficulty", ""))
    agent = escape(str(lr_entry.get("agent", ""))) if lr_entry else ""
    score = lr_entry.get("score") if lr_entry else None

    parts = ['<div class="card">']
    parts.append(f'<div class="card-number">#{num}</div>')
    parts.append(f'<div class="card-title">{title}</div>')
    parts.append('<div class="card-badges">')
    if domain:
        parts.append(f'<span class="badge badge-domain">{domain}</span>')
    if difficulty:
        parts.append(f'<span class="badge badge-diff">{difficulty}</span>')
    parts.append("</div>")
    if agent:
        parts.append(f'<div class="card-agent">agent: {agent}</div>')
    if score is not None:
        parts.append(f'<div class="card-score">{score}</div>')
    parts.append("</div>")
    return "".join(parts)


# ── Static assets (CSS + JS) ────────────────────────────────────────────────


_CSS = r""":root {
  --ink: #0e0f0d;
  --cream: #f5f1e8;
  --brass: #c0a050;
  --brass-dim: #8a7a40;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { font-size: 16px; }
body {
  background: var(--ink);
  color: var(--cream);
  font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  line-height: 1.5;
  padding: 2rem;
  min-height: 100vh;
}
h1, h2, h3, h4 {
  font-family: Georgia, 'Palatino Linotype', Palatino, 'Times New Roman', serif;
}
h1 {
  font-size: 1.75rem;
  color: var(--brass);
}
h2 {
  font-size: 1.1rem;
  color: var(--brass);
}
.board-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 1rem;
  margin-bottom: 1.5rem;
  border-bottom: 1px solid var(--brass-dim);
}
.board-header time {
  font-size: 0.8rem;
  color: var(--brass-dim);
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
}
.board-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1rem;
  min-width: 0;
}
.col {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--brass-dim);
  border-radius: 8px;
  padding: 1rem;
  min-width: 0;
}
.col h2 {
  margin-bottom: 0.75rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid rgba(192, 160, 80, 0.2);
}
.cards {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(192, 160, 80, 0.15);
  border-radius: 6px;
  padding: 0.7rem;
  transition: border-color 0.2s, background 0.2s;
}
.card:hover {
  border-color: var(--brass);
  background: rgba(192, 160, 80, 0.08);
}
.card-number {
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
  font-size: 0.7rem;
  color: var(--brass-dim);
  margin-bottom: 0.2rem;
}
.card-title {
  font-size: 0.9rem;
  margin-bottom: 0.4rem;
  line-height: 1.4;
}
.card-badges {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
  margin-bottom: 0.3rem;
}
.badge {
  font-size: 0.65rem;
  padding: 0.15rem 0.4rem;
  border-radius: 3px;
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
}
.badge-domain {
  background: rgba(192, 160, 80, 0.15);
  color: var(--brass);
}
.badge-diff {
  background: rgba(255, 255, 255, 0.08);
  color: var(--cream);
}
.card-agent {
  font-size: 0.75rem;
  color: var(--brass-dim);
  margin-bottom: 0.1rem;
}
.card-score {
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
  font-size: 0.8rem;
  color: var(--brass);
}
.empty-col {
  color: var(--brass-dim);
  font-size: 0.8rem;
  padding: 1rem 0;
  text-align: center;
}
"""

_JS_POLL = """
<script>
(function() {
  'use strict';
  function rerender(columns) {
    var keys = ['todo','in_progress','in_review','done'];
    var cols = document.querySelectorAll('.col');
    for (var i = 0; i < cols.length; i++) {
      var container = cols[i].querySelector('.cards');
      if (!container) continue;
      var key = keys[i] || '';
      var cards = (columns && columns[key]) || [];
      if (!cards.length) {
        container.innerHTML = '<div class="empty-col">\u2014</div>';
        continue;
      }
      var html = '';
      for (var j = 0; j < cards.length; j++) {
        var c = cards[j];
        html += '<div class="card">';
        html += '<div class="card-number">#' + c.n + '</div>';
        html += '<div class="card-title">' + c.t + '</div>';
        html += '<div class="card-badges">';
        if (c.dom) html += '<span class="badge badge-domain">' + c.dom + '</span>';
        if (c.diff) html += '<span class="badge badge-diff">' + c.diff + '</span>';
        html += '</div>';
        if (c.a) html += '<div class="card-agent">agent: ' + c.a + '</div>';
        if (c.s != null) html += '<div class="card-score">' + c.s + '</div>';
        html += '</div>';
      }
      container.innerHTML = html;
    }
  }
  function poll() {
    fetch('/api/board.json')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data && data.columns) rerender(data.columns);
      })
      .catch(function() {});
  }
  setInterval(poll, 15000);
})();
</script>
"""

_COLUMN_META = [
    ("todo", "To Do"),
    ("in_progress", "In Progress"),
    ("in_review", "In Review"),
    ("done", "Done"),
]


# ── Public API ──────────────────────────────────────────────────────────────


def build_board_html(
    issues: list[dict],
    processed: list[int],
    last_run: list[dict],
) -> str:
    """Generate a self-contained kanban board HTML page.

    The returned HTML is fully stand-alone:

    * Embeded dark-editorial CSS (Lavish design system) in ``<style>``.
    * Four CSS-grid columns: To Do, In Progress, In Review, Done.
    * Task cards with number, title, domain/difficulty badges, agent, score.
    * Vanilla JS polling ``/api/board.json`` every 15 seconds.

    Parameters
    ----------
    issues : list[dict]
        Issue dicts (e.g. from :func:`github_fetcher.fetch_issues`).
        Each should contain ``issue_number``, ``title``, ``domain``,
        ``difficulty``, and optionally ``state``.
    processed : list[int]
        Issue numbers that have been processed (Done).
    last_run : list[dict]
        Append-only log entries (``issue``, ``status``, ``agent``, ``score``,
        ``timestamp``).

    Returns
    -------
    str
        Complete ``<!doctype html>`` document.
    """
    processed_set: set[int] = set(processed)
    lr_map = _build_last_run_map(last_run)

    columns: dict[str, list[dict]] = {
        "todo": [],
        "in_progress": [],
        "in_review": [],
        "done": [],
    }
    for issue in issues:
        col = assign_column(issue, processed_set, lr_map)
        lr_entry = lr_map.get(issue["issue_number"])
        columns[col].append({
            "issue": issue,
            "lr_entry": lr_entry,
        })

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Render columns
    col_html_parts: list[str] = []
    for key, label in _COLUMN_META:
        items = columns[key]
        cards_html = "".join(
            _render_card(item["issue"], item["lr_entry"]) for item in items
        )
        if not cards_html:
            cards_html = '<div class="empty-col">\u2014</div>'
        col_html_parts.append(
            f'<section class="col" data-status="{key}">'
            f"<h2>{label}</h2>"
            f'<div class="cards">{cards_html}</div>'
            f"</section>\n"
        )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        "<title>School Board \u2014 Task Kanban</title>\n"
        f"<style>\n{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="board-header">\n'
        "<h1>Task Board</h1>\n"
        f'<time datetime="{now}">Updated {now}</time>\n'
        "</div>\n"
        '<div class="board-grid">\n'
        f'{"".join(col_html_parts)}'
        "</div>\n"
        f"{_JS_POLL}\n"
        "</body>\n"
        "</html>"
    )
