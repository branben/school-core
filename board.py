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
       - ``"retry"`` → ``"retry"``
       - ``"blocked"`` → ``"blocked"``
       - ``"crew_in_flight"`` → ``"crew_in_flight"``
       - ``"school-failed"`` / ``"error"`` → ``"school_failed"``
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
        One of ``"todo"``, ``"in_progress"``, ``"in_review"``, ``"retry"``,
        ``"blocked"``, ``"crew_in_flight"``, ``"school_failed"``, ``"done"``.
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
        if status == "retry":
            return "retry"
        if status == "blocked":
            return "blocked"
        if status == "crew_in_flight":
            return "crew_in_flight"
        if status in ("school-failed", "error"):
            return "school_failed"
        # Unknown/non-terminal statuses fall through to the remaining rules.

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


def _normalize_card(issue: dict, lr_entry: Optional[dict]) -> dict:
    """Canonical card shape shared by the static render, the live JS poll, and
    the ``/api/board.json`` endpoint.

    Keys: ``n`` (number), ``t`` (title), ``dom`` (domain), ``diff``
    (difficulty), ``a`` (agent), ``s`` (score), ``traj`` (trajectory basename).
    """
    return {
        "n": issue.get("issue_number", issue.get("number", "")),
        "t": issue.get("title", ""),
        "dom": issue.get("domain", ""),
        "diff": issue.get("difficulty", ""),
        "a": str(lr_entry.get("agent", "")) if lr_entry else "",
        "s": lr_entry.get("score") if lr_entry else None,
        "traj": Path(lr_entry.get("trajectory", "")).name
        if lr_entry and lr_entry.get("trajectory")
        else None,
        "candidate_id": issue.get("candidate_id", ""),
        "head_sha": issue.get("head_sha", ""),
        "base_sha": issue.get("base_sha", ""),
        "local_gate": issue.get("local_gate", "unknown"),
        "pr_state": issue.get("pr_state", "unknown"),
        "ci_state": issue.get("ci_state", "unknown"),
        "approval_state": issue.get("approval_state", "unknown"),
        "merge_state": issue.get("merge_state", "unknown"),
        "beads_state": issue.get("beads_state", "unknown"),
    }


def _render_card(card: dict) -> str:
    """Render a single kanban card HTML snippet from a canonical card dict.

    Parameters
    ----------
    card : dict
        Canonical card (see :func:`_normalize_card`) with keys ``n``, ``t``,
        ``dom``, ``diff``, ``a``, ``s``.

    Returns
    -------
    str
        HTML ``<div class="card">…</div>`` (no newline at end).
    """
    num = escape(str(card.get("n", "")))
    title = escape(str(card.get("t", "")))
    domain = escape(str(card.get("dom", "")))
    difficulty = escape(str(card.get("diff", "")))
    agent = escape(str(card.get("a", "")))
    score = card.get("s")
    traj = card.get("traj")

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
    if traj:
        parts.append(f'<a class="card-session" href="/trajectory/{escape(str(traj))}">session ↗</a>')
    candidate_id = escape(str(card.get("candidate_id", "")))
    head_sha = escape(str(card.get("head_sha", "")))
    merge_state = escape(str(card.get("merge_state", "unknown")))
    if candidate_id or head_sha:
        parts.append(
            '<div class="card-evidence">'
            f'<span>candidate: {candidate_id or "unknown"}</span>'
            f'<span>head: {head_sha or "unknown"}</span>'
            f'<span>merge: {merge_state}</span>'
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _render_timeline_event(event: dict) -> str:
    """Render one activity event as escaped, human-readable timeline HTML."""
    kind = escape(str(event.get("kind", event.get("type", "activity"))))
    timestamp = escape(str(event.get("timestamp", "")))
    issue = escape(str(event.get("issue", "")))
    agent = escape(str(event.get("agent", "")))
    summary = escape(str(event.get("summary", event.get("description", ""))))
    plan_expected = escape(str(event.get("plan_expected", "")))
    code_revealed = escape(str(event.get("code_revealed", "")))
    decision = escape(str(event.get("decision", "")))
    revisit = escape(str(event.get("revisit", "")))
    evidence = event.get("evidence", [])
    evidence_html = ""
    if isinstance(evidence, list) and evidence:
        evidence_html = (
            '<div class="timeline-evidence"><strong>Evidence:</strong> '
            + ", ".join(escape(str(item)) for item in evidence)
            + "</div>"
        )

    fields = ""
    if plan_expected or code_revealed or decision or revisit:
        fields = (
            '<div class="timeline-fields">'
            f'<div><strong>Plan said:</strong> {plan_expected or "—"}</div>'
            f'<div><strong>Code revealed:</strong> {code_revealed or "—"}</div>'
            f'<div><strong>Conservative choice:</strong> {decision or "—"}</div>'
            f'<div><strong>Revisit if:</strong> {revisit or "—"}</div>'
            "</div>"
        )
    identity = " · ".join(part for part in (issue, agent) if part)
    return (
        '<article class="timeline-event">'
        f'<div class="timeline-event-meta"><span class="timeline-kind">{kind}</span>'
        f'<span>{timestamp}</span><span>{identity}</span></div>'
        f'<div class="timeline-summary">{summary or "—"}</div>'
        f"{fields}{evidence_html}"
        "</article>"
    )


def _render_timeline(events: list[dict]) -> str:
    if not events:
        return '<div class="timeline-empty">No activity events yet.</div>'
    return "".join(_render_timeline_event(event) for event in events)


# ── Static assets (CSS + JS) ────────────────────────────────────────────────


_CSS = r""":root {
  --ink: #0e0f0d;
  --cream: #f5f1e8;
  --brass: #c0a050;
  --brass-dim: #8a7a40;
  --timeline-bg: #141612;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; }
body {
  background: var(--ink);
  color: var(--cream);
  font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  line-height: 1.5;
  padding: 1rem 1.25rem;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
  padding-bottom: 0.6rem;
  margin-bottom: 0.75rem;
  border-bottom: 1px solid var(--brass-dim);
  flex: 0 0 auto;
}
.board-header time {
  font-size: 0.8rem;
  color: var(--brass-dim);
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
}  .board-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 0.75rem;
  min-width: 0;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
.col {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--brass-dim);
  border-radius: 8px;
  padding: 0.6rem;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.col h2 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid rgba(192, 160, 80, 0.2);
  flex: 0 0 auto;
}
.col-count {
  font-size: 0.7rem;
  background: rgba(138, 122, 64, 0.2);
  color: var(--brass-dim);
  padding: 0.1rem 0.45rem;
  border-radius: 8px;
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
}
.cards {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  overflow-y: auto;
  min-height: 0;
  padding-right: 2px;
}
.card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(192, 160, 80, 0.15);
  border-radius: 6px;
  padding: 0.5rem 0.55rem;
  transition: border-color 0.2s, background 0.2s;
}
.card:hover {
  border-color: var(--brass);
  background: rgba(192, 160, 80, 0.08);
}
.card-number {
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
  font-size: 0.65rem;
  color: var(--brass-dim);
  margin-bottom: 0.15rem;
}
.card-title {
  font-size: 0.82rem;
  margin-bottom: 0.3rem;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
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
.card-session {
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
  font-size: 0.65rem;
  color: var(--brass);
  text-decoration: none;
  margin-top: 0.2rem;
  display: inline-block;
}
.card-session:hover {
  text-decoration: underline;
}
.card-evidence {
  display: grid;
  gap: 0.15rem;
  margin-top: 0.35rem;
  color: var(--brass-dim);
  font: 0.62rem ui-monospace, 'SF Mono', 'Fira Code', monospace;
  overflow-wrap: anywhere;
}
.empty-col {
  color: var(--brass-dim);
  font-size: 0.8rem;
  padding: 1rem 0;
  text-align: center;
}
.board-filter {
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--brass-dim);
  border-radius: 4px;
  color: var(--cream);
  padding: 0.25rem 0.5rem;
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
  font-size: 0.75rem;
  width: 180px;
}
.board-filter::placeholder {
  color: var(--brass-dim);
}
.live-indicator {
  font-size: 0.75rem;
  color: #555;
  transition: color 0.3s;
  margin-right: 0.5rem;
}
.live-indicator.on {
  color: #4caf50;
}
.timeline-panel {
  background: var(--timeline-bg);
  border: 1px solid var(--brass-dim);
  border-radius: 8px;
  margin-top: 0.75rem;
  padding: 0.6rem 0.75rem;
  flex: 0 0 18rem;
  min-height: 0;
  overflow: hidden;
}
.timeline-panel h2 {
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid rgba(192, 160, 80, 0.2);
  padding-bottom: 0.4rem;
  margin-bottom: 0.4rem;
}
.timeline-count {
  color: var(--brass-dim);
  font: 0.7rem ui-monospace, 'SF Mono', 'Fira Code', monospace;
}
.timeline-events {
  max-height: 13rem;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  padding-right: 0.25rem;
}
.timeline-event {
  border-left: 2px solid var(--brass-dim);
  padding: 0.35rem 0.55rem;
  background: rgba(255, 255, 255, 0.035);
  border-radius: 0 5px 5px 0;
}
.timeline-event-meta,
.timeline-evidence {
  color: var(--brass-dim);
  font: 0.68rem ui-monospace, 'SF Mono', 'Fira Code', monospace;
  display: flex;
  gap: 0.7rem;
  flex-wrap: wrap;
}
.timeline-kind {
  color: var(--brass);
  text-transform: uppercase;
}
.timeline-summary {
  margin: 0.2rem 0;
  font-size: 0.8rem;
}
.timeline-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.2rem 0.8rem;
  color: var(--cream);
  font-size: 0.7rem;
  margin: 0.25rem 0;
}
.timeline-fields strong { color: var(--brass); }
.timeline-empty { color: var(--brass-dim); font-size: 0.8rem; }
@media (max-width: 800px) {
  .timeline-fields { grid-template-columns: 1fr; }
  .timeline-panel { flex-basis: 14rem; }
}
"""

_JS_POLL = """
<script>
(function() {
  'use strict';
  function rerender(columns) {
    var keys = ['todo','in_progress','in_review','retry','blocked','crew_in_flight','school_failed','done'];
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
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  function renderTimeline(events) {
    var panel = document.querySelector('#timeline-events');
    var count = document.getElementById('timeline-count');
    if (!panel) return;
    if (count) count.textContent = String((events || []).length) + ' events';
    if (!events || !events.length) {
      panel.innerHTML = '<div class="timeline-empty">No activity events yet.</div>';
      return;
    }
    panel.innerHTML = events.map(function(event) {
      event = event || {};
      var kind = escapeHtml(event.kind || event.type || 'activity');
      var timestamp = escapeHtml(event.timestamp || '');
      var identity = [event.issue || '', event.agent || ''].filter(Boolean).map(escapeHtml).join(' · ');
      var summary = escapeHtml(event.summary || event.description || '—');
      var fields = '';
      if (event.plan_expected || event.code_revealed || event.decision || event.revisit) {
        fields = '<div class="timeline-fields">'
          + '<div><strong>Plan said:</strong> ' + escapeHtml(event.plan_expected || '—') + '</div>'
          + '<div><strong>Code revealed:</strong> ' + escapeHtml(event.code_revealed || '—') + '</div>'
          + '<div><strong>Conservative choice:</strong> ' + escapeHtml(event.decision || '—') + '</div>'
          + '<div><strong>Revisit if:</strong> ' + escapeHtml(event.revisit || '—') + '</div>'
          + '</div>';
      }
      var evidence = Array.isArray(event.evidence) && event.evidence.length
        ? '<div class="timeline-evidence"><strong>Evidence:</strong> ' + event.evidence.map(escapeHtml).join(', ') + '</div>'
        : '';
      return '<article class="timeline-event">'
        + '<div class="timeline-event-meta"><span class="timeline-kind">' + kind + '</span>'
        + '<span>' + timestamp + '</span><span>' + identity + '</span></div>'
        + '<div class="timeline-summary">' + summary + '</div>' + fields + evidence + '</article>';
    }).join('');
  }
  function loadTimeline() {
    fetch('/api/timeline?n=50')
      .then(function(r) { return r.json(); })
      .then(function(data) { if (data && data.events) renderTimeline(data.events); })
      .catch(function() {});
  }
  function live(on) {
    var ind = document.getElementById('live-indicator');
    if (ind) ind.className = 'live-indicator' + (on ? ' on' : '');
  }
  // SSE live stream (primary)
  if (typeof EventSource !== 'undefined') {
    var es = new EventSource('/stream');
    es.addEventListener('board', function(e) {
      try {
        var data = JSON.parse(e.data);
        if (data && data.columns) rerender(data.columns);
        if (data && data.timeline) renderTimeline(data.timeline);
      } catch(_) {}
    });
    es.addEventListener('activity', function(e) {
      try {
        var entries = JSON.parse(e.data);
        if (!Array.isArray(entries) || !entries.length) return;
        var panel = document.getElementById('timeline-events');
        if (!panel) return;
        var current = Array.from(panel.querySelectorAll('.timeline-event')).length;
        if (current < 50) {
          fetch('/api/timeline?n=50')
            .then(function(r) { return r.json(); })
            .then(function(data) { if (data && data.events) renderTimeline(data.events); })
            .catch(function() {});
        }
      } catch(_) {}
    });
    es.onopen = function() { live(true); };
    es.onerror = function() { live(false); };
  }
  // Fallback: 15s poll (for browsers that don't support EventSource)
  function poll() {
    fetch('/api/board.json')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data && data.columns) rerender(data.columns);
        if (data && data.timeline) renderTimeline(data.timeline);
      })
      .catch(function() {});
  }
  setInterval(poll, 15000);
  loadTimeline();
})();
</script>
"""

_COLUMN_META = [
    ("todo", "To Do"),
    ("in_progress", "In Progress"),
    ("in_review", "In Review"),
    ("retry", "Retry Pending"),
    ("blocked", "Blocked"),
    ("crew_in_flight", "Crew In Flight"),
    ("school_failed", "School Failed"),
    ("done", "Done"),
]


# ── Public API ──────────────────────────────────────────────────────────────


def build_board_html(
    issues: list[dict],
    processed: list[int],
    last_run: list[dict],
    timeline: Optional[list[dict]] = None,
) -> str:
    """Generate a self-contained kanban board HTML page.

    The returned HTML is fully stand-alone:

    * Embeded dark-editorial CSS (Lavish design system) in ``<style>``.
    * Four CSS-grid columns: To Do, In Progress, In Review, Done.
    * Task cards with number, title, domain/difficulty badges, agent, score.
    * Vanilla JS polling ``/api/board.json`` every 15 seconds.
    * Optional structured activity timeline, refreshed by ``/api/timeline``.

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
        "retry": [],
        "blocked": [],
        "crew_in_flight": [],
        "school_failed": [],
        "done": [],
    }
    for issue in issues:
        col = assign_column(issue, processed_set, lr_map)
        lr_entry = lr_map.get(issue["issue_number"])
        columns[col].append(_normalize_card(issue, lr_entry))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Render columns
    col_html_parts: list[str] = []
    for key, label in _COLUMN_META:
        items = columns[key]
        cards_html = "".join(_render_card(item) for item in items)
        if not cards_html:
            cards_html = '<div class="empty-col">\u2014</div>'
        col_html_parts.append(
            f'<section class="col" data-status="{key}">'
            f"<h2>{label}<span class=\"col-count\">{len(items)}</span></h2>"
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
        '<span id="live-indicator" class="live-indicator">● live</span>\n'
        '<input id="board-filter" class="board-filter" placeholder="filter titles…" oninput="boardFilter()">\n'
        f'<time datetime="{now}">Updated {now}</time>\n'
        "</div>\n"
        '<div class="board-grid">\n'
        f'{"".join(col_html_parts)}'
        "</div>\n"
        '<section class="timeline-panel" aria-labelledby="timeline-heading">\n'
        '<h2 id="timeline-heading">Deviation / event timeline'
        f'<span id="timeline-count" class="timeline-count">{len(timeline or [])} events</span></h2>'
        f'<div id="timeline-events" class="timeline-events">{_render_timeline(timeline or [])}</div>'
        "</section>\n"
        f"{_JS_POLL}\n"
        "</body>\n"
        "</html>"
    )
