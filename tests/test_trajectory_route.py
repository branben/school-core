"""Tests for the /trajectory/<id> route (Task 4.5 of Durable Board plan).

Run: python -m pytest tests/test_trajectory_route.py -v
"""

import json
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer
from pathlib import Path

import pytest

from activity_server import ActivityHandler

TRAJECTORIES_DIR = Path(__file__).parent.parent / "data" / "trajectories"


@pytest.fixture
def server_url():
    """Start activity server on a random port, yield the base URL, clean up."""
    server = HTTPServer(("127.0.0.1", 0), ActivityHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def temp_traj_file():
    """Create a temporary trajectory file and clean up after the test."""
    traj_id = "test_traj_route_20260716.json"
    traj_path = TRAJECTORIES_DIR / traj_id
    traj_data = {
        "timestamp": "2026-07-16T12:00:00",
        "domain": "coding",
        "difficulty": "easy",
        "agent": "test-agent",
        "prompt": "test prompt",
        "response": "test response",
        "task_score": 85,
        "old_score": 70,
        "new_score": 85,
        "evaluation": {"score": 85},
        "error": None,
        "engram_obs_id": None,
    }
    TRAJECTORIES_DIR.mkdir(parents=True, exist_ok=True)
    traj_path.write_text(json.dumps(traj_data))
    yield traj_id
    if traj_path.exists():
        traj_path.unlink()


class TestTrajectoryRoute:
    """Tests for GET /trajectory/<id>."""

    def test_get_trajectory_returns_200_with_json(self, server_url, temp_traj_file):
        """GET /trajectory/<valid_id> returns 200 text/plain with JSON content."""
        url = f"{server_url}/trajectory/{temp_traj_file}"
        resp = urllib.request.urlopen(url)
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/plain" in ct
        body = resp.read().decode()
        # Should contain expected JSON keys
        assert '"timestamp"' in body
        assert '"agent"' in body
        assert '"response"' in body

    def test_get_trajectory_nonexistent_returns_404(self, server_url):
        """GET /trajectory/<nonexistent> returns 404."""
        url = f"{server_url}/trajectory/nonexistent_file_12345.json"
        try:
            urllib.request.urlopen(url)
            assert False, "Expected 404 error"
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_get_trajectory_path_traversal_returns_400(self, server_url):
        """GET /trajectory/<../../etc> returns 400."""
        url = f"{server_url}/trajectory/../../etc/passwd"
        try:
            urllib.request.urlopen(url)
            assert False, "Expected 400 error"
        except urllib.error.HTTPError as e:
            assert e.code == 400
