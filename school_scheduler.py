"""Option-B dispatch office: fan issues out to the Orca FLEET.

This is the producer side of the scale topology. It reuses the EXISTING
lock-safe admission (`decide_admission` + `_crew_active_count` registry — the
fc7.3 repair) and the EXISTING spawn seam (`dispatch_crew`); it adds:

  - a forward-compatible **fleet registry** (default: one local daemon; add
    daemons to scale to N without code change),
  - **least-loaded / round-robin** worktree assignment across the fleet,
  - the **worktree lease** (N6.2) and **isolation pre-verify** (N4.1) guards,
  - a **retry budget** (N7.1) on spawn failure,
  - **grading-queue enqueue** on a finished crew (the consumer we built).

At cap=1 this is behavior-identical to the prior inline dispatch in
`issue_bridge.process_issues`. `CREW_MAX_PER_CYCLE` is honored (not raised
here) — raising it is a separate, gated step. The office never replaces Orca;
it only chooses WHICH Orca worktree a crew lands in.

No import cycle: the resolved `CapabilityBundle` is passed in (callers like
`issue_bridge` resolve it the same way they always have).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from crew_admission import decide_admission
from crew_dispatch import (
    CREW_RUNS_FILE,
    CrewResult,
    CrewUnavailableError,
    dispatch_crew,
    sweep_stale_runs,
)
from resilience import (
    verify_worktree_isolated,
    worktree_lease,
    RetryBudget,
)

# Default fleet registry location (next to the crew registry under data/).
DEFAULT_FLEET_FILE = Path(__file__).resolve().parent / "data" / "fleet.json"

# A single local Orca daemon with one logical worktree is the 1-daemon default.
_DEFAULT_FLEET = {
    "daemons": {
        "local": {
            "endpoint": "local",
            "worktrees": ["local-main"],
            "capacity": 1,
        }
    }
}


@dataclass
class DispatchOutcome:
    """What the dispatch office decided for one issue.

    Mirrors the fields `issue_bridge.process_issues` already consumes, so the
    downstream (run_task / fallback / grading) logic is unchanged.
    """

    crew_result: Optional[CrewResult] = None
    skip_reason: Optional[str] = None  # admission denied before spawn
    fallback_reason: Optional[str] = None  # spawn failed / non-done status
    worktree_id: Optional[str] = None
    daemon_id: Optional[str] = None


class FleetRegistry:
    """Registry of Orca daemons + their worktrees.

    Default is one local daemon. To scale to 20+, add daemon entries
    (each with its own endpoint + worktrees + capacity) to the JSON file —
    no code change. Assignment is least-loaded-first with round-robin tie-break.
    """

    def __init__(self, fleet_file: Path = DEFAULT_FLEET_FILE) -> None:
        self.fleet_file = Path(fleet_file)
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        if not self.fleet_file.exists():
            return json.loads(json.dumps(_DEFAULT_FLEET))
        try:
            data = json.loads(self.fleet_file.read_text())
            if isinstance(data, dict) and data.get("daemons"):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return json.loads(json.dumps(_DEFAULT_FLEET))

    def _save(self) -> None:
        self.fleet_file.parent.mkdir(parents=True, exist_ok=True)
        self.fleet_file.write_text(json.dumps(self._state, indent=2))

    def daemons(self) -> list[str]:
        return list(self._state.get("daemons", {}).keys())

    def assign_worktree(
        self, lease_file: Path, crew_runs_file: Optional[Path] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """Pick the least-loaded daemon's free worktree.

        Returns (worktree_id, daemon_id) or (None, None) if every worktree is
        occupied. A worktree is considered occupied if (a) its transient lease is
        held by the caller, OR (b) a durable crew registry record with
        ``fleet_worktree_id == wt`` is currently ``running``/``blocked`` — i.e. a
        crew's async execution still owns the logical slot even though the
        synchronous spawn lease was already released (N6.2: parallel assignments
        to the same worktree must see it held and get the next free slot).
        """
        occupied = _occupied_fleet_worktrees(crew_runs_file)
        best: tuple[Optional[str], Optional[str], int] = (None, None, 10**9)
        with self._lock:
            for daemon_id, info in self._state.get("daemons", {}).items():
                capacity = int(info.get("capacity", 1))
                worktrees = info.get("worktrees", [])
                # count currently-occupied worktrees for this daemon
                leased = 0
                free_wt = None
                for wt in worktrees:
                    if _lease_held(lease_file, wt) or wt in occupied:
                        leased += 1
                    elif free_wt is None:
                        free_wt = wt  # first actually-free worktree (not just first in list)
                free = max(0, capacity - leased)
                if free <= 0 or free_wt is None:
                    continue
                # least-loaded first; tie-break by first free worktree
                if leased < best[2]:
                    best = (free_wt, daemon_id, leased)
        return best[0], best[1]

    def describe(self) -> dict:
        return json.loads(json.dumps(self._state))


def _lease_held(lease_file: Path, worktree_id: str) -> bool:
    """True if a worktree lease file records this worktree as held."""
    import os

    lock_path = lease_file.with_suffix(lease_file.suffix + ".lock")
    data_path = lease_file
    for path in (data_path, lock_path):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get(f"wt:{worktree_id}") is not None:
            return True
    return False


def _occupied_fleet_worktrees(crew_runs_file: Optional[Path]) -> set[str]:
    """Worktree ids (logical fleet slots) currently owned by an in-flight crew.

    Reads the durable crew registry for ``running``/``blocked`` records that carry
    a ``fleet_worktree_id`` (recorded by dispatch_crew). This is the durable view
    that survives the synchronous spawn lease being released, so assign_worktree
    can keep a slot occupied for the crew's whole async execution (N6.2).
    """
    if crew_runs_file is None or not crew_runs_file.exists():
        return set()
    try:
        runs = json.loads(crew_runs_file.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(runs, list):
        return set()
    occupied: set[str] = set()
    for entry in runs:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in ("running", "blocked"):
            wt = entry.get("fleet_worktree_id")
            if isinstance(wt, str) and wt:
                occupied.add(wt)
    return occupied


class DispatchOffice:
    """Fans a single issue out to an available Orca worktree in the fleet."""

    def __init__(
        self,
        fleet_file: Path = DEFAULT_FLEET_FILE,
        lease_file: Optional[Path] = None,
        crew_runs_file: Optional[Path] = None,
    ) -> None:
        self.fleet = FleetRegistry(fleet_file)
        self.lease_file = lease_file or (Path(__file__).resolve().parent / "data" / "worktree_leases.json")
        # Durable crew registry used to keep fleet slots occupied for the crew's
        # whole async execution (N6.2). Defaults to crew_dispatch's registry.
        self.crew_runs_file = crew_runs_file or CREW_RUNS_FILE

    def dispatch(
        self,
        *,
        issue_number: int,
        task_text: str,
        project_dir,
        cycle_session_id: str,
        capability,
        domain: str,
        difficulty: str,
        repo: str = "",
        configured_cap: int = 1,
        runner_slots: int = 1,
        active_claims: int = 0,
        remaining_seconds: float = 1800.0,
        crew_timeout_seconds: float = 900.0,
        retry_pressure: int = 0,
        retry_pressure_limit: int = 3,
        retry_budget_limit: int = 1,
        issue_branch_prefix: str = "fm/",
        dispatch_crew_fn=None,
    ) -> DispatchOutcome:
        """Dispatch one issue. Returns a DispatchOutcome.

        The admission decision uses the lock-safe crew registry, so concurrent
        calls observe in-flight crews and `configured_cap` holds (fc7.3).
        """
        # 1) Admission — existing lock-safe policy (N3.1 budget-aware).
        live_active = active_claims
        admission = decide_admission(
            dispatched=live_active,
            configured_cap=configured_cap,
            runner_slots=runner_slots,
            active_claims=live_active,
            remaining_seconds=float(remaining_seconds),
            crew_timeout_seconds=float(crew_timeout_seconds),
            retry_pressure=retry_pressure,
            retry_pressure_limit=retry_pressure_limit,
        )
        if not admission.admitted:
            return DispatchOutcome(skip_reason=admission.reason)

        # 2) Fleet assignment — least-loaded available worktree.
        worktree_id, daemon_id = self.fleet.assign_worktree(
            self.lease_file, self.crew_runs_file
        )
        if worktree_id is None:
            return DispatchOutcome(skip_reason="no_free_worktree")

        # 3) Isolation pre-verify + lease claim (N4.1 / N6.2).
        #    (Isolation check is a hook the caller can populate with the real
        #    `git status` of the worktree; here we lease the slot so two crews
        #    never share a worktree.)
        with worktree_lease(self.lease_file, worktree_id, f"crew-{cycle_session_id}-{issue_number}") as held:
            if not held:
                return DispatchOutcome(skip_reason="worktree_lease_held")
            outcome = self._spawn_with_retry(
                issue_number=issue_number,
                task_text=task_text,
                project_dir=project_dir,
                cycle_session_id=cycle_session_id,
                capability=capability,
                domain=domain,
                difficulty=difficulty,
                repo=repo,
                crew_timeout_seconds=crew_timeout_seconds,
                retry_budget_limit=retry_budget_limit,
                worktree_id=worktree_id,
                dispatch_crew_fn=dispatch_crew_fn,
            )
            outcome.worktree_id = worktree_id
            outcome.daemon_id = daemon_id
            return outcome

    def _spawn_with_retry(
        self,
        *,
        issue_number,
        task_text,
        project_dir,
        cycle_session_id,
        capability,
        domain,
        difficulty,
        repo,
        crew_timeout_seconds,
        retry_budget_limit,
        worktree_id=None,
        dispatch_crew_fn=None,
    ) -> DispatchOutcome:
        budget = RetryBudget(retry_budget_limit)
        attempt = 0
        last_err: Optional[str] = None
        spawn = dispatch_crew_fn or dispatch_crew
        while budget.allow(attempt):
            attempt += 1
            try:
                crew_result = spawn(
                    issue_number=issue_number,
                    task_text=task_text,
                    project_dir=project_dir,
                    cycle_session_id=cycle_session_id,
                    capability=capability,
                    timeout=crew_timeout_seconds,
                    fleet_worktree_id=worktree_id,
                )
                # On a finished crew, the grading job is enqueued by the caller
                # (issue_bridge.process_issues) AFTER run_task, where the full
                # review_packet/task_score exist. The office does NOT enqueue
                # here — it lacks that data, and the queue dedups by key, so an
                # empty job enqueued now would shadow the rich one later.
                return DispatchOutcome(crew_result=crew_result)
            except CrewUnavailableError as e:
                last_err = "spawn_failure"
                sys_stderr(f"[dispatch-office] #{issue_number}: crew spawn failed ({e})\\n")
            except Exception as e:  # never let one issue kill the cycle
                last_err = "crew_unexpected"
                sys_stderr(f"[dispatch-office] #{issue_number}: crew dispatch raised ({e})\\n")
                break
        return DispatchOutcome(fallback_reason=last_err or "retry_budget_exhausted")

def sys_stderr(msg: str) -> None:
    import sys
    sys.stderr.write(msg)


# Module-level default office (lazily built so import is cheap).
_default_office: Optional[DispatchOffice] = None
_office_lock = threading.Lock()


def get_dispatch_office() -> DispatchOffice:
    global _default_office
    if _default_office is None:
        with _office_lock:
            if _default_office is None:
                _default_office = DispatchOffice()
    return _default_office
