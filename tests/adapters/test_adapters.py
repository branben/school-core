"""Tests for the adapters package.

Tests verify that:
1. Abstract base classes define the expected interfaces
2. Concrete implementations properly implement the interfaces
3. The AdapterRegistry works correctly for dependency injection
4. Mock adapters can be used for testing
"""

import pytest
from unittest.mock import MagicMock, patch

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
from adapters import (
    AdapterRegistry,
    OrcaCLIAdapter,
    OrcaHTTPAdapter,
    OrcaHermesAdapter,
    OmniRouteClientAdapter,
    NousDirectAdapter,
    AgentMailClientAdapter,
    GitHubCLIAdapter,
    get_default_registry,
)


# ── Abstract Base Class Tests ────────────────────────────────────────────────


class TestAbstractBaseClasses:
    """Test that abstract base classes define the expected interfaces."""

    def test_orca_adapter_is_abstract(self):
        """OrcaAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            OrcaAdapter()

    def test_hermes_adapter_is_abstract(self):
        """HermesAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            HermesAdapter()

    def test_omniroute_adapter_is_abstract(self):
        """OmniRouteAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            OmniRouteAdapter()

    def test_agentmail_adapter_is_abstract(self):
        """AgentMailAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            AgentMailAdapter()

    def test_github_adapter_is_abstract(self):
        """GitHubAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            GitHubAdapter()


# ── Exception Hierarchy Tests ────────────────────────────────────────────────


class TestExceptionHierarchy:
    """Test that all adapter exceptions inherit from AdapterError."""

    def test_orca_unavailable_is_adapter_error(self):
        assert issubclass(OrcaUnavailableError, AdapterError)

    def test_hermes_unavailable_is_adapter_error(self):
        assert issubclass(HermesUnavailableError, AdapterError)

    def test_model_error_is_adapter_error(self):
        assert issubclass(ModelError, AdapterError)

    def test_agentmail_error_is_adapter_error(self):
        assert issubclass(AgentMailError, AdapterError)

    def test_github_error_is_adapter_error(self):
        assert issubclass(GitHubError, AdapterError)

    def test_adapter_error_is_exception(self):
        assert issubclass(AdapterError, Exception)


# ── OrcaCLIAdapter Tests ─────────────────────────────────────────────────────


class TestOrcaCLIAdapter:
    """Test the OrcaCLIAdapter concrete implementation."""

    def test_implements_orca_adapter(self):
        """OrcaCLIAdapter implements OrcaAdapter interface."""
        from adapters.orca_cli import OrcaCLIAdapter
        assert issubclass(OrcaCLIAdapter, OrcaAdapter)

    def test_status_returns_dict(self):
        """status() returns a dict even when Orca is unavailable."""
        adapter = OrcaCLIAdapter()
        # When orca CLI is not available, should return a dict with unavailable state
        result = adapter.status()
        assert isinstance(result, dict)

    def test_create_worktree_raises_on_missing_cli(self):
        """create_worktree raises OrcaUnavailableError when CLI is missing."""
        adapter = OrcaCLIAdapter()
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(OrcaUnavailableError):
                adapter.create_worktree("test-wt")

    def test_close_worktree_returns_true_for_nonexistent_path(self):
        """close_worktree returns True for a path that doesn't exist."""
        adapter = OrcaCLIAdapter()
        result = adapter.close_worktree("/nonexistent/path")
        assert result is True


# ── OrcaHTTPAdapter Tests ────────────────────────────────────────────────────


class TestOrcaHTTPAdapter:
    """Test the OrcaHTTPAdapter concrete implementation."""

    def test_implements_orca_adapter(self):
        """OrcaHTTPAdapter implements OrcaAdapter interface."""
        assert issubclass(OrcaHTTPAdapter, OrcaAdapter)

    def test_status_returns_dict(self):
        """status() returns a dict even when Orca is unavailable."""
        adapter = OrcaHTTPAdapter()
        result = adapter.status()
        assert isinstance(result, dict)

    def test_create_terminal_raises_not_supported(self):
        """create_terminal raises OrcaUnavailableError (not supported via HTTP)."""
        adapter = OrcaHTTPAdapter()
        with patch("orca_http.OrcaHTTPClient"):
            with pytest.raises(OrcaUnavailableError, match="not supported"):
                adapter.create_terminal("test")

    def test_close_worktree_returns_true_for_nonexistent_path(self):
        """close_worktree returns True for a path that doesn't exist."""
        adapter = OrcaHTTPAdapter()
        result = adapter.close_worktree("/nonexistent/path")
        assert result is True


# ── OmniRouteClientAdapter Tests ─────────────────────────────────────────────


class TestOmniRouteClientAdapter:
    """Test the OmniRouteClientAdapter concrete implementation."""

    def test_implements_omniroute_adapter(self):
        """OmniRouteClientAdapter implements OmniRouteAdapter interface."""
        assert issubclass(OmniRouteClientAdapter, OmniRouteAdapter)

    def test_call_model_raises_model_error_on_failure(self):
        """call_model raises ModelError when executor.call_model fails."""
        adapter = OmniRouteClientAdapter()
        with patch("executor.call_model", side_effect=Exception("test error")):
            with pytest.raises(ModelError, match="test error"):
                adapter.call_model("coder", "test prompt")


class TestNousDirectAdapter:
    """Test the NousDirectAdapter concrete implementation."""

    def test_implements_omniroute_adapter(self):
        """NousDirectAdapter implements OmniRouteAdapter interface."""
        assert issubclass(NousDirectAdapter, OmniRouteAdapter)

    def test_call_model_raises_on_missing_api_key(self):
        """call_model raises ModelError when NOUS_API_KEY is missing."""
        adapter = NousDirectAdapter(api_key="", model="test-model")
        with pytest.raises(ModelError, match="NOUS_API_KEY"):
            adapter.call_model("coder", "test prompt")


# ── AgentMailClientAdapter Tests ─────────────────────────────────────────────


class TestAgentMailClientAdapter:
    """Test the AgentMailClientAdapter concrete implementation."""

    def test_implements_agentmail_adapter(self):
        """AgentMailClientAdapter implements AgentMailAdapter interface."""
        assert issubclass(AgentMailClientAdapter, AgentMailAdapter)

    def test_send_message_raises_on_failure(self):
        """send_message raises AgentMailError when the API call fails."""
        adapter = AgentMailClientAdapter()
        with patch("agentmail_client.req", side_effect=Exception("API error")):
            with pytest.raises(AgentMailError, match="API error"):
                adapter.send_message(["inbox-id"], "subject", "text")

    def test_get_inboxes_raises_on_failure(self):
        """get_inboxes raises AgentMailError when the API call fails."""
        adapter = AgentMailClientAdapter()
        with patch("agentmail_client.req", side_effect=Exception("API error")):
            with pytest.raises(AgentMailError, match="API error"):
                adapter.get_inboxes()


# ── GitHubCLIAdapter Tests ───────────────────────────────────────────────────


class TestGitHubCLIAdapter:
    """Test the GitHubCLIAdapter concrete implementation."""

    def test_implements_github_adapter(self):
        """GitHubCLIAdapter implements GitHubAdapter interface."""
        assert issubclass(GitHubCLIAdapter, GitHubAdapter)

    def test_fetch_issues_raises_on_failure(self):
        """fetch_issues raises GitHubError when the fetch fails."""
        adapter = GitHubCLIAdapter()
        with patch("github_fetcher.fetch_issues", side_effect=Exception("fetch error")):
            with pytest.raises(GitHubError, match="fetch error"):
                adapter.fetch_issues("owner/repo")

    def test_fetch_single_issue_raises_on_failure(self):
        """fetch_single_issue raises GitHubError when the fetch fails."""
        adapter = GitHubCLIAdapter()
        with patch("github_fetcher.fetch_single_issue", side_effect=Exception("fetch error")):
            with pytest.raises(GitHubError, match="fetch error"):
                adapter.fetch_single_issue("owner", "repo", 1)


# ── OrcaHermesAdapter Tests ─────────────────────────────────────────────────


class TestOrcaHermesAdapter:
    """Test the OrcaHermesAdapter concrete implementation."""

    def test_implements_hermes_adapter(self):
        """OrcaHermesAdapter implements HermesAdapter interface."""
        assert issubclass(OrcaHermesAdapter, HermesAdapter)

    def test_run_task_raises_on_failure(self):
        """run_task raises HermesUnavailableError when the task fails."""
        adapter = OrcaHermesAdapter()
        with patch("orca_executor.OrcaExecutionManager") as mock_mgr_cls:
            mock_mgr = MagicMock()
            mock_mgr.run_hermes.side_effect = Exception("task failed")
            mock_mgr_cls.return_value = mock_mgr
            adapter._mgr = mock_mgr

            with pytest.raises(HermesUnavailableError, match="task failed"):
                adapter.run_task("/wt/path", "bead-123", "test task")


# ── AdapterRegistry Tests ────────────────────────────────────────────────────


class TestAdapterRegistry:
    """Test the AdapterRegistry for dependency injection."""

    def test_register_and_get_orca(self):
        """Register and retrieve an Orca adapter."""
        registry = AdapterRegistry()
        mock_orca = MagicMock(spec=OrcaAdapter)
        registry.register_orca(mock_orca)
        assert registry.get_orca() is mock_orca

    def test_register_and_get_hermes(self):
        """Register and retrieve a Hermes adapter."""
        registry = AdapterRegistry()
        mock_hermes = MagicMock(spec=HermesAdapter)
        registry.register_hermes(mock_hermes)
        assert registry.get_hermes() is mock_hermes

    def test_register_and_get_omniroute(self):
        """Register and retrieve an OmniRoute adapter."""
        registry = AdapterRegistry()
        mock_omni = MagicMock(spec=OmniRouteAdapter)
        registry.register_omniroute(mock_omni)
        assert registry.get_omniroute() is mock_omni

    def test_register_and_get_agentmail(self):
        """Register and retrieve an AgentMail adapter."""
        registry = AdapterRegistry()
        mock_am = MagicMock(spec=AgentMailAdapter)
        registry.register_agentmail(mock_am)
        assert registry.get_agentmail() is mock_am

    def test_register_and_get_github(self):
        """Register and retrieve a GitHub adapter."""
        registry = AdapterRegistry()
        mock_gh = MagicMock(spec=GitHubAdapter)
        registry.register_github(mock_gh)
        assert registry.get_github() is mock_gh

    def test_default_orca_adapter(self):
        """get_orca returns a default OrcaCLIAdapter when none is registered."""
        registry = AdapterRegistry()
        adapter = registry.get_orca()
        assert isinstance(adapter, OrcaCLIAdapter)

    def test_default_hermes_adapter(self):
        """get_hermes returns a default OrcaHermesAdapter when none is registered."""
        registry = AdapterRegistry()
        adapter = registry.get_hermes()
        assert isinstance(adapter, OrcaHermesAdapter)

    def test_default_agentmail_adapter(self):
        """get_agentmail returns a default AgentMailClientAdapter when none is registered."""
        registry = AdapterRegistry()
        adapter = registry.get_agentmail()
        assert isinstance(adapter, AgentMailClientAdapter)

    def test_default_github_adapter(self):
        """get_github returns a default GitHubCLIAdapter when none is registered."""
        registry = AdapterRegistry()
        adapter = registry.get_github()
        assert isinstance(adapter, GitHubCLIAdapter)


# ── Mock Adapter Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def mock_orca_adapter():
    """Create a mock OrcaAdapter for testing."""
    return MagicMock(spec=OrcaAdapter)


@pytest.fixture
def mock_hermes_adapter():
    """Create a mock HermesAdapter for testing."""
    return MagicMock(spec=HermesAdapter)


@pytest.fixture
def mock_omniroute_adapter():
    """Create a mock OmniRouteAdapter for testing."""
    return MagicMock(spec=OmniRouteAdapter)


@pytest.fixture
def mock_agentmail_adapter():
    """Create a mock AgentMailAdapter for testing."""
    return MagicMock(spec=AgentMailAdapter)


@pytest.fixture
def mock_github_adapter():
    """Create a mock GitHubAdapter for testing."""
    return MagicMock(spec=GitHubAdapter)


@pytest.fixture
def registry_with_mocks(
    mock_orca_adapter,
    mock_hermes_adapter,
    mock_omniroute_adapter,
    mock_agentmail_adapter,
    mock_github_adapter,
):
    """Create an AdapterRegistry pre-populated with mock adapters."""
    registry = AdapterRegistry()
    registry.register_orca(mock_orca_adapter)
    registry.register_hermes(mock_hermes_adapter)
    registry.register_omniroute(mock_omniroute_adapter)
    registry.register_agentmail(mock_agentmail_adapter)
    registry.register_github(mock_github_adapter)
    return registry


class TestMockAdapters:
    """Test that mock adapters work correctly with the registry."""

    def test_mock_orca_in_registry(self, registry_with_mocks, mock_orca_adapter):
        """Mock Orca adapter is returned from registry."""
        assert registry_with_mocks.get_orca() is mock_orca_adapter

    def test_mock_hermes_in_registry(self, registry_with_mocks, mock_hermes_adapter):
        """Mock Hermes adapter is returned from registry."""
        assert registry_with_mocks.get_hermes() is mock_hermes_adapter

    def test_mock_omniroute_in_registry(self, registry_with_mocks, mock_omniroute_adapter):
        """Mock OmniRoute adapter is returned from registry."""
        assert registry_with_mocks.get_omniroute() is mock_omniroute_adapter

    def test_mock_agentmail_in_registry(self, registry_with_mocks, mock_agentmail_adapter):
        """Mock AgentMail adapter is returned from registry."""
        assert registry_with_mocks.get_agentmail() is mock_agentmail_adapter

    def test_mock_github_in_registry(self, registry_with_mocks, mock_github_adapter):
        """Mock GitHub adapter is returned from registry."""
        assert registry_with_mocks.get_github() is mock_github_adapter
