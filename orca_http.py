"""HTTP client for Orca shim — replaces subprocess.run(['orca', ...])."""
import os
from typing import Optional

import httpx

ORCA_HTTP = os.environ.get("ORCA_HTTP", "http://localhost:9100")


class OrcaHTTPError(Exception):
    """Raised when the Orca shim returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


class OrcaHTTPClient:
    """HTTP client that talks to the Orca shim daemon on the host."""

    def __init__(self, base_url: str = ORCA_HTTP, token: str = None, timeout: float = 30.0):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

    def _request(self, method: str, path: str, json_body: dict = None) -> dict:
        try:
            if method == "GET":
                resp = self.client.get(path)
            else:
                resp = self.client.post(path, json=json_body or {})
            if resp.status_code >= 400:
                raise OrcaHTTPError(
                    f"HTTP {resp.status_code}: {resp.text[:300]}",
                    status_code=resp.status_code,
                )
            return resp.json()
        except httpx.RequestError as e:
            raise OrcaHTTPError(f"Connection failed: {e}") from e

    def create_worktree(self, name: str, repo_path: str = None) -> dict:
        return self._request("POST", "/worktree/create", {"name": name, "repo_path": repo_path})

    def remove_worktree(self, path: str) -> dict:
        return self._request("POST", "/worktree/rm", {"path": path})

    def list_worktrees(self) -> list:
        return self._request("GET", "/worktree/list")

    def prune_worktrees(self) -> dict:
        return self._request("POST", "/worktree/prune", {})

    def list_repos(self) -> list:
        return self._request("GET", "/repo/list")

    def add_repo(self, path: str) -> dict:
        return self._request("POST", "/repo/add", {"path": path})

    def send_terminal(self, handle: str, text: str, enter: bool = True) -> dict:
        return self._request("POST", "/terminal/send", {"handle": handle, "text": text, "enter": enter})

    def close_terminal(self, handle: str) -> dict:
        return self._request("POST", "/terminal/close", {"handle": handle})

    def status(self) -> dict:
        return self._request("GET", "/status")

    def health(self) -> bool:
        try:
            result = self.status()
            return result.get("ok", False)
        except Exception:
            return False
