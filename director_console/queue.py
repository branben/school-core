"""Classification logic for the Director Queue.

Reads ``bd ready --json`` output and projects each issue into one of
five attention buckets (borrowed from loopx, extended with needs_triage
for planless issues — a key finding from the design debate).

Buckets:
  ready_to_work      — open, no blockers, linked KC plan is active
  waiting_on_external — blocked by dependency or human approval
  agent_in_flight    — contract issued, subagent running
  needs_review       — agent reported done, awaiting Director verification
  needs_triage       — open issue with NO linked KC plan (don't drop these)
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


KC_ROOT = "/Users/brandonbennett/Documents/KnowledgeCore"
PLAN_ANCHOR_RE = re.compile(r"anchor:\s*(plan-[a-z0-9-]+)")
KC_LINK_RE = re.compile(
    r"KnowledgeCore[\\/].*?\.md|"
    r"\.handoffs[\\/]|"
    r"docs[\\/]plans[\\/]"
)


class Bucket(str, Enum):
    READY_TO_WORK = "ready_to_work"
    WAITING_ON_EXTERNAL = "waiting_on_external"
    AGENT_IN_FLIGHT = "agent_in_flight"
    NEEDS_REVIEW = "needs_review"
    NEEDS_TRIAGE = "needs_triage"


BUCKET_LABELS = {
    Bucket.READY_TO_WORK: "Ready to Work",
    Bucket.WAITING_ON_EXTERNAL: "Waiting on External",
    Bucket.AGENT_IN_FLIGHT: "Agent In Flight",
    Bucket.NEEDS_REVIEW: "Needs Review",
    Bucket.NEEDS_TRIAGE: "Needs Triage",
}


@dataclass
class QueueItem:
    id: str
    title: str
    status: str
    priority: int
    issue_type: str
    bucket: Bucket
    recommended_action: str
    updated_at: str
    dependency_count: int = 0
    dependent_count: int = 0
    comment_count: int = 0
    kc_plan_anchor: Optional[str] = None
    kc_plan_active: Optional[bool] = None
    blast_radius: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "issue_type": self.issue_type,
            "bucket": self.bucket.value,
            "recommended_action": self.recommended_action,
            "updated_at": self.updated_at,
            "dependency_count": self.dependency_count,
            "dependent_count": self.dependent_count,
            "comment_count": self.comment_count,
            "kc_plan_anchor": self.kc_plan_anchor,
            "kc_plan_active": self.kc_plan_active,
        }
        if self.blast_radius is not None:
            d["blast_radius"] = self.blast_radius
        return d


def fetch_ready_issues() -> list[dict[str, Any]]:
    """Call ``bd ready --json`` and return the parsed list.

    Returns an empty list on any failure (bd missing, non-JSON, etc.).
    """
    try:
        result = subprocess.run(
            ["bd", "ready", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, list):
        return []

    return data


def resolve_kc_plan(issue_body: str) -> tuple[Optional[str], Optional[bool]]:
    """Parse an issue body for a linked KC plan anchor.

    Returns (anchor, is_active).  is_active is None when the plan
    cannot be found in the vault (treated as "unknown" — the item
    goes to needs_triage).
    """
    if not issue_body:
        return None, None

    m = PLAN_ANCHOR_RE.search(issue_body)
    if not m:
        return None, None

    anchor = m.group(1)
    return anchor, _is_plan_active(anchor)


def _is_plan_active(anchor: str) -> Optional[bool]:
    """Check whether a KC plan with the given anchor is status:active.

    Returns None if the plan file cannot be found.
    """
    import os

    plans_dir = os.path.join(KC_ROOT, "docs", "plans")
    if not os.path.isdir(plans_dir):
        return None

    # Fast path: scan filenames for the anchor.
    for fname in os.listdir(plans_dir):
        if anchor in fname and fname.endswith(".md"):
            fpath = os.path.join(plans_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    head = f.read(2000)
            except OSError:
                return None
            if re.search(r"^status:\s*active", head, re.MULTILINE):
                return True
            if re.search(r"^status:\s*archived", head, re.MULTILINE):
                return False
            return None  # plan found but no status field

    return None  # plan not found in vault


def classify_issue(issue: dict[str, Any]) -> Bucket:
    """Project a single bd issue into a queue bucket.

    Classification order matters:
      1. Closed/done → excluded by ``bd ready`` already.
      2. Has unresolved dependencies → waiting_on_external.
      3. Has a linked KC plan that is active → ready_to_work.
      4. No linked KC plan at all → needs_triage (key debate finding).
    """
    status = issue.get("status", "open")
    dep_count = issue.get("dependency_count", 0)

    # Blocked by upstream dependency.
    if dep_count and dep_count > 0:
        return Bucket.WAITING_ON_EXTERNAL

    # Check for linked KC plan.
    body = issue.get("description", "") or ""
    anchor, plan_active = resolve_kc_plan(body)

    if anchor is None:
        # No plan linked — don't silently drop it.
        return Bucket.NEEDS_TRIAGE

    if plan_active is True:
        return Bucket.READY_TO_WORK

    # Plan linked but inactive or not found — still needs triage.
    return Bucket.NEEDS_TRIAGE


def recommended_action(bucket: Bucket, issue: dict[str, Any]) -> str:
    """Emit a one-line recommended action for a queued issue."""
    actions = {
        Bucket.READY_TO_WORK: "Issue contract, dispatch agent",
        Bucket.WAITING_ON_EXTERNAL: "Resolve dependency or escalate to human",
        Bucket.AGENT_IN_FLIGHT: "Check run-log, verify Sentinel verdict",
        Bucket.NEEDS_REVIEW: "Verify CI green, bd close if clean",
        Bucket.NEEDS_TRIAGE: "Link a KC plan or decide scope before dispatch",
    }
    return actions.get(bucket, "Review")


def _parse_updated(raw: str) -> datetime:
    """Best-effort parse of an ISO timestamp for sorting."""
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def build_queue(
    issues: list[dict[str, Any]],
    include_context: bool = False,
    repo_path: str = "/Users/brandonbennett/school-core",
) -> list[QueueItem]:
    """Transform raw bd issues into sorted QueueItems."""
    from director_console.ripwire_client import blast_radius

    items: list[QueueItem] = []

    for issue in issues:
        bucket = classify_issue(issue)
        body = issue.get("description", "") or ""
        anchor, plan_active = resolve_kc_plan(body)

        blast = None
        if include_context:
            # Use title keywords as the ripwire query.
            keywords = issue.get("title", "")
            blast = blast_radius(repo_path, keywords)

        item = QueueItem(
            id=issue.get("id", "unknown"),
            title=issue.get("title", "Untitled"),
            status=issue.get("status", "open"),
            priority=issue.get("priority", 99),
            issue_type=issue.get("issue_type", "task"),
            bucket=bucket,
            recommended_action=recommended_action(bucket, issue),
            updated_at=issue.get("updated_at", ""),
            dependency_count=issue.get("dependency_count", 0),
            dependent_count=issue.get("dependent_count", 0),
            comment_count=issue.get("comment_count", 0),
            kc_plan_anchor=anchor,
            kc_plan_active=plan_active,
            blast_radius=blast,
        )
        items.append(item)

    # Sort by priority (lower number = higher priority), then updated_at.
    items.sort(key=lambda i: (i.priority, _parse_updated(i.updated_at)))
    return items
