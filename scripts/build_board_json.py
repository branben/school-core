#!/usr/bin/env python3
"""build_board_json.py — rebuild the triage board projection from durable state.

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
