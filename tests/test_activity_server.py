"""Tests for activity_server.py board endpoints (Task 3 of Durable Board plan).

Run: python -m pytest tests/test_activity_server.py -v
"""

import json
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

import pytest

import activity_server
from activity_log import ActivityLog
from activity_server import ActivityHandler


@pytest.fixture(scope="module")
def server_url():
    """Start activity server on a random port, yield the base URL, clean up."""
    server = HTTPServer(("127.0.0.1", 0), ActivityHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


class TestBoardApi:
    """Tests for the /api/board.json endpoint."""

    def test_get_board_json_returns_200_with_columns(self, server_url):
        """GET /api/board.json returns 200, Content-Type application/json,
        and JSON body with key 'columns' containing columns todo/in_progress/
        in_review/done (each a list)."""
        resp = urllib.request.urlopen(f"{server_url}/api/board.json")
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"
        data = json.loads(resp.read().decode())
        assert "columns" in data
        cols = data["columns"]
        for key in (
            "todo", "in_progress", "in_review", "retry",
            "blocked", "crew_in_flight", "school_failed", "done",
        ):
            assert key in cols, f"Expected key '{key}' in columns"
            assert isinstance(cols[key], list), f"Column '{key}' should be a list"

    def test_get_board_returns_html_with_column_headers(self, server_url):
        """GET /board returns 200 HTML containing the 4 column headers."""
        resp = urllib.request.urlopen(f"{server_url}/board")
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/html" in ct, f"Expected text/html, got {ct}"
        html = resp.read().decode()
        assert "To Do" in html
        assert "In Progress" in html
        assert "In Review" in html
        assert "Done" in html

    def test_get_timeline_exposes_structured_deviation_fields(
        self, server_url, tmp_path, monkeypatch
    ):
        log_path = tmp_path / "activity_log.json"
        log = ActivityLog(log_path)
        log.start_task("legacy-agent", "python-testing", "easy")
        log.record_deviation(
            issue="school-core-s6k",
            agent="parallel-agent",
            summary="The plan assumed a shared cap",
            plan_expected="One global count cap",
            code_revealed="Production disables the shared cap",
            decision="Keep the local byte-budget lease",
            revisit="Revisit if admission is re-enabled",
            evidence=["crew_dispatch.py:123"],
        )
        monkeypatch.setattr(activity_server, "ACTIVITY_LOG_PATH", log_path)

        resp = urllib.request.urlopen(
            f"{server_url}/api/timeline?issue=school-core-s6k&kind=deviation"
        )
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["total"] == 1
        assert data["events"][0]["kind"] == "deviation"
        assert data["events"][0]["plan_expected"] == "One global count cap"
        assert data["events"][0]["code_revealed"] == "Production disables the shared cap"
        assert data["events"][0]["decision"] == "Keep the local byte-budget lease"
        assert data["events"][0]["revisit"] == "Revisit if admission is re-enabled"

        activity_response = urllib.request.urlopen(f"{server_url}/api/activity")
        activity_data = json.loads(activity_response.read().decode())
        assert activity_data["entries"][-1]["type"] == "deviation"

    def test_timeline_missing_or_corrupt_log_is_empty_success(
        self, server_url, tmp_path, monkeypatch
    ):
        log_path = tmp_path / "missing.json"
        monkeypatch.setattr(activity_server, "ACTIVITY_LOG_PATH", log_path)
        response = urllib.request.urlopen(f"{server_url}/api/timeline")
        assert json.loads(response.read().decode()) == {
            "events": [],
            "total": 0,
            "filters": {"issue": "", "kind": "", "n": 50},
        }

        log_path.write_text("not json")
        response = urllib.request.urlopen(f"{server_url}/api/timeline?n=bad")
        assert json.loads(response.read().decode())["events"] == []
