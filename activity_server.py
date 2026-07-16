#!/usr/bin/env python3
"""
activity_server.py — Serve the activity log as JSON for the live dashboard.

Serves:
  GET /api/activity         — full activity log (all entries)
  GET /api/activity/recent  — last N entries (?n=50)
  GET /api/activity/since   — entries since ISO timestamp (?ts=2026-06-13T...)
  GET /api/agents           — current state of all agents
  GET /                     — the live dashboard HTML

Usage:
  python activity_server.py              # serve on port 8765
  python activity_server.py --port 9000  # custom port
"""

import argparse
import json
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Add parent to path so we can import school-core modules
sys.path.insert(0, str(Path(__file__).parent))

ACTIVITY_LOG_PATH = Path(__file__).parent / "data" / "activity_log.json"
SCORES_PATH = Path(__file__).parent / "data" / "scores.json"
DASHBOARD_PATH = Path(__file__).parent / "docs" / "site" / "live_activity_dashboard.html"

# Board data paths (network-free — local files only)
BOARD_PROCESSED_PATH = Path(__file__).parent / "data" / "processed_issues.json"
BOARD_LAST_RUN_PATH = Path(__file__).parent / "data" / "last_run.json"
BOARD_CACHE_PATH = Path(__file__).parent / "data" / "issues_cache.json"


def _load_board_data() -> tuple[list[dict], list[int], list[dict]]:
    """Load board data from local JSON files.

    Returns (issues_cache, processed, last_run) triple.
    All three sources are network-free — reads ``data/`` files only.
    Missing or corrupt files are handled gracefully (empty defaults).
    """
    issues_cache: list[dict] = []
    if BOARD_CACHE_PATH.exists():
        try:
            issues_cache = json.loads(BOARD_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            issues_cache = []

    processed: list[int] = []
    if BOARD_PROCESSED_PATH.exists():
        try:
            processed = json.loads(BOARD_PROCESSED_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            processed = []

    last_run: list[dict] = []
    if BOARD_LAST_RUN_PATH.exists():
        try:
            last_run = json.loads(BOARD_LAST_RUN_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            last_run = []

    return issues_cache, processed, last_run


class ActivityHandler(SimpleHTTPRequestHandler):
    """Serve activity log JSON and the dashboard."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        if path == "/api/activity" or path == "/api/activity/recent":
            self._serve_activity(qs)
        elif path == "/api/activity/since":
            self._serve_activity_since(qs)
        elif path == "/api/agents":
            self._serve_agents()
        elif path == "/health":
            self._serve_health()
        elif path == "/api/board.json":
            self._serve_board_json()
        elif path == "/board":
            self._serve_board()
        else:
            self._serve_dashboard()

    def _serve_activity(self, qs):
        n = int(qs.get("n", [50])[0])
        try:
            if ACTIVITY_LOG_PATH.exists():
                data = json.loads(ACTIVITY_LOG_PATH.read_text())
                entries = data.get("entries", [])
                if n:
                    entries = entries[-n:]
                self._json_response({"entries": entries, "total": len(data.get("entries", []))})
            else:
                self._json_response({"entries": [], "total": 0})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_activity_since(self, qs):
        ts = qs.get("ts", [""])[0]
        try:
            if ACTIVITY_LOG_PATH.exists():
                data = json.loads(ACTIVITY_LOG_PATH.read_text())
                entries = [e for e in data.get("entries", []) if e.get("timestamp", "") > ts]
                self._json_response({"entries": entries})
            else:
                self._json_response({"entries": []})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_agents(self):
        try:
            if SCORES_PATH.exists():
                scores = json.loads(SCORES_PATH.read_text())
            else:
                scores = {}
            # Also read latest activity per agent
            latest_activity = {}
            if ACTIVITY_LOG_PATH.exists():
                act_data = json.loads(ACTIVITY_LOG_PATH.read_text())
                for e in act_data.get("entries", []):
                    a = e.get("agent", "")
                    if a and not a.startswith("staff:"):
                        latest_activity[a] = e
            agents = []
            for agent, domains in scores.items():
                if agent.startswith("ses_"):
                    continue
                max_score = max(domains.values()) if domains else 0
                gate = "easy"
                for gname, gthr in sorted({"easy": 0, "medium": 25, "hard": 50, "diploma": 75}.items(), key=lambda x: x[1]):
                    if max_score >= gthr:
                        gate = gname
                role = "Student"
                if max_score >= 75:
                    role = "Faculty"
                elif max_score >= 50:
                    role = "Teacher"
                elif max_score >= 25:
                    role = "Senior Student"
                act = latest_activity.get(agent, {})
                agents.append({
                    "id": agent,
                    "name": agent,
                    "score": domains.get("_default", 0),
                    "max_score": max_score,
                    "gate": gate,
                    "role": role,
                    "domains": {k: v for k, v in domains.items() if k != "_default"},
                    "latest_activity": act,
                })
            agents.sort(key=lambda a: a["score"], reverse=True)
            self._json_response({"agents": agents})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_health(self):
        self._json_response({"status": "ok"})

    def _serve_board_json(self):
        """GET /api/board.json — column-grouped board data (network-free)."""
        from board import assign_column, _build_last_run_map

        try:
            issues_cache, processed, last_run = _load_board_data()
        except Exception as e:
            self._json_response({"error": f"Failed to load board data: {e}"}, 500)
            return

        processed_set: set[int] = set(processed)
        lr_map = _build_last_run_map(last_run)

        columns: dict[str, list[dict]] = {
            "todo": [],
            "in_progress": [],
            "in_review": [],
            "done": [],
        }

        for issue in issues_cache:
            col = assign_column(issue, processed_set, lr_map)
            lr_entry = lr_map.get(issue["issue_number"])
            columns[col].append({
                "n": issue["issue_number"],
                "t": issue.get("title", ""),
                "dom": issue.get("domain", ""),
                "diff": issue.get("difficulty", ""),
                "a": str(lr_entry.get("agent", "")) if lr_entry else "",
                "s": lr_entry.get("score") if lr_entry else None,
            })

        self._json_response({"columns": columns})

    def _serve_board(self):
        """GET /board — rendered kanban board HTML."""
        from board import build_board_html

        try:
            issues_cache, processed, last_run = _load_board_data()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Failed to load board data: {e}".encode())
            return

        html = build_board_html(issues_cache, processed, last_run)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self):
        if DASHBOARD_PATH.exists():
            html = DASHBOARD_PATH.read_text()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Dashboard not found. Generate it first.")

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress default logging for cleanliness
        pass


def main():
    parser = argparse.ArgumentParser(description="Serve activity log for live dashboard")
    parser.add_argument("--port", type=int, default=8765, help="Port to serve on")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), ActivityHandler)
    print(f"Activity server running at http://127.0.0.1:{args.port}")
    print(f"  Dashboard:     http://127.0.0.1:{args.port}/")
    print(f"  Activity API:  http://127.0.0.1:{args.port}/api/activity")
    print(f"  Agents API:    http://127.0.0.1:{args.port}/api/agents")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
