"""Test: Orca host daemon (FastAPI) endpoints."""
import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestOrcaShim:
    """FastAPI daemon should wrap Orca CLI and return JSON."""

    @pytest.fixture
    def app(self):
        from scripts.orca_shim import app
        return app

    @pytest.fixture
    def client(self, app):
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_readyz_ok(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"runtime": {"state": "running"}}
            resp = client.get("/readyz")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}

    def test_readyz_not_running(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.side_effect = Exception("Orca not found")
            resp = client.get("/readyz")
            assert resp.status_code == 503

    def test_worktree_create(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"path": "/tmp/wt", "name": "study-coder-1"}
            resp = client.post("/worktree/create", json={"name": "study-coder-1"})
            assert resp.status_code == 200
            assert resp.json()["name"] == "study-coder-1"
            mock_run.assert_called_once_with(["worktree", "create", "--name", "study-coder-1"])

    def test_worktree_rm(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"ok": True}
            resp = client.post("/worktree/rm", json={"path": "/tmp/wt"})
            assert resp.status_code == 200
            mock_run.assert_called_once_with(["worktree", "rm", "--worktree", "path:/tmp/wt", "--force"])

    def test_worktree_list(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = [{"path": "/tmp/wt", "name": "s-c-1"}]
            resp = client.get("/worktree/list")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

    def test_worktree_prune(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"ok": True}
            resp = client.post("/worktree/prune", json={})
            assert resp.status_code == 200
            mock_run.assert_called_once_with(["worktree", "prune"])

    def test_repo_list(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = [{"path": "/repo", "name": "school-core"}]
            resp = client.get("/repo/list")
            assert resp.status_code == 200

    def test_repo_add(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"ok": True}
            resp = client.post("/repo/add", json={"path": "/Users/me/school-core"})
            assert resp.status_code == 200
            mock_run.assert_called_once_with(["repo", "add", "--path", "/Users/me/school-core"])

    def test_terminal_send(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"ok": True}
            resp = client.post("/terminal/send", json={"handle": "term-1", "text": "git status"})
            assert resp.status_code == 200
            mock_run.assert_called_once_with(["terminal", "send", "--terminal", "term-1", "git status"])

    def test_terminal_close(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"ok": True}
            resp = client.post("/terminal/close", json={"handle": "term-1"})
            assert resp.status_code == 200
            mock_run.assert_called_once_with(["terminal", "close", "--terminal", "term-1"])

    def test_status(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            mock_run.return_value = {"runtime": {"state": "running"}}
            resp = client.get("/status")
            assert resp.status_code == 200
            assert resp.json()["runtime"]["state"] == "running"

    def test_orca_failure_returns_503(self, client):
        with patch("scripts.orca_shim.run_orca") as mock_run:
            from fastapi import HTTPException
            mock_run.side_effect = HTTPException(503, "Orca crashed")
            resp = client.post("/worktree/create", json={"name": "test"})
            assert resp.status_code == 503
