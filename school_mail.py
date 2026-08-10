#!/usr/bin/env python3
"""AgentMail notify client for the Agent-School Principal.

Sends a verdict notification to the human operator after the Principal
reconciles a bead's two-judge review. Plain stdlib REST call against the
AgentMail /v0 API (no SDK, no MCP) — see the `agentmail-rest` skill for the
verified contract. Best-effort: any failure degrades to stderr + printing so
the serve-mode Principal never crashes mid-reconcile.

Env:
    AGENTMAIL_API_KEY   (required) — user-scoped key (am_us_…)
    AGENTMAIL_INBOX      (optional) — destination inbox id (email address).
                          Defaults to the inbox resolved from the key via
                          GET /v0/inboxes (first inbox), so it works with no
                          config in the common single-inbox case.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://api.agentmail.to"
API_PREFIX = "/v0"

# Agent-School human-in-the-loop control-plane inbox. Verdict notifications are
# sent here so the human can reply with /approve /reject /fix and the inbound
# poller (scripts/school_inbound.py) — which watches this same inbox — can act.
# Keep this in sync with SCHOOL_INBOX in scripts/school_inbound.py.
SCHOOL_INBOX = "REDACTED@REDACTED.invalid"


def _headers() -> dict:
    key = os.environ.get("AGENTMAIL_API_KEY")
    if not key:
        raise RuntimeError("AGENTMAIL_API_KEY not set")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{API_PREFIX}{path}", data=data, headers=_headers(), method=method
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _default_inbox() -> str:
    """Resolve the destination inbox id from the key (first inbox)."""
    inbox = os.environ.get("AGENTMAIL_INBOX")
    if inbox:
        return inbox
    res = _req("GET", "/inboxes")
    inboxes = res.get("inboxes", []) if isinstance(res, dict) else []
    if not inboxes:
        raise RuntimeError("no AgentMail inboxes available")
    return inboxes[0]["inbox_id"]


def _resolve_dest_inbox() -> str:
    """Destination inbox for verdict notifications.

    Prefer the Agent-School control-plane inbox (SCHOOL_INBOX) so the human's
    replies land where the inbound poller (scripts/school_inbound.py) watches.
    Falls back to AGENTMAIL_INBOX env, then the first available inbox.
    """
    inbox = os.environ.get("AGENTMAIL_INBOX")
    if inbox:
        return inbox
    res = _req("GET", "/inboxes")
    inboxes = res.get("inboxes", []) if isinstance(res, dict) else []
    if not inboxes:
        raise RuntimeError("no AgentMail inboxes available")
    for ib in inboxes:
        if ib.get("inbox_id") == SCHOOL_INBOX:
            return ib["inbox_id"]
    return inboxes[0]["inbox_id"]


def _format_findings_table(findings: list[dict]) -> str:
    """Render qodo pre-merge findings + review findings as a compact table."""
    if not findings:
        return "  (none)"
    lines = ["  file:line                  sev  message"]
    for f in findings:
        file = f.get("file", "?")
        line = f.get("line", "?")
        sev = f.get("severity", f.get("level", "?"))
        msg = f.get("message", f.get("description", ""))[:60]
        lines.append(f"  {file}:{line:<20} {sev:<4} {msg}")
    return "\n".join(lines)


def notify_verdict(
    bead: str,
    accepted: bool,
    cto_verdict: str,
    coo_verdict: str,
    summary: str = "",
    repo: str = "__global__",
    qodo_findings: list[dict] | None = None,
    qodo_status: str | None = None,
    cto_findings: list | None = None,
    coo_findings: list | None = None,
) -> bool:
    """Send a two-judge verdict notification to the human operator.

    Returns True if the send succeeded, False if it degraded (missing key,
    network error, etc.) — never raises, so the Principal's reconcile loop
    stays resilient.

    Sends a rubber-stamp card: the human reads the verdict + qodo output
    and replies with /approve /reject /fix. The AgentMail inbound poller
    (school_mail_poller.py) processes the reply and triggers merge/dispose.
    """
    try:
        inbox = _resolve_dest_inbox()
    except Exception as e:
        sys.stderr.write(f"[school_mail] notify skipped (no inbox: {e})\n")
        return False

    mark = "✅ ACCEPTED" if accepted else "❌ REJECTED"

    # Build the rubber-stamp card body
    parts = [
        f"[Agent-School] {bead} — {mark}",
        "",
        f"Repo: {repo}",
        f"CTO: {cto_verdict}  |  COO: {coo_verdict}",
    ]

    if qodo_status:
        parts.append("")
        parts.append(f"Qodo pre-merge: {qodo_status}")
        if qodo_findings:
            parts.append("  Findings (real bugs only):")
            parts.append(_format_findings_table(qodo_findings))

    if cto_findings or coo_findings:
        parts.append("")
        parts.append("Review findings:")
        if cto_findings:
            parts.append(f"  CTO: {_format_findings_table(cto_findings)}")
        if coo_findings:
            parts.append(f"  COO: {_format_findings_table(coo_findings)}")

    if summary:
        parts.append("")
        parts.append(summary)

    parts.append("")
    parts.append("Commands: /approve  /reject  /fix <note>")

    text = "\n".join(parts)

    try:
        _req(
            "POST",
            f"/inboxes/{inbox}/messages/send",
            {"to": [inbox], "subject": f"[school] {bead} — {mark}", "text": text},
        )
        return True
    except urllib.error.URLError as e:
        sys.stderr.write(f"[school_mail] send failed (network): {e}\n")
    except Exception as e:  # noqa: BLE001 — degrade, never crash the principal
        sys.stderr.write(f"[school_mail] send failed: {e}\n")
    return False


def notify_issue_alert(
    issue_number: int,
    title: str,
    status: str,          # "retry" | "school-failed"
    error: str = "",
    repo: str = "__global__",
    attempt: int = 1,
    retry_limit: int = 2,
) -> bool:
    """Alert the human operator when the Agent-School hits a problem issue.

    - ``status == "retry"`` — transient failure (gateway/Orca hiccup); the issue
      will be retried automatically on the next cycle (attempt N of retry_limit).
    - ``status == "school-failed"`` — retry budget exhausted; the issue is
      labeled school-failed and needs human review.

    Fired exactly once per issue per transition (the bridge retries at most
    once), so it never spams. Best-effort: missing key, network errors, etc.
    degrade to stderr + False — never raises, so the bridge stays resilient.
    Uses the same AgentMail channel + control-plane inbox as
    :func:`notify_verdict`.
    """
    try:
        inbox = _resolve_dest_inbox()
    except Exception as e:
        sys.stderr.write(f"[school_mail] notify skipped (no inbox: {e})\n")
        return False

    if status == "school-failed":
        mark = "❌ SCHOOL-FAILED"
        subject = f"[school] #{issue_number} — SCHOOL-FAILED"
        parts = [
            f"[Agent-School] Issue #{issue_number} — {mark}",
            "",
            f"Repo: {repo}",
            f"Title: {title}",
            "",
            f"Retry budget exhausted ({attempt}/{retry_limit}) — the school could",
            "not complete this issue. Needs human review.",
        ]
    else:  # "retry"
        mark = "🔄 RETRY PENDING"
        subject = f"[school] #{issue_number} — RETRY ({attempt}/{retry_limit})"
        parts = [
            f"[Agent-School] Issue #{issue_number} — {mark}",
            "",
            f"Repo: {repo}",
            f"Title: {title}",
            "",
            f"Transient failure on attempt {attempt}/{retry_limit} — will be",
            "retried automatically on the next cycle.",
        ]

    if error:
        parts += ["", f"Error: {str(error)[:500]}"]

    parts += ["", f"Issue: https://github.com/{repo}/issues/{issue_number}"]
    text = "\n".join(parts)

    try:
        _req(
            "POST",
            f"/inboxes/{inbox}/messages/send",
            {"to": [inbox], "subject": subject, "text": text},
        )
        return True
    except urllib.error.URLError as e:
        sys.stderr.write(f"[school_mail] send failed (network): {e}\n")
    except Exception as e:  # noqa: BLE001 — degrade, never crash the bridge
        sys.stderr.write(f"[school_mail] send failed: {e}\n")
    return False


if __name__ == "__main__":
    ok = notify_verdict(
        "demo-bead", False, "PASS", "FAIL", "teacher-coo found incomplete acceptance criteria"
    )
    print("notify_verdict ->", ok)
