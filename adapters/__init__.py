from __future__ import annotations
"""Adapters package — uniform interfaces for ecosystem couplings.


This package provides abstract base classes and concrete implementations
for coupling to external systems:

- HermesAdapter: Run Hermes agent tasks (student/teacher execution)
- OrcaAdapter: Manage Orca worktrees and terminals (runtime)
- OmniRouteAdapter: Route model calls to LLM backends (substrate)
- AgentMailAdapter: Send/receive email notifications (control plane)
- GitHubAdapter: Fetch issues and interact with GitHub (issue source)

Usage:
    from adapters import HermesAdapter, OrcaAdapter
    from adapters.hermes_orca import OrcaHermesAdapter
    from adapters.orca_cli import OrcaCLIAdapter

    hermes = OrcaHermesAdapter()
    orca = OrcaCLIAdapter()

    # Use the adapters through their abstract interfaces
    worktree = orca.create_worktree("study-coder-r1")
    response = hermes.run_task(worktree, "bead-123", "Write a function")
"""

from adapters.base import (
    AdapterError,
    AgentMailAdapter,
    AgentMailError,
    GitHubAdapter,
    GitHubError,
    HermesAdapter,
    HermesUnavailableError,
    ModelError,
    OmniRouteAdapter,
    OrcaAdapter,
    OrcaUnavailableError,
)

__all__ = [
    # Base interfaces
    "AdapterError",
    "AgentMailAdapter",
    "AgentMailError",
    "GitHubAdapter",
    "GitHubError",
    "HermesAdapter",
    "HermesUnavailableError",
    "ModelError",
    "OmniRouteAdapter",
    "OrcaAdapter",
    "OrcaUnavailableError",
    # Concrete implementations
    "OrcaCLIAdapter",
    "OrcaHTTPAdapter",
    "OrcaHermesAdapter",
    "OmniRouteClientAdapter",
    "NousDirectAdapter",
    "AgentMailClientAdapter",
    "GitHubCLIAdapter",
    # Factory
    "AdapterRegistry",
    "get_default_registry",
]

from adapters.orca_cli import OrcaCLIAdapter
from adapters.orca_http_adapter import OrcaHTTPAdapter
from adapters.hermes_orca import OrcaHermesAdapter
from adapters.omniroute_client import OmniRouteClientAdapter, NousDirectAdapter
from adapters.agentmail_client import AgentMailClientAdapter
from adapters.github_cli import GitHubCLIAdapter


class AdapterRegistry:
    """Registry for adapter instances.

    Provides a central place to configure and access adapter instances.
    Useful for dependency injection and testing (swap adapters via the registry).

    Usage:
        registry = AdapterRegistry()
        registry.register_orca(OrcaCLIAdapter())
        registry.register_hermes(OrcaHermesAdapter())

        orca = registry.get_orca()
        hermes = registry.get_hermes()
    """

    def __init__(self):
        self._orca: OrcaAdapter | None = None
        self._hermes: HermesAdapter | None = None
        self._omniroute: OmniRouteAdapter | None = None
        self._agentmail: AgentMailAdapter | None = None
        self._github: GitHubAdapter | None = None

    def register_orca(self, adapter: OrcaAdapter) -> None:
        """Register an Orca adapter."""
        self._orca = adapter

    def register_hermes(self, adapter: HermesAdapter) -> None:
        """Register a Hermes adapter."""
        self._hermes = adapter

    def register_omniroute(self, adapter: OmniRouteAdapter) -> None:
        """Register an OmniRoute adapter."""
        self._omniroute = adapter

    def register_agentmail(self, adapter: AgentMailAdapter) -> None:
        """Register an AgentMail adapter."""
        self._agentmail = adapter

    def register_github(self, adapter: GitHubAdapter) -> None:
        """Register a GitHub adapter."""
        self._github = adapter

    def get_orca(self) -> OrcaAdapter:
        """Get the registered Orca adapter, or create a default one."""
        if self._orca is None:
            self._orca = OrcaCLIAdapter()
        return self._orca

    def get_hermes(self) -> HermesAdapter:
        """Get the registered Hermes adapter, or create a default one."""
        if self._hermes is None:
            self._hermes = OrcaHermesAdapter()
        return self._hermes

    def get_omniroute(self) -> OmniRouteAdapter:
        """Get the registered OmniRoute adapter, or create a default one."""
        if self._omniroute is None:
            import os
            provider = os.environ.get("MODEL_PROVIDER", "omniroute").strip()
            if provider == "nous":
                self._omniroute = NousDirectAdapter()
            else:
                self._omniroute = OmniRouteClientAdapter()
        return self._omniroute

    def get_agentmail(self) -> AgentMailAdapter:
        """Get the registered AgentMail adapter, or create a default one."""
        if self._agentmail is None:
            self._agentmail = AgentMailClientAdapter()
        return self._agentmail

    def get_github(self) -> GitHubAdapter:
        """Get the registered GitHub adapter, or create a default one."""
        if self._github is None:
            self._github = GitHubCLIAdapter()
        return self._github


# Default global registry
_default_registry: AdapterRegistry | None = None


def get_default_registry() -> AdapterRegistry:
    """Get the default global adapter registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = AdapterRegistry()
    return _default_registry
