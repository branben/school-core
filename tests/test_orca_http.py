"""Test: Orca HTTP client routes to FastAPI daemon."""
import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestOrcaHTTPClient:
    """OrcaHTTPClient should hit the FastAPI daemon, not shell out."""

    @pytest.fixture
    def client(self):
        from orca_http import OrcaHTTPClient
        with patch("orca_http.httpx.Client") as MockClient:
            instance = MagicMock()
            MockClient.return_value = instance
            client = OrcaHTTPClient(base_url="http://test:9100", token="tok")
            client._mock_instance = instance
            yield client

    def _mock_response(self, json_data, status_code=200):
        resp = MagicMock()
        resp.json.return_value = json_data
        resp.status_code = status_code
        resp.text = json.dumps(json_data)
        return resp

    def test_create_worktree(self, client):
        client._mock_instance.post.return_value = self._mock_response(
            {"path": "/tmp/wt", "name": "study-coder-1"}
        )

        result = client.create_worktree("study-coder-1")

        client._mock_instance.post.assert_called_once_with(
            "/worktree/create",
            json={"name": "study-coder-1", "repo_path": None},
        )
        assert result == {"path": "/tmp/wt", "name": "study-coder-1"}

    def test_remove_worktree(self, client):
        client._mock_instance.post.return_value = self._mock_response({"ok": True})

        client.remove_worktree("/tmp/wt")

        client._mock_instance.post.assert_called_once_with(
            "/worktree/rm",
            json={"path": "/tmp/wt"},
        )

    def test_list_worktrees(self, client):
        client._mock_instance.get.return_value = self._mock_response(
            [{"path": "/tmp/wt", "name": "s-c-1"}]
        )

        result = client.list_worktrees()

        client._mock_instance.get.assert_called_once_with("/worktree/list")
        assert len(result) == 1

    def test_prune_worktrees(self, client):
        client._mock_instance.post.return_value = self._mock_response(
            {"ok": True, "pruned": 3}
        )

        client.prune_worktrees()

        client._mock_instance.post.assert_called_once_with("/worktree/prune", json={})

    def test_list_repos(self, client):
        client._mock_instance.get.return_value = self._mock_response(
            [{"path": "/repo", "name": "school-core"}]
        )

        result = client.list_repos()

        client._mock_instance.get.assert_called_once_with("/repo/list")

    def test_add_repo(self, client):
        client._mock_instance.post.return_value = self._mock_response(
            {"ok": True, "name": "school-core"}
        )

        client.add_repo("/Users/me/school-core")

        client._mock_instance.post.assert_called_once_with(
            "/repo/add",
            json={"path": "/Users/me/school-core"},
        )

    def test_send_terminal(self, client):
        client._mock_instance.post.return_value = self._mock_response({"ok": True})

        client.send_terminal("term-1", "git status")

        client._mock_instance.post.assert_called_once_with(
            "/terminal/send",
            json={"handle": "term-1", "text": "git status", "enter": True},
        )

    def test_close_terminal(self, client):
        client._mock_instance.post.return_value = self._mock_response({"ok": True})

        client.close_terminal("term-1")

        client._mock_instance.post.assert_called_once_with(
            "/terminal/close",
            json={"handle": "term-1"},
        )

    def test_status(self, client):
        client._mock_instance.get.return_value = self._mock_response(
            {"ok": True, "runtime": {"state": "running"}}
        )

        result = client.status()

        client._mock_instance.get.assert_called_once_with("/status")
        assert result["ok"] is True

    def test_health_returns_true_when_ready(self, client):
        client._mock_instance.get.return_value = self._mock_response({"ok": True})
        assert client.health() is True

    def test_health_returns_false_when_not_ready(self, client):
        client._mock_instance.get.side_effect = Exception("connection refused")
        assert client.health() is False

    def test_error_propagates(self, client):
        from orca_http import OrcaHTTPError
        import httpx
        client._mock_instance.post.side_effect = httpx.RequestError("connection refused")

        with pytest.raises(OrcaHTTPError, match="connection refused"):
            client.create_worktree("test")
