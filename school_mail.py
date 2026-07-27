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


def notify_verdict(
    bead: str,
    accepted: bool,
    cto_verdict: str,
    coo_verdict: str,
    summary: str = "",
    repo: str = "__global__",
) -> bool:
    """Send a two-judge verdict notification to the human operator.

    Returns True if the send succeeded, False if it degraded (missing key,
    network error, etc.) — never raises, so the Principal's reconcile loop
    stays resilient.

    Sends a FRESH thread per verdict (reply-on-thread is 400 on the
    user-scoped key). The body carries /approve /reject /fix command hints.
    """
    try:
        inbox = _default_inbox()
    except Exception as e:
        sys.stderr.write(f"[school_mail] notify skipped (no inbox: {e})\n")
        return False

    mark = "ACCEPTED ✅" if accepted else "REJECTED ❌"
    text = (
        f"Agent-School verdict [{repo}]\n"
        f"Bead: {bead}\n"
        f"Result: {mark}\n"
        f"CTO: {cto_verdict}  COO: {coo_verdict}\n"
        f"{summary}\n\n"
        f"Commands: /approve  /reject  /fix"
    )
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


if __name__ == "__main__":
    ok = notify_verdict(
        "demo-bead", False, "PASS", "FAIL", "teacher-coo found incomplete acceptance criteria"
    )
    print("notify_verdict ->", ok)
