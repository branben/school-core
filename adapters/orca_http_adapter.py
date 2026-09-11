"""Orca HTTP adapter — concrete implementation of OrcaAdapter via HTTP.

Wraps the OrcaHTTPClient (orca_http.py) which talks to the Orca shim daemon
on the host. Useful when the Orca CLI is not available but the shim is.
"""

from __future__ import annotations

from typing import Optional

from adapters.base import OrcaAdapter, OrcaUnavailableError


class OrcaHTTPAdapter(OrcaAdapter):
    """Concrete Orca adapter that talks to the Orca shim daemon via HTTP.

    This adapter is useful in environments where the Orca CLI is not
    available locally but the shim daemon is running (e.g., remote containers,
    CI environments).
    """

    def __init__(self, base_url: str = "http://localhost:9100", token: str = ""):
        self._base_url = base_url
        self._token = token or ""
        self._client = None

    def _get_client(self):
        """Get or create the HTTP client."""
        if self._client is None:
            try:
                from orca_http import OrcaHTTPClient
                self._client = OrcaHTTPClient(
                    base_url=self._base_url,
                    token=self._token or None,
                )
            except ImportError:
                raise OrcaUnavailableError(
                    "orca_http module not available — install the Orca shim"
                )
        return self._client

    def create_worktree(self, name: str, repo_path: Optional[str] = None) -> str:
        """Create a child worktree and return its absolute path."""
        client = self._get_client()
        try:
            result = client.create_worktree(name, repo_path)
            # The HTTP response may contain the path directly or in a nested structure
            if isinstance(result, dict):
                path = result.get("path", result.get("id", ""))
                if path and "::" in str(path):
                    return path.split("::", 1)[1]
                return path
            raise OrcaUnavailableError(f"Unexpected response: {result}")
        except Exception as e:
            if "OrcaUnavailableError" in str(type(e)):
                raise
            raise OrcaUnavailableError(f"Failed to create worktree: {e}") from e

    def close_worktree(self, path: str) -> bool:
        """Remove a worktree by path. Idempotent."""
        import os
        if not os.path.exists(path):
            return True
        client = self._get_client()
        try:
            client.remove_worktree(path)
            return not os.path.exists(path)
        except Exception:
            return False

    def create_terminal(self, title: str = "exec") -> str:
        """Create a terminal and return its handle.

        Note: The Orca HTTP shim may not support terminal creation directly.
        This method raises OrcaUnavailableError if not supported.
        """
        raise OrcaUnavailableError(
            "Terminal creation not supported via HTTP adapter — use OrcaCLIAdapter"
        )

    def close_terminal(self, handle: str) -> None:
        """Close a terminal session. Best-effort."""
        client = self._get_client()
        try:
            client.close_terminal(handle)
        except Exception:
            pass

    def status(self) -> dict:
        """Get Orca runtime status."""
        client = self._get_client()
        try:
            return client.status()
        except Exception:
            return {"runtime": {"state": "unavailable"}}
