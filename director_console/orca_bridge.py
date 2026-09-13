"""Orca bridge — queue → dispatch.

When the Director says "dispatch", this module:

  1. Generates a typed contract (packet.yaml) from the issue + linked KC plan.
  2. Claims the bd issue (prevents double-dispatch).
  3. Creates an Orca task using the packet as the task spec.
  4. Dispatches the task to a target agent.
  5. Logs the run to ``director-runs/``.

Atomicity guarantee (key debate finding):
  If task creation fails after claim, the claim rolls back. A claim-timeout
  mechanism auto-unclaims stale claims (older than N minutes). A
  reconciliation protocol polls Orca for task existence after dispatch
  and heals split-brain states.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from director_console.contract import (
    Intent,
    Packet,
    Provenance,
    Scope,
    ValidationError,
    validate,
)
from director_console.queue import (
    Bucket,
    QueueItem,
    resolve_kc_plan,
)

logger = logging.getLogger(__name__)

ORCA_BIN = os.environ.get("ORCA_BIN", "orca")
CLAIM_TIMEOUT_MIN = int(os.environ.get("CLAIM_TIMEOUT_MIN", "30"))
RUN_LOG_DIR = os.environ.get(
    "DIRECTOR_RUN_LOG_DIR",
    os.path.join(os.path.expanduser("~"), ".hermes", "director-runs"),
)


class DispatchError(Exception):
    """Raised when dispatch fails after all retries."""


class DoubleDispatchError(Exception):
    """Raised when an issue is already claimed."""


@dataclass
class RunRecord:
    """A single dispatch attempt logged to ``director-runs/``."""
    run_id: str
    issue_id: str
    packet_yaml_path: str
    orca_task_id: Optional[str] = None
    issued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    dispatched_at: Optional[str] = None
    completed_at: Optional[str] = None
    verdict: Optional[str] = None
    false_refutation: bool = False
    settled: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[var-annotated]

    def save(self) -> str:
        """Append this record to the run log and return the file path."""
        import dataclasses

        os.makedirs(RUN_LOG_DIR, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m")
        log_path = os.path.join(RUN_LOG_DIR, f"{date_str}.jsonl")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(self.to_dict(), default=str) + "\n")
        return log_path


import dataclasses  # noqa: E402 — needed for dataclasses.asdict in save()


def _run_cmd(
    cmd: list[str],
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run a CLI command, returning the completed process."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _orca_task_exists(task_id: str) -> bool:
    """Check whether an Orca task with the given id exists."""
    proc = _run_cmd([ORCA_BIN, "orchestration", "task-get", "--task", task_id], timeout=30)
    return proc.returncode == 0 and "not found" not in proc.stdout.lower()


def _unclaim_issue(issue_id: str) -> None:
    """Roll back a claim on a bd issue."""
    proc = _run_cmd(["bd", "update", issue_id, "--unclaim"], timeout=30)
    if proc.returncode != 0:
        logger.error("Failed to unclaim issue %s: %s", issue_id, proc.stderr.strip())
    else:
        logger.info("Unclaimed issue %s (rollback)", issue_id)


def _is_claim_stale(issue: dict[str, Any]) -> bool:
    """Check if an issue has a stale claim (older than CLAIM_TIMEOUT_MIN)."""
    claimed_at_str = issue.get("claimed_at", "")
    if not claimed_at_str:
        return False
    try:
        claimed_at = datetime.fromisoformat(claimed_at_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - claimed_at > timedelta(minutes=CLAIM_TIMEOUT_MIN)
    except (ValueError, TypeError):
        return False


def build_packet(item: QueueItem) -> Packet:
    """Build a typed contract from a QueueItem."""
    body = item.extra.get("description", "") if item.extra else ""
    anchor, _ = resolve_kc_plan(body)

    return Packet(
        intent=Intent(
            what=item.title,
            why=f"Director dispatched: {item.recommended_action}",
            anchor=anchor or "",
        ),
        acceptance_criteria=[f"Resolve issue {item.id}"],
        scope=Scope(
            files_in_scope=[],
            files_out_of_scope=[],
            max_lines_changed=500,
        ),
        provenance=Provenance(
            bd_issue=item.id,
            kc_plan=anchor or "",
        ),
        verdict_criteria=["Issue can be closed with clean CI"],
    )


def dispatch_issue(
    item: QueueItem,
    target_handle: str,
    *,
    skip_claim_check: bool = False,
) -> RunRecord:
    """Dispatch a queued issue to an agent.

    Args:
        item: The queued issue to dispatch.
        target_handle: The agent handle to dispatch to (e.g. "student-coder").
        skip_claim_check: If True, skip the double-dispatch guard (for testing).

    Raises:
        DoubleDispatchError: If the issue is already claimed.
        DispatchError: If dispatch fails after rollback.
    """
    import uuid

    run_id = str(uuid.uuid4())
    packet = build_packet(item)

    # Validate the packet
    validate(packet)

    # Save the packet YAML for the run log
    os.makedirs(RUN_LOG_DIR, exist_ok=True)
    packet_path = os.path.join(RUN_LOG_DIR, f"packet-{run_id}.yaml")
    with open(packet_path, "w", encoding="utf-8") as f:
        f.write(packet.to_yaml())

    record = RunRecord(
        run_id=run_id,
        issue_id=item.id,
        packet_yaml_path=packet_path,
    )

    # Step 1: Claim the bd issue (prevents double-dispatch)
    if not skip_claim_check:
        claim_proc = _run_cmd(["bd", "update", item.id, "--claim"], timeout=30)
        if claim_proc.returncode != 0:
            error_msg = claim_proc.stderr.strip()
            if "already claimed" in error_msg.lower():
                raise DoubleDispatchError(
                    f"Issue {item.id} is already claimed: {error_msg}"
                )
            raise DispatchError(f"Failed to claim issue {item.id}: {error_msg}")
        logger.info("Claimed issue %s", item.id)

    # Step 2: Create Orca task (with rollback on failure)
    orca_task_id: Optional[str] = None
    try:
        packet_json = packet.to_json()
        create_proc = _run_cmd(
            [ORCA_BIN, "orchestration", "task-create", "--spec", packet_json],
            timeout=60,
        )
        if create_proc.returncode != 0:
            raise DispatchError(
                f"Orca task-create failed: {create_proc.stderr.strip()}"
            )
        # Parse task id from stdout
        try:
            task_data = json.loads(create_proc.stdout)
            orca_task_id = task_data.get("id", task_data.get("task_id"))
        except (json.JSONDecodeError, AttributeError):
            # Fallback: treat the whole stdout as the task id
            orca_task_id = create_proc.stdout.strip().splitlines()[0] if create_proc.stdout.strip() else None

        if not orca_task_id:
            raise DispatchError("Orca task-create returned no task id")

        record.orca_task_id = orca_task_id
        record.dispatched_at = datetime.now(timezone.utc).isoformat()
        logger.info("Created Orca task %s for issue %s", orca_task_id, item.id)

    except DispatchError:
        # Rollback: unclaim the issue
        _unclaim_issue(item.id)
        record.notes = "dispatch_failed: task creation failed, claim rolled back"
        record.save()
        raise

    # Step 3: Dispatch the task to the target agent
    dispatch_proc = _run_cmd(
        [ORCA_BIN, "orchestration", "dispatch", "--task", orca_task_id, "--to", target_handle, "--inject"],
        timeout=60,
    )
    if dispatch_proc.returncode != 0:
        # Dispatch failed but task exists — log for reconciliation
        logger.error(
            "Dispatch failed for task %s: %s",
            orca_task_id,
            dispatch_proc.stderr.strip(),
        )
        record.notes = f"dispatch_failed: {dispatch_proc.stderr.strip()}"
        record.save()
        raise DispatchError(
            f"Orca dispatch failed for task {orca_task_id}: {dispatch_proc.stderr.strip()}"
        )

    logger.info(
        "Dispatched task %s to %s for issue %s",
        orca_task_id,
        target_handle,
        item.id,
    )

    # Step 4: Reconciliation — verify task exists after a short delay
    time.sleep(1)
    if not _orca_task_exists(orca_task_id):
        logger.warning(
            "Reconciliation: task %s not found after dispatch — marking as failed",
            orca_task_id,
        )
        record.notes = "reconciliation_failed: task not found after dispatch"
        record.save()
        raise DispatchError(
            f"Reconciliation failed: task {orca_task_id} not found after dispatch"
        )

    record.settled = True
    record.verdict = "dispatched"
    record.save()
    return record
