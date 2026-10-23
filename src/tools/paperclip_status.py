"""One-way, opt-in Paperclip status mirror; GitHub remains authoritative."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 3
_STATUS_MAP = {
    "success": "in_review",
    "retry": "blocked",
    "error": "blocked",
    "crew_in_flight": "in_progress",
}
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an optional Paperclip bearer token across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_base_url(raw: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(raw.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if parsed.scheme == "https":
            pass
        elif parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                return None
        else:
            return None
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    except (ValueError, AttributeError):
        return None


def _valid_repo(repo: Any) -> bool:
    if not isinstance(repo, str) or repo.count("/") != 1:
        return False
    owner, name = repo.split("/")
    return all(_REPO_PART.fullmatch(part) and part not in {".", ".."} for part in (owner, name))


def _safe_pr_url(raw: Any, repo: str) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme == "https" and parsed.hostname == "github.com"
        and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment
        and len(parts) == 4 and "/".join(parts[:2]).lower() == repo.lower()
        and parts[2] == "pull" and parts[3].isdigit() and int(parts[3]) > 0
    ):
        return f"https://github.com/{repo}/pull/{int(parts[3])}"
    return None


class PaperclipStatusMirror:
    """Mirror final lifecycle state without importing task text or delegating work."""

    def __init__(
        self, *, api_url: str, company_id: str, project_id: str,
        api_key: str | None, enabled: str,
    ):
        self.base_url = _safe_base_url(api_url)
        self.company_id = company_id.strip()
        self.project_id = project_id.strip()
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.enabled = _truthy(enabled)
        self._open = self._open_default

    @staticmethod
    def _open_default(request: urllib.request.Request, timeout: float):
        opener = urllib.request.build_opener(_NoRedirect())
        return opener.open(request, timeout=timeout)

    def _request(self, method: str, path: str, payload: dict | None = None):
        assert self.base_url
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method,
        )
        with self._open(request, TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if not 200 <= status < 300:
                raise urllib.error.HTTPError(request.full_url, status, "Paperclip request failed", {}, None)
            raw = response.read()
        return json.loads(raw) if raw else None

    def _find(self, title: str, marker: str) -> dict | None:
        query = urllib.parse.urlencode({"q": title, "limit": 50})
        path = f"/api/companies/{urllib.parse.quote(self.company_id, safe='')}/issues?{query}"
        rows = self._request("GET", path)
        if isinstance(rows, dict):
            rows = rows.get("data", rows.get("issues", []))
        if not isinstance(rows, list):
            raise ValueError("Unexpected Paperclip issue-list response")
        matches = [
            row for row in rows
            if isinstance(row, dict) and row.get("title") == title
            and isinstance(row.get("description"), str)
            and marker in row["description"]
        ]
        if len(matches) > 1:
            raise ValueError("Duplicate Paperclip mirror identity")
        if matches and not isinstance(matches[0].get("id"), str):
            raise ValueError("Paperclip mirror response has no issue ID")
        return matches[0] if matches else None

    def _patch_existing(self, existing: dict, status: str, description: str) -> bool:
        # Never mutate a task unless it is already in the explicitly selected project.
        if existing.get("projectId") != self.project_id:
            return False
        issue_id = urllib.parse.quote(existing["id"], safe="")
        response = self._request("PATCH", f"/api/issues/{issue_id}", {
            "status": status,
            "description": description,
        })
        return (
            isinstance(response, dict)
            and response.get("id") == existing["id"]
            and response.get("projectId") == self.project_id
            and response.get("status") == status
        )

    def sync_result(self, repo: str, result: dict) -> str:
        if not self.enabled:
            return "disabled"
        number = result.get("issue_number") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict) or not _valid_repo(repo)
            or not isinstance(number, int) or isinstance(number, bool) or number <= 0
        ):
            return "invalid_source"
        result_status = result.get("status")
        if not isinstance(result_status, str):
            return "invalid_source"
        status = _STATUS_MAP.get(result_status)
        if status is None:
            return "ignored"
        if (
            not self.base_url or not _UUID.fullmatch(self.company_id)
            or not _UUID.fullmatch(self.project_id)
        ):
            return "failed"

        source_id = f"{repo}#{number}"
        marker = f"[school-core-source:{source_id}]"
        title = f"School-core status: {source_id}"
        source_url = f"https://github.com/{repo}/issues/{number}"
        lines = [marker, f"Source issue: {source_url}", f"School-core status: {status}"]
        pr_url = _safe_pr_url(result.get("pr_url"), repo)
        if pr_url:
            lines.append(f"Review PR: {pr_url}")
        payload = {
            "title": title,
            "description": "\n".join(lines),
            "status": status,
            "priority": "medium",
            "projectId": self.project_id,
        }
        company_path = f"/api/companies/{urllib.parse.quote(self.company_id, safe='')}/issues"
        try:
            existing = self._find(title, marker)
            if existing is not None:
                if not self._patch_existing(existing, status, payload["description"]):
                    return "failed"
                return "updated"
            try:
                created = self._request("POST", company_path, payload)
                if (
                    isinstance(created, dict) and isinstance(created.get("id"), str)
                    and created.get("title") == title and created.get("status") == status
                    and created.get("projectId") == self.project_id
                ):
                    return "created"
            except Exception:
                # POST may have committed before a connection failed. Reconcile,
                # but never blindly repeat the non-idempotent create.
                existing = self._find(title, marker)
                if existing is not None:
                    if self._patch_existing(existing, status, payload["description"]):
                        return "reconciled"
            return "failed"
        except Exception:
            return "failed"


def sync_paperclip_results(repo: str, results: list[dict]) -> list[str]:
    """Mirror returned bridge outcomes best-effort; never affects bridge verdicts."""
    if not _truthy(os.environ.get("SCHOOL_CORE_PAPERCLIP_MIRROR")):
        return []
    mirror = PaperclipStatusMirror(
        api_url=os.environ.get("PAPERCLIP_API_URL", ""),
        company_id=os.environ.get("PAPERCLIP_COMPANY_ID", ""),
        project_id=os.environ.get("PAPERCLIP_PROJECT_ID", ""),
        api_key=os.environ.get("PAPERCLIP_API_KEY"),
        enabled=os.environ.get("SCHOOL_CORE_PAPERCLIP_MIRROR", ""),
    )
    outcomes = []
    for result in results:
        try:
            outcomes.append(mirror.sync_result(repo, result))
        except Exception:
            # A malformed result is isolated from remaining projections and the bridge.
            outcomes.append("failed")
    failures = sum(outcome == "failed" for outcome in outcomes)
    if failures:
        sys.stderr.write(f"[paperclip_status] {failures} status mirror request(s) failed; school-core state is unchanged\n")
    return outcomes


def mirror_paperclip_status(repo: str, result: dict) -> str:
    """Environment-configured single-result helper, useful for focused checks."""
    if not _truthy(os.environ.get("SCHOOL_CORE_PAPERCLIP_MIRROR")):
        return "disabled"
    mirror = PaperclipStatusMirror(
        api_url=os.environ.get("PAPERCLIP_API_URL", ""),
        company_id=os.environ.get("PAPERCLIP_COMPANY_ID", ""),
        project_id=os.environ.get("PAPERCLIP_PROJECT_ID", ""),
        api_key=os.environ.get("PAPERCLIP_API_KEY"),
        enabled=os.environ.get("SCHOOL_CORE_PAPERCLIP_MIRROR", ""),
    )
    return mirror.sync_result(repo, result)
