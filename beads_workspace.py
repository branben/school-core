"""Local Beads workspace projection and explicitly confirmed mutations."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent
ACTIVITY_PATH = REPO_ROOT / "data" / "activity_log.json"
ISSUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
ACTIVE_STAGES = {"clone", "boot", "hermes_thinking", "bookbag_written", "teachers_reviewing"}
ALLOWED_ASSIGNEES = {"Brandon Bennett", "Freebuff", "Hermes"}
ALLOWED_STATUSES = {"open", "in_progress", "blocked", "closed"}
ALLOWED_TYPES = {"task", "bug", "feature", "epic", "chore", "decision"}
PREVIEW_TTL_SECONDS = 120
ACTIVE_WINDOW = timedelta(minutes=5)


class WorkspaceError(Exception):
    """Base error for safe tracker responses."""


class WorkspaceValidationError(WorkspaceError):
    """The requested operation is outside the tracker contract."""


class WorkspaceConflict(WorkspaceError):
    """The issue changed after its operation was previewed."""


class WorkspaceService:
    """Runs allowlisted ``bd`` commands in one local repository checkout."""

    def __init__(self, root: Path = REPO_ROOT, runner: Callable | None = None):
        self.root = Path(root).resolve()
        self.runner = runner or self._run_bd
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _run_bd(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bd", *args], cwd=self.root, capture_output=True, text=True,
            check=False, timeout=20,
        )

    def list_issues(self) -> list[dict]:
        result = self.runner([
            "--sandbox", "list", "--all", "--include-gates", "--limit", "0",
            "--no-pager", "--readonly", "--json",
        ])
        if result.returncode:
            raise WorkspaceError("Beads could not be read. Check that `bd` is available and the local database is healthy.")
        try:
            raw = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceError("Beads returned invalid JSON.") from exc
        if not isinstance(raw, list):
            raise WorkspaceError("Beads returned an unexpected issue list.")
        return [item for item in raw if isinstance(item, dict)]

    def workspace(self) -> dict:
        issues = self.list_issues()
        activity = read_activity(ACTIVITY_PATH)
        return build_workspace(issues, activity)

    def preview(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise WorkspaceValidationError("Expected a JSON object.")
        action = payload.get("action")
        before: dict | None = None
        parent_before = None
        if action == "create":
            parent_id = payload.get("parent")
            if parent_id:
                if not isinstance(parent_id, str) or not ISSUE_ID_RE.fullmatch(parent_id):
                    raise WorkspaceValidationError("Invalid project Beads ID.")
                parent_before = next((issue for issue in self.list_issues() if issue.get("id") == parent_id), None)
                if parent_before is None or parent_before.get("issue_type") != "epic" or parent_before.get("status") == "closed":
                    raise WorkspaceValidationError("Choose an existing, open epic as the project.")
            args, summary = self._create_args(payload)
            target_id = None
        else:
            target_id = payload.get("issue_id")
            if not isinstance(target_id, str) or not ISSUE_ID_RE.fullmatch(target_id):
                raise WorkspaceValidationError("Invalid Beads issue ID.")
            before = next((issue for issue in self.list_issues() if issue.get("id") == target_id), None)
            if before is None:
                raise WorkspaceValidationError("Beads issue was not found.")
            args, summary = self._update_args(action, target_id, payload, before)

        token = secrets.token_urlsafe(24)
        expires_at = time.monotonic() + PREVIEW_TTL_SECONDS
        pending = {
            "args": args,
            "issue_id": target_id,
            "before": before,
            "parent_id": payload.get("parent") if action == "create" else None,
            "parent_before": parent_before,
            "created": action == "create",
            "expires_at": expires_at,
        }
        with self._lock:
            self._pending[token] = pending
            self._prune_pending()
        return {"token": token, "summary": summary, "args": args, "expires_in": PREVIEW_TTL_SECONDS}

    def confirm(self, token: object) -> dict:
        if not isinstance(token, str) or len(token) > 128:
            raise WorkspaceValidationError("Invalid confirmation token.")
        with self._lock:
            pending = self._pending.pop(token, None)
        if pending is None or pending["expires_at"] < time.monotonic():
            raise WorkspaceConflict("That preview expired or was already used. Preview the change again.")

        parent_id = pending["parent_id"]
        if parent_id:
            parent = next((issue for issue in self.list_issues() if issue.get("id") == parent_id), None)
            if parent is None or _issue_fingerprint(parent) != _issue_fingerprint(pending["parent_before"]):
                raise WorkspaceConflict("The selected project changed after preview. Review a fresh preview before applying.")

        issue_id = pending["issue_id"]
        if issue_id is not None:
            current = next((issue for issue in self.list_issues() if issue.get("id") == issue_id), None)
            if current is None or _issue_fingerprint(current) != _issue_fingerprint(pending["before"]):
                raise WorkspaceConflict("The Beads issue changed after preview. Review a fresh preview before applying.")

        result = self.runner(pending["args"])
        if result.returncode:
            detail = (result.stderr or result.stdout or "bd command failed").strip()
            raise WorkspaceError(f"Beads did not apply the change: {detail[:240]}")
        return {"ok": True, "message": "Beads updated locally. No agent was launched and no remote sync was performed."}

    def _create_args(self, payload: dict) -> tuple[list[str], str]:
        title = _required_text(payload.get("title"), "Issue title", 200)
        description = _optional_text(payload.get("description"), "Description", 4000)
        issue_type = payload.get("issue_type", "task")
        if issue_type not in ALLOWED_TYPES:
            raise WorkspaceValidationError("Unsupported Beads issue type.")
        args = ["--sandbox", "create", title, "--description", description, "--type", issue_type, "--json"]
        parent = payload.get("parent")
        if parent:
            args.extend(["--parent", parent])
        return args, f"Create {issue_type}: {title}"

    def _update_args(self, action: object, issue_id: str, payload: dict, before: dict) -> tuple[list[str], str]:
        if action == "assign":
            assignee = payload.get("assignee")
            if assignee not in ALLOWED_ASSIGNEES:
                raise WorkspaceValidationError("Choose Brandon Bennett, Freebuff, or Hermes as the assignee.")
            return ["--sandbox", "update", issue_id, "--assignee", assignee], f"Assign {issue_id} to {assignee}. No agent will be launched."
        if action == "status":
            status = payload.get("status")
            if status not in ALLOWED_STATUSES:
                raise WorkspaceValidationError("Unsupported Beads status.")
            if status == "closed":
                return ["--sandbox", "close", issue_id, "--reason", "Closed from Beads Workspace"], f"Close {issue_id} in Beads."
            return ["--sandbox", "update", issue_id, "--status", status], f"Set {issue_id} to {status.replace('_', ' ')}."
        raise WorkspaceValidationError("Unsupported tracker action.")

    def _prune_pending(self) -> None:
        now = time.monotonic()
        self._pending = {key: value for key, value in self._pending.items() if value["expires_at"] >= now}


def _required_text(value: object, label: str, limit: int) -> str:
    text = _optional_text(value, label, limit)
    if not text:
        raise WorkspaceValidationError(f"{label} is required.")
    return text


def _optional_text(value: object, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise WorkspaceValidationError(f"{label} must be text.")
    text = value.strip()
    if len(text) > limit:
        raise WorkspaceValidationError(f"{label} is too long (max {limit} characters).")
    return text


def _issue_fingerprint(issue: dict | None) -> str:
    if issue is None:
        return ""
    return hashlib.sha256(json.dumps(issue, sort_keys=True, default=str).encode()).hexdigest()


def read_activity(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _project_for(issue: dict, by_id: dict[str, dict]) -> dict | None:
    parent_id = issue.get("parent")
    seen: set[str] = set()
    while isinstance(parent_id, str) and parent_id not in seen:
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if not parent:
            return None
        if parent.get("issue_type") == "epic":
            return parent
        parent_id = parent.get("parent")
    return None


def _activity_for(issue_id: str, entries: list[dict], now: datetime) -> dict:
    latest: tuple[datetime, dict] | None = None
    for entry in entries:
        if entry.get("type", entry.get("kind")) != "student_stage" or entry.get("bead") != issue_id:
            continue
        timestamp = _timestamp(entry.get("timestamp"))
        if timestamp is None or timestamp > now:
            continue
        if latest is None or timestamp >= latest[0]:
            latest = (timestamp, entry)
    if latest is None:
        return {"state": "unknown", "agent": None, "stage": None, "timestamp": None}
    timestamp, event = latest
    stage = event.get("stage")
    if stage not in ACTIVE_STAGES or now - timestamp > ACTIVE_WINDOW:
        return {"state": "unknown", "agent": None, "stage": None, "timestamp": event.get("timestamp")}
    agent = "Hermes" if stage == "hermes_thinking" else None
    return {
        "state": "recent_event",
        "agent": agent,
        "stage": stage,
        "timestamp": event.get("timestamp"),
    }


def build_workspace(issues: list[dict], activity: list[dict], *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    by_id = {str(item["id"]): item for item in issues if item.get("id")}
    projects = [item for item in issues if item.get("issue_type") == "epic"]
    projected: list[dict] = []
    for item in issues:
        if item.get("issue_type") == "epic":
            continue
        project = _project_for(item, by_id)
        status = item.get("status", "open")
        projected.append({
            "id": str(item.get("id", "")),
            "title": str(item.get("title", "Untitled issue")),
            "description": str(item.get("description", ""))[:1200],
            "status": status,
            "issue_type": str(item.get("issue_type", "task")),
            "priority": item.get("priority", 2),
            "assignee": item.get("assignee"),
            "parent": item.get("parent"),
            "project_id": project.get("id") if project else None,
            "project_title": project.get("title") if project else None,
            "updated_at": item.get("updated_at"),
            "activity": _activity_for(str(item.get("id", "")), activity, now),
        })
    projects_out = []
    for project in projects:
        children = [item for item in projected if item["project_id"] == project.get("id")]
        projects_out.append({
            "id": str(project.get("id", "")),
            "title": str(project.get("title", "Untitled project")),
            "status": project.get("status", "open"),
            "open_count": sum(1 for child in children if child["status"] != "closed"),
            "total_count": len(children),
        })
    issue_sort = lambda item: (str(item.get("status") == "closed"), str(item.get("id")))
    projected.sort(key=issue_sort)
    return {
        "issues": projected,
        "projects": sorted(projects_out, key=lambda item: item["title"].casefold()),
        "inbox": [item for item in projected if item["status"] == "open" and item["parent"] is None and not item["assignee"]],
        "todos": [item for item in projected if item["status"] == "open"],
        "in_progress": [item for item in projected if item["status"] == "in_progress"],
        "blocked": [item for item in projected if item["status"] == "blocked"],
        "closed": [item for item in projected if item["status"] == "closed"],
        "activity_available": any(item["activity"]["state"] == "recent_event" for item in projected),
        "refreshed_at": now.isoformat(),
        "refresh_interval_seconds": 10,
    }
