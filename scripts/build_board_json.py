#!/usr/bin/env python3
"""
build_board_json.py - Export slice-tracker board.json from pipeline + beads state.

Zero-dependency (stdlib only), in the school's "compiler before critic" spirit.

Wire format consumed by docs/templates/triage-board.html:

    {
      "generated_at":  "2026-09-13T00:00:00Z",
      "board": [
        {"id": "366", "title": "...", "lane": "later",
         "actor": "", "reason": "open", "tag": "security"}
      ]
    }

Lanes (the four triage columns from the thariqs 18-editor board):

    now   - actively worked this cycle (in_progress / crew_in_flight)
    next  - queued behind a current slice (in_review / retry / blocked)
    later - not yet touched by the loop (open cache issues seed this lane)
    cut   - loop decided the slice is done or not actionable (success / error /
            school-failed / done), with the original status preserved as reason

The board is a VIEW. It never writes the durable store (ADR 0005 property):
drag in the HTML only changes the LOCAL ordering; the "copy bd commands" export
emits `bd update <id> --status <lane>` strings for a human or an agent to run.

Data sources: data/last_run.json (loop outcome per issue) +
data/issues_cache.json (GitHub issue titles + open-state seeding).

Usage:
    python3 scripts/build_board_json.py [--out data/board.json] [--max-cards 250]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# TODO(U5): import from board.py:assign_column once that lives standalone;
# this module keeps its own mapping so it is independently testable.
_LANE_BY_STATUS = {
    "in_progress": "now",
    "crew_in_flight": "now",
    "in_review": "next",
    "retry": "next",
    "blocked": "next",
    "success": "cut",
    "done": "cut",
    "error": "cut",
    "school-failed": "cut",
}


def lane_for(status: str | None) -> str:
    """Map a pipeline status to a triage lane."""
    return _LANE_BY_STATUS.get(status or "", "later")


def infer_tag(run: dict, title: str = "") -> str:
    """Best-effort tag for filtering, from the cache domain or the title."""
    domain = run.get("domain")
    if domain and domain not in ("_default", "None", ""):
        return str(domain)
    low = title.lower()
    for kw in ("security", "churn", "triage", "e2e", "test", "feat", "bug"):
        if kw in low:
            return kw
    return "slice"


def card_from_run_dict(run: dict) -> dict:
    """Convert a raw last_run row into a board card dict."""
    title = run.get("title", run.get("trajectory") or f"issue {run.get('issue')}")
    lane = lane_for(run.get("status"))
    return {
        "id": str(run.get("issue", "")),
        "title": title,
        "lane": lane,
        "orig_lane": lane,   # pipeline truth; the board can move cards, this stays
        "actor": str(run.get("agent") or ""),
        "reason": str(run.get("rejection") or run.get("status") or ""),
        "tag": infer_tag(run, title),
    }


def latest_by_issue(runs: list[dict]) -> dict[str, dict]:
    """Last-write-wins per issue id (runs are chronological)."""
    latest: dict[str, dict] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        issue = str(run.get("issue") or "")
        if issue:
            latest[issue] = run
    return latest


def build_board(
    runs: list[dict],
    titles: dict[str, str] | None = None,
    max_cards: int = 250,
    queued: list[dict] | None = None,
) -> dict:
    """Produce the board.json wire format.

    Parameters
    ----------
    runs : list[dict]   — parsed data/last_run.json (chronological)
    titles : dict|None  — {issue_number: title} from data/issues_cache.json
    max_cards : int     — truncate to the newest N cards (board stays scannable)
    queued : list[dict] | None
        — open cache issues not yet touched by the loop. They seed the "later"
          lane so the board shows queued work instead of an empty queue. Any
          issue already present in ``runs`` is not duplicated.
    """
    latest = latest_by_issue(runs)
    cards = []
    for issue, run in latest.items():
        title = (titles or {}).get(issue, "")
        if not title:
            title = run.get("title") or f"issue {issue}"
        r = dict(run)
        r["title"] = title
        cards.append(card_from_run_dict(r))

    if queued:
        for item in queued:
            iid = str(item.get("issue_number") or "")
            if not iid or iid in latest:
                continue
            if str(item.get("state")) != "open":
                continue
            title = str(item.get("title") or f"issue {iid}")
            cards.append(
                {
                    "id": iid,
                    "title": title,
                    "lane": "later",
                    "orig_lane": "later",
                    "actor": "",
                    "reason": "open",
                    "tag": infer_tag({"domain": item.get("domain")}, title),
                }
            )
    # newest first (runs were chronological; latest dict preserved insertion order
    # of first-seen, so re-sort by generated timestamp desc, then issue desc)
    def _sort_key(c: dict) -> tuple[str, str]:
        return (c["lane"], -int(c["id"]) if c["id"].lstrip("-").isdigit() else 0)
    cards.sort(key=_sort_key, reverse=True)
    cards = cards[:max_cards]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "board": cards,
    }


def build_bd_commands(board: list[dict]) -> str:
    """Serialize a triage board into runnable ``bd`` invocations.

    Lane → command mapping (the apply-gate contract; never writes the store
    itself, only prints commands for the configured filter/human to run):

        now   -> bd update <id> --claim                 (claim the slice)
        next  -> bd update <id> --status open           (queue it; unclaim)
        later -> bd update <id> --status open           (queue it; unclaim)
        cut   -> bd close <id> --reason="Triage cut"    (decline/reorder)

    A card is emitted only when its ``lane`` differs from ``orig_lane`` (its
    pipeline truth). Dragging untouched cards around is free; nothing is
    emitted, so re-exporting is idempotent.
    """
    lines: list[str] = []
    for card in board:
        lane = card.get("lane")
        orig = card.get("orig_lane", card.get("_origLane"))
        cid = str(card.get("id") or "")
        if not cid or lane is None or orig is None:
            continue
        if lane == orig:
            continue  # unchanged — no command
        if lane == "now":
            lines.append(f"bd update {cid} --claim")
        elif lane in ("next", "later"):
            lines.append(f"bd update {cid} --status open")
        elif lane == "cut":
            lines.append(f'bd close {cid} --reason="Triage cut"')
    return "\n".join(lines) + ("\n" if lines else "")


def _load_json(path: Path) -> list:
    with path.open() as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "runs" in data:
        return data["runs"]
    return data if isinstance(data, list) else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/board.json", help="output path")
    parser.add_argument("--max-cards", type=int, default=250)
    parser.add_argument("--last-run", default="data/last_run.json")
    parser.add_argument("--issues-cache", default="data/issues_cache.json")
    args = parser.parse_args(argv)

    last_path = Path(args.last_run)
    cache_path = Path(args.issues_cache)
    out_path = Path(args.out)

    if not last_path.exists():
        print(f"build_board_json: missing {last_path}", file=sys.stderr)
        return 2

    runs = _load_json(last_path)
    titles: dict[str, str] = {}
    queued: list[dict] = []
    if cache_path.exists():
        try:
            cached = _load_json(cache_path)
            titles = {
                str(x.get("issue_number")): str(x.get("title") or "")
                for x in cached
            }
            queued = [x for x in cached if str(x.get("state")) == "open"]
        except (json.JSONDecodeError, TypeError):
            titles = {}
            queued = []

    out = build_board(runs, titles, max_cards=args.max_cards, queued=queued)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"build_board_json: wrote {len(out['board'])} cards -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())