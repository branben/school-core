#!/usr/bin/env python3
"""leaf.py — Disposable student leaf worktree.

A leaf is a short-lived Orca child worktree where a student task executes.
The leaf manages its own lifecycle:

    boot() → write_brief() → run_task() → signal_ready() → dispose()

In Phase 1 (synchronous), the LLM call and CTO/COO review happen inline
in the principal process. The leaf provides workspace isolation and
auto-cleanup. The bookbag is written and verdicts filled by
director.run_task() before signal_ready() is called.

In Phase 2 (future, async), the leaf would only write the bookbag and
signal the teachers, then wait for them to poll and fill verdicts
asynchronously via wait_for_handoff().

Usage:
    # Full lifecycle via convenience function:
    from leaf import run_leaf
    result = run_leaf("Write a hello function", role="coder", domain="python-coding")

    # Manual lifecycle (more control):
    from leaf import StudentLeaf
    leaf = StudentLeaf("coder", "python-coding")
    try:
        leaf.boot()
        leaf.write_brief("Write a hello function")
        result = leaf.run_task("Write a hello function")
        leaf.write_output(result)
        leaf.signal_ready()
    finally:
        leaf.dispose()
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from bookbag import BookbagSignal, wait_for_verdicts, write_bookbag, HANDOFF_TIMEOUT
from director import run_task
from orca_executor import OrcaExecutionManager, StudentBrief, OrcaUnavailableError
from scoring import ScoreStore

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────


class LeafError(Exception):
    """Base exception for leaf-related errors."""
    pass


class LeafNotBootedError(LeafError):
    """Raised when an operation requires a booted leaf."""
    pass


# ── Student Leaf ─────────────────────────────────────────────────────────────


class StudentLeaf:
    """A disposable student leaf worktree.

    Each leaf represents one student task in its own Orca child worktree.
    The worktree is created on boot() and removed on dispose().

    Lifecycle:
        boot() → write_brief() → run_task() → write_output() → signal_ready() → dispose()

    The leaf auto-generates a unique bead and worktree name. The bead
    follows the pattern ``{role}-{domain}-{rand8}`` and the worktree name
    is ``study-{role}-{rand8}``.

    Context manager support:
        with StudentLeaf("coder", "python-coding") as leaf:
            leaf.write_brief(task)
            result = leaf.run_task(task)
            leaf.write_output(result)
            leaf.signal_ready()
        # auto-disposes

    Args:
        role: The student role (e.g., "coder", "searcher").
        domain: Task domain for role selection.
        difficulty: Task difficulty level.
        store: Optional ScoreStore instance (created fresh if None).
        poll_interval: Poll interval for future async handoff.
    """

    def __init__(
        self,
        role: str,
        domain: str,
        difficulty: str = "easy",
        store: Optional[ScoreStore] = None,
        handoff_timeout: float = HANDOFF_TIMEOUT,
    ):
        self.role = role
        self.domain = domain
        self.difficulty = difficulty
        self.handoff_timeout = handoff_timeout

        # Auto-generate unique identifiers
        rand = uuid.uuid4().hex[:8]
        self.bead = f"{role}-{domain}-{rand}"
        self.worktree_name = f"study-{role}-{rand}"
        self.worktree_path: Optional[str] = None

        # Dependencies
        self._store = store or ScoreStore()
        self._mgr: Optional[OrcaExecutionManager] = None
        self._booted = False

        # Hermes profile name derived from role
        self._hermes_profile = self._profile_for_role(role)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @staticmethod
    def _profile_for_role(role: str) -> str:
        """Map a school-core role to a Hermes profile name."""
        mapping = {
            "coder": "student-coder",
            "searcher": "student-searcher",
            "executor": "student-executor",
            "browser": "student-browser",
            "reviewer": "student-reviewer",
            "tester": "student-coder",
            "debugger": "student-coder",
        }
        return mapping.get(role, "student-coder")

    def boot(self) -> str:
        """Create the disposable student worktree in Orca.

        The worktree appears in Orca's UI sidebar with the name
        ``study-{role}-{rand8}``. It is a proper child worktree linked
        to the school-core repository.

        Returns:
            The absolute path to the created worktree.

        Raises:
            LeafError: If Orca runtime is unavailable.
        """
        self._mgr = OrcaExecutionManager()
        try:
            self.worktree_path = self._mgr.create_worktree(self.worktree_name)
            self._booted = True
            logger.info("[leaf:%s] Booted worktree at %s", self.bead[:12], self.worktree_path)
            return self.worktree_path
        except OrcaUnavailableError as e:
            raise LeafError(
                f"Failed to boot leaf '{self.bead}': {e}"
            ) from e

    def write_brief(self, task_prompt: str) -> Path:
        """Write a StudentBrief to the worktree.

        The brief is placed at ``.hermes/briefs/{bead}.json`` inside the
        worktree. This serves as the task contract for the student.

        Args:
            task_prompt: The task description.

        Returns:
            Path to the written brief file.

        Raises:
            LeafNotBootedError: If boot() hasn't been called.
        """
        self._ensure_booted()
        brief = StudentBrief(
            bead=self.bead,
            role=self.role,
            domain=self.domain,
            task=task_prompt,
            difficulty=self.difficulty,
        )
        return self._mgr.write_student_brief(self.worktree_path, brief)

    def run_via_hermes(self, task_prompt: str) -> dict:
        """Execute the task by running Hermes agent in the leaf's Orca terminal.

        Unlike ``run_task()`` which makes an OmniRoute API call from the
        principal process, this method runs Hermes inside the leaf's own
        Orca terminal — the terminal actually does work instead of sitting
        empty. Output is captured via file redirect.

        Pipeline:
            1. Read SOUL.md from ``~/.hermes/profiles/{profile}/SOUL.md``
            2. Compose full prompt: ``{SOUL.md}\n\n---\n\nTask: {task_prompt}``
            3. Call ``OrcaExecutionManager.run_hermes()`` in the worktree
            4. Write the response to ``.hermes/outputs/{bead}.json``
            5. Write the bookbag directly (bypasses ``director.run_task()``)

        Args:
            task_prompt: The task to dispatch.

        Returns:
            Dict with ``status``, ``response``, ``bead``, ``bookbag`` path,
            and empty review fields (teachers fill verdicts async).

        Raises:
            LeafNotBootedError: If boot() hasn't been called.
            OrcaUnavailableError: If Orca is down or Hermes fails.
        """
        self._ensure_booted()

        # ── Read SOUL.md from Hermes profile ─────────────────────────────
        soul_path = Path.home() / ".hermes" / "profiles" / self._hermes_profile / "SOUL.md"
        if soul_path.exists():
            soul = soul_path.read_text().strip()
        else:
            # Fallback: use role name as minimal system prompt
            soul = f"You are a {self.role} agent. Complete the task precisely."

        # ── Compose full prompt ──────────────────────────────────────────
        full_prompt = f"{soul}\n\n---\n\nTask: {task_prompt}"

        # ── Run Hermes in the leaf's Orca terminal ───────────────────────
        logger.info("[leaf:%s] Running Hermes (%s) in Orca terminal...", self.bead[:12], self._hermes_profile)

        try:
            response = self._mgr.run_hermes(
                worktree_path=self.worktree_path,
                bead=self.bead,
                task=full_prompt,
            )
        except OrcaUnavailableError as e:
            logger.error("[leaf:%s] Hermes failed: %s", self.bead[:12], e)
            # Write a failed bookbag so pipeline doesn't hang
            write_bookbag(
                self.bead,
                student=self.role,
                domain=self.domain,
                difficulty=self.difficulty,
                task=task_prompt[:200],
                output=f"[Hermes error: {e}]",
            )
            return {
                "status": "error",
                "bead": self.bead,
                "agent": self.role,
                "domain": self.domain,
                "response": "",
                "error": str(e),
                "bookbag": str(Path.home() / ".hermes" / "bookbag" / f"{self.bead}.json"),
                "review": {
                    "cto_verdict": "", "coo_verdict": "",
                    "cto_score": 0, "coo_score": 0,
                    "findings": [], "accepted": False,
                },
                "async": True,
            }

        # ── Write output for audit trail ─────────────────────────────────
        output_data = {
            "bead": self.bead,
            "role": self.role,
            "domain": self.domain,
            "difficulty": self.difficulty,
            "task": task_prompt,
            "response": response,
            "response_chars": len(response),
            "hermes_profile": self._hermes_profile,
        }
        self._mgr.write_student_output(self.worktree_path, self.bead, output_data)

        # ── Write bookbag directly (bypasses director.run_task) ──────────
        write_bookbag(
            self.bead,
            student=self.role,
            domain=self.domain,
            difficulty=self.difficulty,
            task=task_prompt[:200],
            output=response,
        )

        logger.info("[leaf:%s] Hermes completed: %d chars", self.bead[:12], len(response))

        return {
            "status": "success",
            "bead": self.bead,
            "agent": self.role,
            "domain": self.domain,
            "difficulty": self.difficulty,
            "response": response,
            "error": None,
            "bookbag": str(Path.home() / ".hermes" / "bookbag" / f"{self.bead}.json"),
            "review": {
                "cto_verdict": "",
                "coo_verdict": "",
                "cto_score": 0,
                "coo_score": 0,
                "findings": [],
                "accepted": False,
            },
            "async": True,
        }

    def run_task(self, task_prompt: str, skip_review: bool = False) -> dict:
        """Execute the task via ``director.run_task()``.

        In Phase 1 (synchronous), the LLM call and CTO/COO review happen
        inline. The result includes verdicts, findings, and scores.

        In Phase 2 (async), set ``skip_review=True``. Only the LLM call
        and bookbag write happen. Verdicts are empty — teachers fill them
        asynchronously. The caller must poll for verdicts and score.

        Args:
            task_prompt: The task to dispatch to the student LLM.
            skip_review: If True, skip CTO/COO review (Phase 2 async).

        Returns:
            Dict with task result (same schema as director.run_task()).

        Raises:
            LeafNotBootedError: If boot() hasn't been called.
        """
        self._ensure_booted()
        result = run_task(
            prompt=task_prompt,
            domain=self.domain,
            difficulty=self.difficulty,
            force_agent=self.role,
            store=self._store,
            skip_review=skip_review,
        )
        return result

    def write_output(self, data: dict) -> Path:
        """Write task output to the worktree for audit trail.

        Data is written to ``.hermes/outputs/{bead}.json`` inside the
        worktree. This provides a complete record of the task execution.

        Args:
            data: Dict of output data (response, review, scores, etc.).

        Returns:
            Path to the written output file.

        Raises:
            LeafNotBootedError: If boot() hasn't been called.
        """
        self._ensure_booted()
        return self._mgr.write_student_output(self.worktree_path, self.bead, data)

    def signal_ready(self) -> None:
        """Signal teachers that the bookbag is ready for review.

        Writes a ``.hermes/signals/{bead}.ready`` flag file. In Phase 1
        this is informational (verdicts are already filled synchronously).
        In Phase 2, teachers would poll for this flag.
        """
        signal = BookbagSignal(self.bead)
        signal.ready()
        logger.info("[leaf:%s] Signaled ready for review", self.bead[:12])

    def wait_for_handoff(self, timeout: Optional[float] = None) -> tuple[str, str]:
        """Wait for CTO and COO verdicts on the bookbag.

        Phase 2 (async) method. In Phase 1, verdicts are already filled
        by the time run_task() returns, so this is a no-op in practice.
        In Phase 2, this would poll until both teachers fill verdicts.

        Args:
            timeout: Maximum seconds to wait. Falls back to
                     ``self.handoff_timeout`` if not specified.

        Returns:
            (cto_verdict, coo_verdict) — both non-empty strings.

        Raises:
            HandoffTimeoutError: If timeout expires before both verdicts.
        """
        return wait_for_verdicts(
            self.bead,
            timeout=timeout if timeout is not None else self.handoff_timeout,
        )

    def dispose(self) -> None:
        """Remove the worktree and clean up. Idempotent.

        Removes the Orca child worktree, clears the booted flag, and
        releases the OrcaExecutionManager reference.
        """
        if self._mgr and self.worktree_path:
            try:
                self._mgr.close_worktree(self.worktree_path)
                logger.info("[leaf:%s] Disposed worktree", self.bead[:12])
            except Exception as e:
                logger.warning("[leaf:%s] Dispose error: %s", self.bead[:12], e)
            self.worktree_path = None
            self._booted = False
            self._mgr = None

    # ── Context Manager ──────────────────────────────────────────────────────

    def __enter__(self):
        if not self._booted:
            self.boot()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.dispose()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _ensure_booted(self) -> None:
        """Raise LeafNotBootedError if boot() hasn't been called."""
        if not self._booted or not self.worktree_path:
            raise LeafNotBootedError(
                f"Leaf '{self.bead}' is not booted. Call boot() first."
            )

    def __repr__(self) -> str:
        status = "booted" if self._booted else "unbooted"
        return f"<StudentLeaf bead={self.bead!r} role={self.role!r} {status}>"


# ── Convenience Function ─────────────────────────────────────────────────────


def run_leaf(
    task_prompt: str,
    role: str,
    domain: str,
    difficulty: str = "easy",
    store: Optional[ScoreStore] = None,
    async_mode: bool = False,
) -> dict:
    """Run a task in a disposable leaf worktree.

    Full lifecycle convenience wrapper:

    **Phase 1 (synchronous, default):**
        1. Create and boot the leaf worktree
        2. Write the StudentBrief
        3. Execute the task (LLM call + CTO/COO review inline)
        4. Write output to the worktree
        5. Signal teachers that the bookbag is ready
        6. Auto-dispose the worktree

    **Phase 2 (async, ``async_mode=True``):**
        1. Create and boot the leaf worktree
        2. Write the StudentBrief
        3. Execute only the LLM call (no review — ``skip_review=True``)
        4. Signal teachers that the bookbag is ready
        5. **DOES NOT dispose** — the caller must call ``leaf.dispose()``
           after polling for teacher verdicts and scoring.
        The returned result includes ``"async": True`` and ``"bead"``
        for the caller to poll via ``wait_for_verdicts()``.

    Args:
        task_prompt: The task to dispatch.
        role: Student role (e.g., "coder", "searcher").
        domain: Task domain.
        difficulty: Task difficulty level.
        store: Optional ScoreStore.
        async_mode: If True, skip review and disposal (Phase 2).

    Returns:
        Dict with the full task result. In async mode, ``review`` fields
        are empty — the caller must poll for teacher verdicts.

    Raises:
        LeafError: If Orca runtime is unavailable.
    """
    leaf = StudentLeaf(role=role, domain=domain, difficulty=difficulty, store=store)

    try:
        leaf.boot()
        leaf.write_brief(task_prompt)
        result = leaf.run_task(task_prompt, skip_review=async_mode)

        if result.get("status") == "success":
            # Write output for audit trail
            output_data = {
                "bead": leaf.bead,
                "role": role,
                "domain": domain,
                "difficulty": difficulty,
                "task": task_prompt,
                "response": result.get("response", ""),
                "response_chars": len(result.get("response", "")),
                "review": {
                    "cto_verdict": result.get("review", {}).get("cto_verdict", "?"),
                    "coo_verdict": result.get("review", {}).get("coo_verdict", "?"),
                    "cto_score": result.get("review", {}).get("cto_score", 0),
                    "coo_score": result.get("review", {}).get("coo_score", 0),
                    "findings": result.get("review", {}).get("findings", []),
                    "accepted": result.get("review", {}).get("accepted", False),
                },
                "scores": {
                    "old": result.get("old_score", 0),
                    "new": result.get("new_score", 0),
                },
            }
            leaf.write_output(output_data)
            leaf.signal_ready()

            if not async_mode:
                # Phase 2 (future): uncomment for async teacher handoff
                # cto, coo = leaf.wait_for_handoff()
                # result["handoff"] = {"cto_verdict": cto, "coo_verdict": coo}
                pass

        return result

    finally:
        if not async_mode:
            leaf.dispose()
        # In async mode, caller is responsible for disposing after handoff


# ── CLI Entry Point ──────────────────────────────────────────────────────────


def main():
    """Run a single leaf task from the command line.

    Usage:
        python leaf.py --role coder --domain python-coding --task "Write a function"
        python leaf.py --role searcher --domain code-search --task "Find all TODO comments"

    Useful for testing individual leaf lifecycle without the full conductor.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Run a disposable student leaf task")
    parser.add_argument("--role", required=True, help="Student role (e.g., coder, searcher)")
    parser.add_argument("--domain", required=True, help="Task domain")
    parser.add_argument("--difficulty", default="easy", help="Task difficulty")
    parser.add_argument("--task", required=True, help="Task prompt")
    parser.add_argument("--store-path", default=None, help="Path to ScoreStore data file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    store = ScoreStore(path=args.store_path) if args.store_path else ScoreStore()

    print(f"🍃 Leaf: role={args.role}, domain={args.domain}, difficulty={args.difficulty}")
    print(f"   Task: {args.task[:100]}")
    print()

    result = run_leaf(
        task_prompt=args.task,
        role=args.role,
        domain=args.domain,
        difficulty=args.difficulty,
        store=store,
    )

    status = result.get("status", "error")
    if status == "success":
        review = result.get("review", {})
        cto = review.get("cto_verdict", "?")
        coo = review.get("coo_verdict", "?")
        accepted = review.get("accepted", False)
        print(f"\n✅ Status: {status}")
        print(f"   CTO: {cto} | COO: {coo} | Accepted: {accepted}")
        response = result.get("response", "")
        print(f"   Response: {len(response)} chars")
    else:
        error = result.get("error", result.get("status", "unknown"))
        print(f"\n❌ Status: {status} — {error}")


if __name__ == "__main__":
    main()
