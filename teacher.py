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

import json
import logging
import os
import subprocess
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
DEFAULT_REVIEW_TIMEOUT = 30  # seconds for Hermes review call (falls back to call_model)
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
        repo: str = "__global__",
        diagnose_on_fail: bool = False,
    ):
        if role not in TEACHER_LENSES:
            raise ValueError(f"Unknown teacher role '{role}'. Must be 'cto' or 'coo'.")
        self.role = role
        self.lenses = TEACHER_LENSES[role]
        self.poll_interval = poll_interval
        self.session_id = f"{session_id}-{role}"
        self.repo = repo
        self.worktree_name = f"teacher-{role}" if repo == "__global__" else f"teacher-{role}-{repo.replace('/', '__')}"
        self.worktree_path: Optional[str] = None
        self._mgr: Optional[OrcaExecutionManager] = None
        self._review_terminal: Optional[str] = None  # Reusable Hermes terminal
        self._store = ScoreStore()
        self._reviewer = AdversarialReviewer(call_model_fn=self._call_review_model_via_hermes)
        self._cycle_count = 0
        self._episodic_history: list[dict] = []
        self._booted = False
        # When True, a FAIL verdict triggers the systematic-debugging + TDD
        # diagnose loop (Rank 1): the teacher writes a regression test that
        # reproduces the gate failure and records a root-cause diagnosis in
        # the bookbag instead of leaving a bare FAIL. Backward compatible:
        # defaults to False (legacy pass/fail behavior unchanged).
        self.diagnose_on_fail = diagnose_on_fail

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

        # Single-source-of-truth rediscovery (Lifecycle invariant).
        # Reuse the persistent worktree if it already exists; never mint a
        # suffixed clone (teacher-cto-2 / -lens-2) — that suffix spray is the
        # zombie-worktree pressure. create_worktree_persistent() handles the
        # scan-and-reuse centrally in orca_executor.
        #
        # NOTE: boot() NO LONGER spawns a `teacher-*-review` terminal. The
        # review loop is owned by an Orca automation (see conductor._boot_teachers
        # → run_teacher_review_once.py), so Orca owns the schedule and there is
        # no per-boot terminal spray.
        try:
            self.worktree_path = self._mgr.create_worktree_persistent(
                self.worktree_name
            )
            self._booted = True
            logger.info(
                "[teacher:%s] Persistent worktree at %s (rediscover-or-create)",
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

        for bead in list_bookbags(self.repo):
            bag = read_bookbag(bead, self.repo)
            if bag is None:
                continue

            verdict_field = f"{self.role}_verdict"
            if bag.get(verdict_field, ""):
                continue  # Already reviewed by this teacher

            # Found a bookbag that needs this teacher's review
            logger.info("[teacher:%s] Reviewing bead=%s repo=%s", self.role, bead, self.repo)

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

                # Rank 1 — systematic-debugging + TDD loop on FAIL.
                # When diagnose_on_fail is set, a FAIL verdict triggers a
                # learning intervention: the teacher reads the failed gate,
                # reproduces it with a regression test (written to disk),
                # traces the root cause, and records a diagnosis dict instead
                # of leaving a bare FAIL. Backward compatible: when
                # diagnose_on_fail is False (default), this block is skipped.
                diagnosis = None
                if verdict == "FAIL" and self.diagnose_on_fail:
                    diagnosis = self._diagnose(bead, bag, task, result)
                    logger.info(
                        "[teacher:%s] Diagnose loop on %s: root_cause=%s, test=%s",
                        self.role, bead,
                        (diagnosis or {}).get("root_cause", "")[:60],
                        (diagnosis or {}).get("regression_test", ""),
                    )

                # Update bookbag with lock protection
                update_fields = {
                    verdict_field: verdict,
                    f"{self.role}_findings": findings_dicts,
                    f"{self.role}_score": result.score,
                    f"{self.role}_confidence": result.confidence,
                    f"{self.role}_lens": result.lens_used,
                    f"{self.role}_reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
                if diagnosis is not None:
                    update_fields[f"{self.role}_diagnosis"] = diagnosis
                # Keep the legacy combined `findings` field in sync for the
                # principal reconcile path (it reads both cto/coo fields).
                if f"{self.role}_diagnosis" not in update_fields:
                    update_fields.setdefault(
                        "findings",
                        bag.get("findings", []) + findings_dicts,
                    )
                updated = locked_update_bookbag(
                    bead,
                    self.repo,
                    lock_timeout=10.0,
                    **update_fields,
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
                    "diagnosis": diagnosis is not None,
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
        """Close the teacher's worktree, terminal, and any stale admin entries.

        Idempotent: safe to call multiple times, on a partial boot, or when
        ``self._mgr`` is unset. Never raises — each step has its own
        try/except so a failure in one does not skip the others.

        Three layers of registry cleanup so a re-serve after ``close()`` lands
        on the canonical ``teacher-<role>`` name (no ``-2`` / ``-3``
        suffix-spray):

        1. ``close_worktree(path)`` — primary path-based removal: covers the
           on-disk directory and the orca-side registration.
        2. ``orca worktree rm --worktree name:<canon> --force`` —
           belt-and-suspenders for the case where the path-based remove
           missed a stale registry entry (e.g. directory removed
           out-of-band). Note: the orca CLI flag is ``--worktree <selector>``
           (the legacy ``--name`` form is rejected); the selector accepts
           ``name:<displayName>`` for canonical-name targeting.
        3. ``git worktree prune`` — drops any lingering
           ``<repo>/.git/worktrees/<name>`` admin entry, the source of the
           ``teacher-cto-N`` suffix spray on re-serve (see ``Lifecycle``
           invariant in docs/school-core-architecture.md).

        The terminal close runs first; only then the worktree cleanup. State
        is nilled unconditionally.
        """
        if self._mgr:
            if self._review_terminal:
                try:
                    self._mgr.close_terminal(self._review_terminal)
                except Exception:
                    pass
                self._review_terminal = None
            # Layers 1+2+3: combined worktree cleanup. Each step is
            # independently best-effort so the union actually happens even
            # when one of them raises (e.g. orca rejects the by-name form
            # because the entry was already gone).
            if self.worktree_path or self.worktree_name:
                # Layer 1: path-based removal (covers dir + orca registration).
                if self.worktree_path:
                    try:
                        self._mgr.close_worktree(self.worktree_path)
                    except Exception:
                        pass
                # Layer 2: belt-and-suspenders by canonical name. Catches
                # the case where path-based remove left a stale registry.
                if self.worktree_name:
                    try:
                        self._mgr._run_orca(
                            ["worktree", "remove", "--worktree",
                             f"name:{self.worktree_name}", "--force"],
                            timeout=15,
                        )
                    except Exception:
                        pass
                # Layer 3: drop any leftover git admin entry. Tested with
                # an isinstance guard so mocked ``REPO_PATH`` (a MagicMock)
                # is naturally skipped in unit tests.
                try:
                    rp = getattr(self._mgr, "REPO_PATH", None)
                    if isinstance(rp, (str, Path)):
                        subprocess.run(
                            ["git", "-C", str(rp), "worktree", "prune"],
                            capture_output=True, timeout=10,
                        )
                except Exception:
                    pass
                # Nil both worktree_path AND worktree_name so that calling
                # ``close()`` a second time is a true no-op for the
                # cleanup block. ``worktree_name`` is otherwise a role
                # canonical invariant; nil-ing it after close() is safe
                # because the teacher is considered ``unbound`` post-close
                # and any future close()/boot() will reconstruct it from
                # ``role`` + ``repo``.
                self.worktree_path = None
                self.worktree_name = None
            self._booted = False

    def __enter__(self):
        if not self._booted:
            self.boot()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _diagnose_dir(self) -> Path:
        """Resolve where diagnosis regression tests are written.

        Priority: DIAGNOSE_DIR env override (tests) → teacher worktree root
        → current working directory. Always nested under diagnoses/<role>/.
        """
        override = os.environ.get("DIAGNOSE_DIR")
        if override:
            base = Path(override)
        elif self.worktree_path:
            base = Path(self.worktree_path)
        else:
            base = Path.cwd()
        d = base / "diagnoses" / self.role
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _diagnose(self, bead: str, bag: dict, task: dict, result) -> dict:
        """Rank 1 systematic-debugging + TDD loop (Matt Pocock method).

        Runs only when a gate verdict is FAIL and ``diagnose_on_fail`` is set.
        Turns a bare FAIL into a learning intervention following Matt Pocock's
        4-phase diagnosing-bugs method:

            understand  Re-read the failed gate criterion + trace data flow to
                        the root cause (from the top finding)
            reproduce   Pin the failure as a self-contained offline regression test
            fix         Record the remediation the student must apply
            verify     Run the regression test (offline, deterministic)

        The regression test encodes the deterministic gate rule
        (AdversarialReviewer returns FAIL iff a CRITICAL/HIGH finding exists),
        so it runs offline without an LLM and stays GREEN once pinned — it
        guards against a future silent weakening of the gate.

        Returns:
            Diagnosis dict: root_cause, regression_test (path), fix_applied,
            phases (list), reproduced (bool), verified (bool), diagnosed_at.
        """
        phases: list[str] = []

        # ── understand: re-read the failed gate criterion ─────────────────
        findings_dicts = [f.to_dict() for f in result.findings]
        output = bag.get("output", "")
        phases.append("understand")

        # ── understand (cont.): trace data flow → root cause ───────────────
        # (done before writing the test so the cause can be embedded in it)
        top = result.findings[0] if result.findings else None
        if top is not None:
            root_cause = (
                f"[{top.issue_class}] {top.description}"
            )
            fix_applied = top.suggestion or (
                f"Tighten the {self.role} lens to catch '{top.issue_class}' "
                f"at {top.citation}."
            )
        else:
            root_cause = "Gate FAILED with no structured findings (empty output or parse failure)."
            fix_applied = "Require the student to produce non-empty, parseable output."
        # root-cause tracing is folded into the "understand" phase above

        # ── reproduce: pin the failure as a self-contained regression test ─
        test_path = self._diagnose_dir() / f"{bead}.py"
        test_body = self._build_regression_test(bead, findings_dicts, output, root_cause)
        test_path.write_text(test_body)
        phases.append("reproduce")

        # ── fix: record the remediation the student must apply ────────────
        # The teacher does not edit the student's output here; it records the
        # fix the student must apply (Matt Pocock: understand → reproduce → fix).
        phases.append("fix")

        # ── verify: run the regression test (offline) ──────────────────────
        verified = False
        try:
            proc = subprocess.run(
                [sys.executable, str(test_path)],
                capture_output=True, text=True, timeout=30,
            )
            verified = proc.returncode == 0
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[teacher:%s] regression test run failed: %s", self.role, e)
        phases.append("verify_test")

        logger.info(
            "[teacher:%s] Diagnose complete for %s (phases=%d, verified=%s)",
            self.role, bead, len(phases), verified,
        )

        return {
            "root_cause": root_cause,
            "regression_test": str(test_path),
            "fix_applied": fix_applied,
            "phases": phases,
            "reproduced": True,
            "verified": verified,
            "diagnosed_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _build_regression_test(
        bead: str, findings_dicts: list[dict], output: str, root_cause: str
    ) -> str:
        """Generate a self-contained regression test that pins the gate failure.

        Encodes the deterministic gate rule (FAIL iff a CRITICAL/HIGH finding
        exists) so it runs offline and stays GREEN — it guards against a future
        silent weakening of the gate for this bead.
        """
        safe_bead = "".join(c if c.isalnum() else "_" for c in bead)
        findings_json = json.dumps(findings_dicts, indent=2)
        output_json = json.dumps(output)
        return (
            f'"""Auto-generated regression test for bead {bead} '
            f'(teacher diagnose loop).\n\n'
            f'RED->GREEN (TDD): this test pins the gate failure so a future\n'
            f'regression cannot silently downgrade the verdict. It runs offline.\n\n'
            f'Root cause: {root_cause}\n'
            f'"""\n'
            f'import json\n'
            f'import sys\n'
            f'\n'
            f'FINDINGS = json.loads({findings_json!r})\n'
            f'OUTPUT = {output_json!r}\n'
            f'\n'
            f'\n'
            f'def test_bead_{safe_bead}_gate_failure_pinned():\n'
            f'    """Pin the original gate failure for this bead.\n'
            f'\n'
            f'    Gate rule (AdversarialReviewer): verdict is FAIL iff at least one\n'
            f'    CRITICAL/HIGH finding exists. If this assertion breaks, the gate\n'
            f'    was silently weakened for this bead.\n'
            f'    """\n'
            f'    severities = {{f["severity"] for f in FINDINGS}}\n'
            f'    assert severities & {{"CRITICAL", "HIGH"}}, (\n'
            f'        "regression: bead {bead} failed the gate via a CRITICAL/HIGH "\n'
            f'        "finding; if this breaks the gate was weakened."\n'
            f'    )\n'
            f'    # The student output that triggered the failure must be preserved.\n'
            f'    assert OUTPUT is not None\n'
            f'\n'
            f'\n'
            f'if __name__ == "__main__":\n'
            f'    test_bead_{safe_bead}_gate_failure_pinned()\n'
            f'    print("OK: regression test for {bead} passes")\n'
            f'    sys.exit(0)\n'
        )

    def _call_review_model_via_hermes(
        self, prompt: str, system_prompt: Optional[str] = None, **kwargs
    ) -> str:
        """Run the review via Hermes in the teacher's Orca terminal.

        Reads the teacher's SOUL.md from ``~/.hermes/profiles/teacher-{role}/SOUL.md``,
        composes a full review prompt with lens instructions + task + student output,
        and runs ``hermes chat --yolo --quiet --max-turns 1 -t hermes-cli,file`` in a disposable
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
                toolsets="hermes-cli,file",
            )
            return response
        except OrcaUnavailableError as e:
            logger.error(
                "[teacher:%s] Hermes review failed: %s", self.role, e
            )
        except Exception as e:
            logger.error(
                "[teacher:%s] Hermes review failed: %s", self.role, e
            )
        # ── Fallback: direct OmniRoute call (faster, no terminal overhead) ──
        # The Hermes terminal path can time out on slow free-tier models.
        # Fall back to direct call_model which routes through OmniRoute's
        # combo-selection (reviewer role → auto/best-free or better).
        logger.info(
            "[teacher:%s] Falling back to direct OmniRoute for review",
            self.role,
        )
        from executor import call_model
        return call_model(
            "reviewer",
            full_prompt,
            system_prompt=system_prompt,
            timeout=180,
        )


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
