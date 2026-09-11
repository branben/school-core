"""Abstract base classes for ecosystem adapters.

Each adapter defines a clean interface for coupling to an external system:
- HermesAdapter: Run Hermes agent tasks
- OrcaAdapter: Manage Orca worktrees and terminals
- OmniRouteAdapter: Route model calls to LLM backends
- AgentMailAdapter: Send/receive email notifications
- GitHubAdapter: Fetch issues and interact with GitHub

Concrete implementations wrap the existing plumbing (subprocess calls,
HTTP requests, CLI invocations) behind these uniform interfaces so that
the rest of the codebase depends on abstractions, not on transport
details. This makes testing easier (inject a mock adapter) and makes it
possible to swap transports (e.g., Orca CLI -> Orca HTTP) without
touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


# ── Hermes ──────────────────────────────────────────────────────────────────


class HermesAdapter(ABC):
    """Interface for running Hermes agent tasks.

    Hermes is the "brain" layer — the agent runtime that executes student
    and teacher tasks inside an Orca worktree terminal.
    """

    @abstractmethod
    def run_task(
        self,
        worktree_path: str,
        bead: str,
        task: str,
        *,
        role: str = "student",
        difficulty: str = "easy",
        timeout_ms: Optional[int] = None,
        toolsets: Optional[str] = None,
    ) -> str:
        """Run a Hermes agent task and return the response text.

        Args:
            worktree_path: Absolute path to the agent's worktree.
            bead: Unique task identifier for scoping temp files.
            task: The full task prompt (system prompt + task).
            role: The agent role (student, teacher-cto, teacher-coo).
            difficulty: Task difficulty — controls max-turns.
            timeout_ms: Max wait time in milliseconds. None = difficulty-based.
            toolsets: Comma-separated Hermes toolset names.

        Returns:
            The captured response text.

        Raises:
            HermesUnavailableError: If Hermes cannot run the task.
        """
        ...


# ── Orca ────────────────────────────────────────────────────────────────────


class OrcaAdapter(ABC):
    """Interface for Orca runtime operations.

    Orca is the "office building" — it provides worktrees (rooms),
    terminals (desks), and the scheduler (clock).
    """

    @abstractmethod
    def create_worktree(self, name: str, repo_path: Optional[str] = None) -> str:
        """Create a child worktree and return its absolute path.

        Args:
            name: Worktree name (e.g., "study-coder-r1").
            repo_path: Optional target repo path. Defaults to the registered repo.

        Returns:
            Absolute path to the created worktree.

        Raises:
            OrcaUnavailableError: If the worktree cannot be created.
        """
        ...

    @abstractmethod
    def close_worktree(self, path: str) -> bool:
        """Remove a worktree by path. Idempotent.

        Returns:
            True if the worktree was removed (or was already gone).
        """
        ...

    @abstractmethod
    def create_terminal(self, title: str = "exec") -> str:
        """Create a terminal and return its handle.

        Raises:
            OrcaUnavailableError: If the terminal cannot be created.
        """
        ...

    @abstractmethod
    def close_terminal(self, handle: str) -> None:
        """Close a terminal session. Best-effort."""
        ...

    @abstractmethod
    def status(self) -> dict:
        """Get Orca runtime status.

        Returns:
            Dict with at least a 'runtime' key containing 'state'.
        """
        ...


# ── OmniRoute ───────────────────────────────────────────────────────────────


class OmniRouteAdapter(ABC):
    """Interface for model routing and LLM calls.

    OmniRoute is the "model faucet" — it routes prompts to the right
    LLM backend (OmniRoute proxy, Nous direct, A2A agent-to-agent).
    """

    @abstractmethod
    def call_model(
        self,
        agent_name: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Call a model and return the response text.

        Args:
            agent_name: The agent/role name (e.g., "coder", "reviewer").
            prompt: The user prompt.
            system_prompt: Optional system prompt override.
            timeout: Optional timeout in seconds.

        Returns:
            The model's response text.

        Raises:
            ModelError: If the model call fails.
        """
        ...


# ── AgentMail ───────────────────────────────────────────────────────────────


class AgentMailAdapter(ABC):
    """Interface for AgentMail email operations.

    AgentMail is the bidirectional control plane — agents report verdicts
    and the human replies /approve /reject /fix.
    """

    @abstractmethod
    def send_message(
        self,
        to: list[str],
        subject: str,
        text: str,
    ) -> dict:
        """Send a message.

        Args:
            to: List of recipient inbox IDs.
            subject: Message subject.
            text: Message body.

        Returns:
            The API response dict.

        Raises:
            AgentMailError: If the send fails.
        """
        ...

    @abstractmethod
    def get_inboxes(self) -> list[dict]:
        """Get available inboxes.

        Returns:
            List of inbox dicts with at least 'inbox_id'.
        """
        ...


# ── GitHub ──────────────────────────────────────────────────────────────────


class GitHubAdapter(ABC):
    """Interface for GitHub operations.

    GitHub is the issue source and PR destination. This adapter fetches
    issues and creates PRs via the `gh` CLI or REST API.
    """

    @abstractmethod
    def fetch_issues(
        self,
        repo: str,
        labels: Optional[list[str]] = None,
    ) -> list[dict]:
        """Fetch open issues from a GitHub repo.

        Args:
            repo: Repository in owner/repo format.
            labels: Optional label filter.

        Returns:
            List of issue dicts with keys: issue_number, title, body,
            domain, difficulty, prompt, category, state.
        """
        ...

    @abstractmethod
    def fetch_single_issue(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> Optional[dict]:
        """Fetch one GitHub issue by number.

        Returns:
            Issue dict or None on failure.
        """
        ...


# ── Exceptions ───────────────────────────────────────────────────────────────


class AdapterError(Exception):
    """Base exception for adapter failures."""
    pass


class HermesUnavailableError(AdapterError):
    """Raised when Hermes cannot run a task."""
    pass


class OrcaUnavailableError(AdapterError):
    """Raised when Orca runtime is not available."""
    pass


class ModelError(AdapterError):
    """Raised when a model call fails."""
    pass


class AgentMailError(AdapterError):
    """Raised when an AgentMail operation fails."""
    pass


class GitHubError(AdapterError):
    """Raised when a GitHub operation fails."""
    pass
