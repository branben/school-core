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
Reads:
  - .beads/ via ``bd ready --json`` (fallback: .beads/interactions.jsonl)
  - data/last_run.json  (pipeline truth: status per issue)
  - data/scores.json    (score store)

Writes:
  - data/board.json     { "lanes": { "now": [], "next": [], "later": [], "cut": [] } }

Lane semantics (from docs/templates/slice-tracking-contract.md):
  now   -> in_progress, crew_in_flight
  next  -> in_review, retry, blocked
  later -> open, unprocessed (default: bead exists but no last_run entry)
  cut   -> success, done, error, school-failed

Every card keeps its original status as the reason, so "cut" never loses why.
"""

from __future__ import annotations

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
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
LAST_RUN_FILE = DATA_DIR / "last_run.json"
BOARD_FILE = DATA_DIR / "board.json"
INTERACTIONS_FILE = REPO / ".beads" / "interactions.jsonl"

# Lane mapping (from slice-tracking-contract.md)
LANE_STATUS_MAP = {
    "now": {"in_progress", "crew_in_flight"},
    "next": {"in_review", "retry", "blocked"},
    "cut": {"success", "done", "error", "school-failed"},
}


def load_last_run() -> list[dict]:
    """Load last_run.json; return [] on missing/garbage."""
    try:
        data = json.loads(LAST_RUN_FILE.read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def fetch_ready_beads() -> list[dict]:
    """Fetch ready beads via `bd ready --json`; fallback to interactions.jsonl."""
    try:
        result = subprocess.run(
            ["bd", "ready", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO),
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if isinstance(data, list):
                return data
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass

    # Fallback: read passive export
    beads: list[dict] = []
    try:
        for line in INTERACTIONS_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "issue_open":
                    beads.append({
                        "id": str(entry.get("id", "")),
                        "title": str(entry.get("title", "Untitled")),
                        "issue_type": str(entry.get("issue_type", "")),
                        "status": "open",
                    })
            except (json.JSONDecodeError, OSError):
                continue
    except (FileNotFoundError, OSError):
        pass
    return beads


def _safe_str(val: object) -> str:
    """Coerce a value to str, handling None."""
    return str(val) if val is not None else ""


def _safe_domain(entry: dict) -> str:
    """Safely extract capability.domain from a last_run entry."""
    cap = entry.get("capability")
    if not isinstance(cap, dict):
        return ""
    return _safe_str(cap.get("domain", ""))


def _extract_score(entry: dict | None) -> float | None:
    """Extract score from a last_run entry (None-safe)."""
    if not isinstance(entry, dict):
        return None
    score = entry.get("score")
    if isinstance(score, (int, float)):
        return float(score)
    te = entry.get("teacher_evidence")
    if isinstance(te, dict):
        combined = te.get("combined_score")
        if isinstance(combined, (int, float)):
            return float(combined)
    return None


def _extract_title(entry: dict) -> str:
    """Best-effort title from a last_run entry."""
    domain = _safe_domain(entry)
    issue_num = entry.get("issue", "?")
    if domain:
        return f"Issue #{_safe_str(issue_num)} ({domain})"
    return f"Issue #{_safe_str(issue_num)}"


def _extract_issue_num(bead: dict) -> int | None:
    """Extract issue number from a bead dict."""
    for key in ("issue", "issue_number", "number"):
        val = bead.get(key)
        if isinstance(val, int):
            return val
    bead_id = bead.get("id", "")
    if isinstance(bead_id, str):
        parts = bead_id.rsplit("-", 1)
        if parts[-1].isdigit():
            return int(parts[-1])
    return None


def _lane_for_status(status: str) -> str:
    """Map a status string to a lane name (default: later)."""
    for lane, statuses in LANE_STATUS_MAP.items():
        if status in statuses:
            return lane
    return "later"


def build_board(beads: list[dict], last_run: list[dict]) -> dict:
    """Map beads + last_run scores into lanes."""
    # Index last_run by issue number, keeping the most recent entry per issue
    last_run_by_issue: dict[int, dict] = {}
    for entry in last_run:
        if not isinstance(entry, dict):
            continue
        issue_num = entry.get("issue")
        if not isinstance(issue_num, int):
            continue
        existing = last_run_by_issue.get(issue_num)
        if existing is None or _safe_str(entry.get("timestamp", "")) > _safe_str(existing.get("timestamp", "")):
            last_run_by_issue[issue_num] = entry

    lanes: dict[str, list[dict]] = {
        "now": [],
        "next": [],
        "later": [],
        "cut": [],
    }
    seen_issues: set[int] = set()

    for bead in beads:
        if not isinstance(bead, dict):
            continue
        issue_num = _extract_issue_num(bead)
        if issue_num is not None:
            seen_issues.add(issue_num)

        run_entry = last_run_by_issue.get(issue_num) if issue_num is not None else None
        status = _safe_str(run_entry.get("status", "open")) if isinstance(run_entry, dict) else "open"
        lane = _lane_for_status(status)

        card: dict = {
            "id": _safe_str(bead.get("id", "")),
            "title": _safe_str(bead.get("title", "Untitled")),
            "issue_type": _safe_str(bead.get("issue_type", "")),
            "status": status,
            "score": _extract_score(run_entry),
            "agent": run_entry.get("agent") if isinstance(run_entry, dict) else None,
            "timestamp": run_entry.get("timestamp") if isinstance(run_entry, dict) else None,
            "retry_attempt": run_entry.get("retry_attempt", 0) if isinstance(run_entry, dict) else 0,
        }
        lanes[lane].append(card)

    # Include beads that have last_run entries but aren't in `bd ready`
    for issue_num, entry in last_run_by_issue.items():
        if issue_num in seen_issues:
            continue
        if not isinstance(entry, dict):
            continue
        status = _safe_str(entry.get("status", "school-failed"))
        lane = _lane_for_status(status)
        card = {
            "id": f"issue-{issue_num}",
            "title": _extract_title(entry),
            "issue_type": _safe_domain(entry),
            "status": status,
            "score": _extract_score(entry),
            "agent": entry.get("agent"),
            "timestamp": entry.get("timestamp"),
            "retry_attempt": entry.get("retry_attempt", 0),
        }
        lanes[lane].append(card)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes": lanes,
    }


def main() -> None:
    last_run = load_last_run()
    beads = fetch_ready_beads()
    board = build_board(beads, last_run)

    # Atomic write
    BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOARD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(board, indent=2))
    tmp.replace(BOARD_FILE)

    lane_counts = {lane: len(cards) for lane, cards in board["lanes"].items()}
    total = sum(lane_counts.values())
    print(
        f"Board rebuilt: {total} cards — "
        f"now={lane_counts['now']} next={lane_counts['next']} "
        f"later={lane_counts['later']} cut={lane_counts['cut']}"
    )


if __name__ == "__main__":
    main()
