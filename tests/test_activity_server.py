"""Tests for activity_server.py board endpoints (Task 3 of Durable Board plan).

Run: python -m pytest tests/test_activity_server.py -v
"""

import json
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

import pytest

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
