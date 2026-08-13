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
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Add parent to path so we can import school-core modules
sys.path.insert(0, str(Path(__file__).parent))

ACTIVITY_LOG_PATH = Path(__file__).parent / "data" / "activity_log.json"
SCORES_PATH = Path(__file__).parent / "data" / "scores.json"
DASHBOARD_PATH = Path(__file__).parent / "docs" / "site" / "live_activity_dashboard.html"
TRAJECTORY_DIR = Path(__file__).parent / "data" / "trajectories"

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

    def handle(self) -> None:
        # Silence client disconnects (SSE streams + Cloudflare edge re-requests
        # drop connections mid-stream). A reset peer is normal, not an error.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

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
        elif path == "/stream":
            self._serve_stream()
        elif path.startswith("/trajectory/"):
            self._serve_trajectory(path[len("/trajectory/"):])
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
        try:
            payload = self._build_board_json_payload()
        except Exception as e:
            self._json_response({"error": f"Failed to load board data: {e}"}, 500)
            return
        self._json_response(payload)

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

    def _build_board_json_payload(self) -> dict:
        """Build the board JSON payload (same shape as ``/api/board.json``).

        Returns ``{"columns": {"todo": […], "in_progress": […], …}}`` with
        the same eight lifecycle keys as ``board._COLUMN_META``.
        Safe to call from the SSE streaming loop — never sends HTTP headers.
        """
        from board import assign_column, _build_last_run_map

        try:
            issues_cache, processed, last_run = _load_board_data()
        except Exception:
            return {
                "columns": {
                    "todo": [],
                    "in_progress": [],
                    "in_review": [],
                    "retry": [],
                    "blocked": [],
                    "crew_in_flight": [],
                    "school_failed": [],
                    "done": [],
                }
            }

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

        return {"columns": columns}

    def _serve_stream(self):
        """GET /stream — SSE live-update endpoint.

        Sends an initial ``event: board`` with the current column-grouped board
        JSON, then polls ``data/`` files every ~2 seconds.  When any watched
        file changes (mtime or size) a fresh ``event: board`` is emitted.  If
        ``activity_log.json`` has grown since the last check, an additional
        ``event: activity`` is emitted with the new entries.

        Gracefully handles missing/corrupt data files via
        :meth:`_build_board_json_payload`.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        watch_files: list[Path] = [
            BOARD_CACHE_PATH,
            BOARD_PROCESSED_PATH,
            BOARD_LAST_RUN_PATH,
            ACTIVITY_LOG_PATH,
        ]

        def _snapshot() -> dict[str, tuple]:
            """Return ``{str(path): (mtime, size)}`` for every watch file."""
            snap: dict[str, tuple] = {}
            for f in watch_files:
                try:
                    s = f.stat()
                    snap[str(f)] = (s.st_mtime, s.st_size)
                except OSError:
                    snap[str(f)] = None
            return snap

        def _emit_board() -> None:
            data = json.dumps(
                self._build_board_json_payload(), ensure_ascii=False
            )
            self.wfile.write(f"event: board\ndata: {data}\n\n".encode())
            self.wfile.flush()

        # ── Track activity-log size ──────────────────────────────────────
        prev_activity_count = 0
        if ACTIVITY_LOG_PATH.exists():
            try:
                prev_activity_count = len(
                    json.loads(ACTIVITY_LOG_PATH.read_text()).get("entries", [])
                )
            except (json.JSONDecodeError, OSError):
                pass

        prev_snap = _snapshot()

        # ── Initial push ─────────────────────────────────────────────────
        _emit_board()

        try:
            while True:
                time.sleep(2)

                new_snap = _snapshot()
                if new_snap == prev_snap:
                    continue
                prev_snap = new_snap

                # At least one watched file changed → re-emit board
                _emit_board()

                # Check for new activity entries since last emit
                new_count = 0
                if ACTIVITY_LOG_PATH.exists():
                    try:
                        new_count = len(
                            json.loads(ACTIVITY_LOG_PATH.read_text()).get(
                                "entries", []
                            )
                        )
                    except (json.JSONDecodeError, OSError):
                        pass

                if new_count > prev_activity_count:
                    try:
                        all_entries: list[dict] = json.loads(
                            ACTIVITY_LOG_PATH.read_text()
                        ).get("entries", [])
                        new_entries = all_entries[prev_activity_count:]
                        if new_entries:
                            payload = json.dumps(
                                new_entries, ensure_ascii=False
                            )
                            self.wfile.write(
                                f"event: activity\ndata: {payload}\n\n".encode()
                            )
                            self.wfile.flush()
                    except (json.JSONDecodeError, OSError):
                        pass
                    prev_activity_count = new_count
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected — stop the stream quietly
            pass

    def _serve_trajectory(self, traj_id: str) -> None:
        """GET /trajectory/<id> — serve a run's trajectory JSON (text/plain).

        ``traj_id`` is a bare filename (e.g. ``20260612_001113_x--_default--m.json``).
        Rejects path traversal / non-filename characters with 400, missing files
        with 404, and pretty-prints the JSON as text/plain on 200.
        """
        import re

        if not traj_id or not re.fullmatch(r"[A-Za-z0-9_.\-]+", traj_id):
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Invalid trajectory id")
            return

        traj_path = TRAJECTORY_DIR / traj_id
        if not traj_path.exists():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Trajectory not found")
            return

        try:
            data = json.loads(traj_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Failed to read trajectory: {e}".encode())
            return

        body = json.dumps(data, indent=2, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
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

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ActivityHandler)
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
