"""Hermes adapter — concrete implementation of HermesAdapter.

Wraps the Orc Hermes execution path behind the HermesAdapter interface.
Uses OrcaExecutionManager.run_hermes() internally.
"""

from __future__ import annotations

from typing import Optional

from adapters.base import HermesAdapter, HermesUnavailableError


class OrcaHermesAdapter(HermesAdapter):
    """Concrete Hermes adapter that delegates to OrcaExecutionManager.run_hermes.

    This adapter runs Hermes agent tasks inside an Orca worktree terminal.
    It wraps the existing ``OrcaExecutionManager.run_hermes()`` so that callers
    can depend on the adapter abstraction rather than importing the concrete
    class directly.
    """

    def __init__(self, orca_adapter: Optional[object] = None):
        """Initialize the Hermes adapter.

        Args:
            orca_adapter: Optional OrcaAdapter instance for terminal/worktree
                management. If None, the adapter will create an
                OrcaExecutionManager internally.
        """
        self._orca = orca_adapter
        self._mgr = None

    def _get_manager(self):
        """Get or create the OrcaExecutionManager."""
        if self._mgr is None:
            try:
                from orca_executor import OrcaExecutionManager
                self._mgr = OrcaExecutionManager()
            except Exception as e:
                raise HermesUnavailableError(
                    f"Failed to create OrcaExecutionManager: {e}"
                ) from e
        return self._mgr

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

        Delegates to OrcaExecutionManager.run_hermes() which handles:
        - Writing the task to a file in the worktree
        - Creating a launcher script that runs hermes chat
        - Creating an Orca terminal and running the launcher
        - Polling a DONE sentinel for completion
        - Reading the response from disk
        """
        mgr = self._get_manager()
        try:
            return mgr.run_hermes(
                worktree_path=worktree_path,
                bead=bead,
                task=task,
                role=role,
                difficulty=difficulty,
                timeout_ms=timeout_ms,
                toolsets=toolsets,
            )
        except Exception as e:
            raise HermesUnavailableError(
                f"Hermes task failed for bead={bead}: {e}"
            ) from e
