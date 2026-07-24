#!/usr/bin/env python3
"""
teacher.py — Persistent teacher worktree lifecycle.

CTO and COO teachers each run in their own persistent Orca worktree.
They sleep between tasks via sleep_state.execute_sleep() and wake when
a new bookbag signals a completed student for review.

Lifecycle per teacher:
    boot() → [sleep() → wake() → review_cycle() → sleep()] × N

The review_cycle() polls ~/.hermes/bookbag/ for un-reviewed bookbags,
applies its adversarial lens (CTO = correctness+security, COO = completeness),
and updates the bookbag with verdict + findings.

Usage:
    # Standalone (meant to run inside a teacher worktree terminal):
    from teacher import TeacherWorktree
    teacher = TeacherWorktree("cto")
    teacher.boot()
    teacher.run_loop()  # infinite sleep/wake/review cycle

    # Or from conductor.py (principal):
    mgr = OrcaExecutionManager()
    path = mgr.create_worktree("teacher-cto")
    handle = mgr.create_terminal(title="teacher-cto")
    mgr._run_orca(["terminal", "send", "--terminal", handle,
                   "--text", 'python3 -c "from teacher import TeacherWorktree; '
                             'TeacherWorktree(\\'cto\\').run_loop()"',
                   "--enter"])
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from adversarial_reviewer import AdversarialReviewer, LensType
from bookbag import read_bookbag, locked_update_bookbag, list_bookbags
from orca_executor import OrcaExecutionManager, OrcaUnavailableError
from scoring import ScoreStore
from sleep_state import execute_sleep, execute_wake

logger = logging.getLogger(__name__)


def load_soul(profile_name: str) -> str:
    """Resolve a persona's SOUL.md.

    Resolution order (single source of truth = repo config/profiles):
        1. ``<repo>/config/profiles/<name>/SOUL.md``  (committed, authoritative)
        2. ``~/.hermes/profiles/<name>/SOUL.md``       (machine-local override)
        3. empty string (caller supplies a generic fallback)
    """
    repo_soul = Path(__file__).parent / "config" / "profiles" / profile_name / "SOUL.md"
    if repo_soul.exists():
        return repo_soul.read_text().strip()
    home_soul = Path.home() / ".hermes" / "profiles" / profile_name / "SOUL.md"
    if home_soul.exists():
        return home_soul.read_text().strip()
    return ""


# ── Role configuration ───────────────────────────────────────────────────────

TEACHER_LENSES = {
    "cto": [LensType.CORRECTNESS, LensType.SECURITY],
    "coo": [LensType.COMPLETENESS],
}

DEFAULT_SESSION_ID = "teacher-default"
DEFAULT_POLL_INTERVAL = 5.0  # seconds between bookbag polls
DEFAULT_REVIEW_TIMEOUT = 90  # seconds for LLM review call
MAX_SESSION_CYCLES = 10  # auto-prune sessions older than this many cycles

# ── Exceptions ───────────────────────────────────────────────────────────────


class TeacherError(Exception):
    """Base exception for teacher-related errors."""
    pass


# ── Teacher Worktree ─────────────────────────────────────────────────────────


class TeacherWorktree:
    """A persistent teacher worktree that sleeps/wakes for bookbag review.

    Each teacher (CTO or COO) runs in its own Orca worktree with a dedicated
    terminal. The teacher enters an infinite loop:
        1. Sleep (save state via sleep_state.execute_sleep)
        2. Wake (restore state via sleep_state.execute_wake)
        3. Poll for un-reviewed bookbags matching its lens type
        4. Review via AdversarialReviewer
        5. Update bookbag with verdict + findings
        6. Repeat

    Args:
        role: "cto" or "coo"
        poll_interval: Seconds between bookbag poll checks.
        session_id: Unique session ID for sleep/wake state persistence.
    """

    def __init__(
        self,
        role: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        session_id: str = DEFAULT_SESSION_ID,
    ):
        if role not in TEACHER_LENSES:
            raise ValueError(f"Unknown teacher role '{role}'. Must be 'cto' or 'coo'.")
        self.role = role
        self.lenses = TEACHER_LENSES[role]
        self.poll_interval = poll_interval
        self.session_id = f"{session_id}-{role}"
        self.worktree_name = f"teacher-{role}"
        self.worktree_path: Optional[str] = None
        self._mgr: Optional[OrcaExecutionManager] = None
        self._review_terminal: Optional[str] = None  # Reusable Hermes terminal
        self._store = ScoreStore()
        self._reviewer = AdversarialReviewer(call_model_fn=self._call_review_model_via_hermes)
        self._cycle_count = 0
        self._episodic_history: list[dict] = []
        self._booted = False

    # ── Public API ───────────────────────────────────────────────────────────

    def boot(self) -> str:
        """Create or rediscover the persistent teacher worktree.

        Tries to create the worktree via OrcaExecutionManager. If the worktree
        already exists (e.g., from a previous principal run), rediscovers it
        by scanning Orca's worktree list for a matching name.

        Returns:
            The path to the teacher's worktree.

        Raises:
            TeacherError: If worktree cannot be created or found.
        """
        self._mgr = OrcaExecutionManager()

        # Rediscover first (idempotency): Orca auto-suffixes
        # `worktree create --name X` when X already exists (X-2, X-3...),
        # so an unconditionally-create() call spawns a duplicate on every
        # re-boot. Scan the Orca worktree list for ANY worktree whose
        # displayName (or path basename) is the canonical name OR a
        # suffixed variant (teacher-cto / teacher-cto-4), and reuse it.
        def _wt_name(wt: dict) -> str:
            # Orca's worktree list returns displayName (live) but some
            # versions/clients populate `name`; fall back to path basename.
            # Check all three so rediscovery works regardless of which
            # field Orca populates (or if path is empty).
            for key in ("displayName", "name", "path"):
                val = wt.get(key) or ""
                if key == "path":
                    val = Path(val).name if val else ""
                if val:
                    return val
            return ""

        try:
            result = self._mgr._run_orca(["worktree", "list"], timeout=15)
            wts = result.get("worktrees", [])
            for wt in wts:
                nm = _wt_name(wt)
                # Match canonical name OR a digit-suffixed variant
                # (teacher-cto / teacher-cto-4), but NOT unrelated names
                # like teacher-cto-backup or teacher-cto-legacy.
                is_match = (
                    nm == self.worktree_name
                    or nm.startswith(self.worktree_name + "-")
                    and nm[len(self.worktree_name) + 1:].isdigit()
                )
                if is_match:
                    path = wt.get("path") or ""
                    if not path and "::" in wt.get("id", ""):
                        path = wt["id"].split("::", 1)[1]
                    if not path:
                        continue
                    self.worktree_path = path
                    self._review_terminal = self._mgr.create_terminal(
                        title="teacher-" + self.role + "-review"
                    )
                    self._booted = True
                    logger.info(
                        "[teacher:%s] Rediscovered worktree at %s",
                        self.role, self.worktree_path,
                    )
                    return self.worktree_path
        except Exception:
            pass

        # No existing worktree - create it.
        try:
            self.worktree_path = self._mgr.create_worktree(self.worktree_name)
            self._review_terminal = self._mgr.create_terminal(
                title="teacher-" + self.role + "-review"
            )
            self._booted = True
            logger.info(
                "[teacher:%s] Created worktree at %s",
                self.role, self.worktree_path,
            )
            return self.worktree_path
        except OrcaUnavailableError:
            pass

        # Fall through to error if neither rediscovery nor creation worked.
        raise TeacherError(
            "[teacher:" + self.role + "] could not create or rediscover worktree '"
            + self.worktree_name + "'"
        )

    def sleep(self, duration_minutes: float = 0.0) -> dict:
        """Execute the sleep sequence for this teacher.

        Persists session state, creates consolidation artifact, and logs
        the sleep event to the Library Log.

        Args:
            duration_minutes: How long this work session lasted.

        Returns:
            Dict with state, consolidation, and log_entry.
        """
        result = execute_sleep(
            session_id=self.session_id,
            agent=f"teacher-{self.role}",
            store=self._store,
            building="default",
            episodic_history=self._episodic_history,
            duration_minutes=duration_minutes,
        )
        self._episodic_history = []
        logger.info(
            "[teacher:%s] Sleep: %d tasks, %d domains",
            self.role,
            result.get("consolidation", {}).tasks_completed,
            len(result.get("consolidation", {}).domains_visited or []),
        )
        return result

    def wake(self) -> dict:
        """Restore teacher state from the last sleep.

        Loads persisted session state and consolidation artifact.
        Returns the restored state for context resumption.

        Returns:
            Dict with state, consolidation, and log_entry.

        Raises:
            TeacherError: If session data is corrupted or missing.
        """
        try:
            result = execute_wake(session_id=self.session_id)
            logger.info(
                "[teacher:%s] Wake: agent=%s, %d queued tasks",
                self.role,
                result.get("state", {}).agent,
                len(result.get("state", {}).task_queue or []),
            )
            return result
        except Exception as e:
            logger.warning("[teacher:%s] Wake failed (first boot?): %s", self.role, e)
            return {"state": None, "consolidation": None, "log_entry": None}

    def review_cycle(self) -> int:
        """Poll for un-reviewed bookbags and review one if found.

        Checks all bookbags on disk for ones where this teacher's verdict
        is empty. If found, applies the adversarial lens, updates the bookbag,
        and records the event in episodic history.

        Returns:
            1 if a bookbag was reviewed, 0 if none found.
        """
        if not self._booted:
            raise TeacherError("Teacher not booted — call boot() first")

        for bead in list_bookbags():
            bag = read_bookbag(bead)
            if bag is None:
                continue

            verdict_field = f"{self.role}_verdict"
            if bag.get(verdict_field, ""):
                continue  # Already reviewed by this teacher

            # Found a bookbag that needs this teacher's review
            logger.info("[teacher:%s] Reviewing bead=%s", self.role, bead)

            # Build the task dict from the bookbag
            task = {
                "title": (bag.get("task") or "")[:100],
                "body": bag.get("task") or "",
                "domain": bag.get("domain", "general"),
                "difficulty": bag.get("difficulty", "medium"),
            }

            # Run the adversarial review
            try:
                result = self._reviewer.review(
                    output=bag.get("output", ""),
                    task=task,
                    codebase_context="",
                    lens_types=self.lenses,
                )

                verdict = result.verdict.value  # "PASS" or "FAIL"
                findings_dicts = [f.to_dict() for f in result.findings]

                # Update bookbag with lock protection
                updated = locked_update_bookbag(
                    bead,
                    lock_timeout=10.0,
                    **{verdict_field: verdict,
                       f"{self.role}_findings": findings_dicts,
                       f"{self.role}_score": result.score,
                       f"{self.role}_confidence": result.confidence,
                       f"{self.role}_lens": result.lens_used,
                       f"{self.role}_reviewed_at": datetime.now(timezone.utc).isoformat(),
                       },
                )

                if updated is None:
                    logger.warning(
                        "[teacher:%s] Could not update bookbag %s (lock timeout)",
                        self.role, bead,
                    )
                    return 0

                # Record in episodic history
                self._episodic_history.append({
                    "bead": bead,
                    "domain": bag.get("domain", "unknown"),
                    "verdict": verdict,
                    "findings_count": len(findings_dicts),
                    "score": result.score,
                    "status": "success",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                logger.info(
                    "[teacher:%s] Verdict for %s: %s (score=%.0f, findings=%d)",
                    self.role, bead, verdict, result.score, len(findings_dicts),
                )

            except Exception as e:
                logger.error(
                    "[teacher:%s] Review failed for %s: %s",
                    self.role, bead, e,
                )
                self._episodic_history.append({
                    "bead": bead,
                    "domain": bag.get("domain", "unknown"),
                    "status": "error",
                    "error": str(e)[:200],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            return 1  # Processed one bookbag

        return 0  # No bookbags to review

    def run_loop(self) -> None:
        """Enter the infinite sleep/wake/review cycle.

        Designed to be started inside the teacher's worktree terminal:
            python -c "from teacher import TeacherWorktree; TeacherWorktree('cto').run_loop()"

        The loop:
        1. Wake (restore state from previous session)
        2. Poll for bookbags to review
        3. If a bookbag is found, review it and loop back to 2
        4. If no bookbags found, sleep and loop back to 1
        """
        if not self._booted:
            print(f"[teacher:{self.role}] Boot required — run boot() first", file=sys.stderr)
            return

        print(f"[teacher:{self.role}] Entering run loop (poll={self.poll_interval}s)")

        while True:
            self._cycle_count += 1
            session_start = time.monotonic()

            # 1. Wake
            self.wake()

            # 2. Poll for bookbags (keep polling while bookbags exist)
            reviewed = 0
            while True:
                count = self.review_cycle()
                if count == 0:
                    break
                reviewed += count
                time.sleep(self.poll_interval)

            # 3. Sleep (only if we reviewed something — saves state)
            if reviewed > 0:
                session_duration = (time.monotonic() - session_start) / 60.0
                self.sleep(duration_minutes=session_duration)
            else:
                # Brief pause before checking again
                time.sleep(self.poll_interval)

            # 4. Prune old sessions periodically
            if self._cycle_count % MAX_SESSION_CYCLES == 0:
                self.prune_sessions(max_cycles=MAX_SESSION_CYCLES)

    def prune_sessions(self, max_cycles: int = MAX_SESSION_CYCLES) -> int:
        """Remove old teacher session files beyond the retention limit.

        sleep_state.execute_sleep() saves session files as {session_id}.json
        and consolidation as {session_id}.yaml. Over time these accumulate.
        Keeps only the most recent `max_cycles` session files.

        Args:
            max_cycles: Maximum number of session files to keep.

        Returns:
            Number of session files pruned.
        """
        from sleep_state import SESSIONS_DIR, CONSOLIDATION_DIR

        pruned = 0
        session_prefix = f"{self.session_id}.json"
        cons_prefix = f"{self.session_id}.yaml"

        # Session files (sorted by mtime, oldest first)
        session_files = sorted(
            SESSIONS_DIR.glob(f"{self.session_id}*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if len(session_files) > max_cycles:
            for f in session_files[:-max_cycles]:
                try:
                    f.unlink()
                    pruned += 1
                except OSError:
                    pass

        # Consolidation files
        cons_files = sorted(
            CONSOLIDATION_DIR.glob(f"{self.session_id}*.yaml"),
            key=lambda p: p.stat().st_mtime,
        )
        if len(cons_files) > max_cycles:
            for f in cons_files[:-max_cycles]:
                try:
                    f.unlink()
                    pruned += 1
                except OSError:
                    pass

        if pruned > 0:
            logger.info("[teacher:%s] Pruned %d old session files", self.role, pruned)
        return pruned

    def close(self) -> None:
        """Close the teacher's worktree and terminal. Idempotent."""
        if self._mgr:
            if self._review_terminal:
                try:
                    self._mgr.close_terminal(self._review_terminal)
                except Exception:
                    pass
                self._review_terminal = None
            if self.worktree_path:
                try:
                    self._mgr.close_worktree(self.worktree_path)
                except Exception:
                    pass
                self.worktree_path = None
            self._booted = False

    def __enter__(self):
        if not self._booted:
            self.boot()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _call_review_model_via_hermes(
        self, prompt: str, system_prompt: Optional[str] = None, **kwargs
    ) -> str:
        """Run the review via Hermes in the teacher's Orca terminal.

        Reads the teacher's SOUL.md from ``~/.hermes/profiles/teacher-{role}/SOUL.md``,
        composes a full review prompt with lens instructions + task + student output,
        and runs ``hermes chat --yolo --quiet --max-turns 1`` in a disposable
        Orca terminal within the teacher's worktree.

        The ``AdversarialReviewer`` handles JSON parsing, circuit breaker logic,
        and verdict determination — this method just provides the raw model response.

        Args:
            prompt: The user prompt (lens instructions + task + student output).
            system_prompt: JSON format instructions from AdversarialReviewer.
            **kwargs: Passed through (timeout, etc.).

        Returns:
            Raw model response string (JSON expected by caller).
        """
        # ── Read SOUL.md (repo config/profiles primary, ~/.hermes override) ──
        soul = load_soul(f"teacher-{self.role}")
        if not soul:
            soul = f"You are a {self.role} reviewer. Output ONLY a JSON object."

        # ── Compose full review prompt ──────────────────────────────────
        full_prompt = (
            f"{soul}\n\n"
            f"---\n\n"
            f"## System Instructions\n{system_prompt or ''}\n\n"
            f"## Review Task\n{prompt}"
        )

        # ── Run Hermes in the teacher's worktree ────────────────────────
        review_bead = f"review-{self.role}-{uuid.uuid4().hex[:8]}"
        timeout_ms = kwargs.get("timeout", DEFAULT_REVIEW_TIMEOUT) * 1000

        if not self._mgr or not self.worktree_path:
            raise TeacherError("Teacher not booted — cannot run Hermes review")

        try:
            response = self._mgr.run_hermes(
                worktree_path=self.worktree_path,
                bead=review_bead,
                task=full_prompt,
                timeout_ms=timeout_ms,
                handle=self._review_terminal,
            )
            return response
        except OrcaUnavailableError as e:
            logger.error(
                "[teacher:%s] Hermes review failed: %s", self.role, e
            )
            raise  # Let AdversarialReviewer handle the error


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    """Run a teacher from the command line.

    Usage:
        python teacher.py --role cto
        python teacher.py --role coo

    This is the entry point for running inside a teacher worktree terminal.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Teacher worktree lifecycle")
    parser.add_argument("--role", required=True, choices=["cto", "coo"],
                        help="Teacher role: cto (correctness+security) or coo (completeness)")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL,
                        help="Seconds between bookbag poll checks")
    parser.add_argument("--once", action="store_true",
                        help="Run one review cycle and exit (instead of infinite loop)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    teacher = TeacherWorktree(args.role, poll_interval=args.poll_interval)
    teacher.boot()

    if args.once:
        count = teacher.review_cycle()
        print(f"[teacher:{args.role}] Reviewed {count} bookbags")
    else:
        print(f"[teacher:{args.role}] Starting infinite review loop...")
        try:
            teacher.run_loop()
        except KeyboardInterrupt:
            print(f"\n[teacher:{args.role}] Shutting down...")
            teacher.close()


if __name__ == "__main__":
    main()
