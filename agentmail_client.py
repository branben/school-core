#!/usr/bin/env python3
"""Shared AgentMail REST client for the Agent-School notification layer.

Single source of truth for the AgentMail /v0 transport used by the three
surfaces that used to each carry their own copy of this plumbing:

  - ``school_mail.py``            — outbound notify (verdicts, issue alerts, CI)
  - ``src/agentmail_poller.py``   — inbound /approve /reject /fix poller (cronned)
  - ``scripts/school_inbound.py`` — legacy inbound handler

Consolidating the key resolution, inbox resolution, and request helper here
means the notifier and the poller cannot drift apart again (previously three
``SCHOOL_INBOX`` constants and three ``_req`` helpers lived in sync-by-comment).

Env:
    AGENTMAIL_API_KEY        (required) — user-scoped key (am_us_…).
    AGENTMAIL_SCHOOL_INBOX   (optional) — the Agent-School control-plane inbox
                              where verdict cards and human replies live.
    AGENTMAIL_INBOX          (optional) — fallback destination/poll inbox.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

BASE = "https://api.agentmail.to"
API_PREFIX = "/v0"

logger = logging.getLogger("agentmail_client")


def configure_logging() -> None:
    """Set up timestamped console logging (safe to call more than once)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


def resolve_api_key() -> str:
    """Return the AgentMail API key: env first, then the hermes config.yaml.

    The poller runs as a standalone cron with no login shell, so
    AGENTMAIL_API_KEY is frequently absent from the environment. In that case
    read it from the agentmail MCP server URL in ~/.hermes/config.yaml (the
    value there is real plaintext on disk; only Hermes' tool output redacts
    it). Mirrors the historical poller behavior.
    """
    key = os.environ.get("AGENTMAIL_API_KEY")
    if key:
        return key
    cfg_path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        raise RuntimeError(
            "AGENTMAIL_API_KEY not set and ~/.hermes/config.yaml unreadable"
        )
    for pattern in (
        r"mcp\.agentmail\.to/mcp\?apiKey=([^&\s\"']+)",
        r"mcp\.agentmail\.to[^\n]*apiKey=(am_us_[A-Za-z0-9]+)",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    raise RuntimeError(
        "AGENTMAIL_API_KEY not set and not found in ~/.hermes/config.yaml"
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {resolve_api_key()}",
        "Content-Type": "application/json",
    }


def req(method: str, path: str, body=None):
    """Make a /v0 API call. Returns the parsed JSON response (or {})."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{API_PREFIX}{path}", data=data, headers=_headers(), method=method
    )
    with urllib.request.urlopen(request, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def resolve_dest_inbox() -> str:
    """Resolve the destination inbox: SCHOOL, then env fallback, then first.

    Preference order:
      1. ``AGENTMAIL_SCHOOL_INBOX`` — the control-plane mailbox the inbound
         poller watches, so outbound cards and human replies share a thread.
      2. ``AGENTMAIL_INBOX`` — legacy/override destination.
      3. The first inbox returned by the API (single-inbox setups work with
         no config at all).

    Previously each module hardcoded a (redacted) ``SCHOOL_INBOX`` constant
    and a near-identical resolver; that triplication is gone.
    """
    for name in ("AGENTMAIL_SCHOOL_INBOX", "AGENTMAIL_INBOX"):
        inbox = os.environ.get(name, "").strip()
        if inbox:
            return inbox
    res = req("GET", "/inboxes")
    inboxes = res.get("inboxes", []) if isinstance(res, dict) else []
    if not inboxes:
        raise RuntimeError("no AgentMail inboxes available")
    inbox_id = inboxes[0].get("inbox_id")
    if not inbox_id:
        raise RuntimeError("AgentMail returned an inbox without an id")
    return inbox_id
