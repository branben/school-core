#!/usr/bin/env python3
"""AgentMail inbound handler for the Agent-School Principal.

Reads threads in the AgentMail inbox, applies /approve /reject /fix
commands, and labels threads school-processed or school-error.

Env:
    AGENTMAIL_API_KEY   (required) — user-scoped key (am_us_…)
    AGENTMAIL_INBOX     (optional) — inbox id to poll. Defaults to
                          the first inbox returned by GET /v0/inboxes.

Usage:
    python3 scripts/school_inbound.py            # acts on queued commands
    python3 scripts/school_inbound.py --dry-run  # shows what it would do

Exit codes: 0 = acted or nothing to act on; 1 = API error (cron will
retry next tick). Never crashes on a single malformed thread — label
and continue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

BASE = "https://api.agentmail.to"
API_PREFIX = "/v0"

INBOX_ID_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@agentmail\.to$")
BeadID_RE = re.compile(r"^[a-zA-Z0-9_\-.]{3,64}$")

# Agent-School human-in-the-loop control-plane inbox. Used as the default
# poll target when AGENTMAIL_INBOX is not set, since GET /inboxes does not
# return the school inbox first.
SCHOOL_INBOX = "REDACTED@REDACTED.invalid"


def _resolve_api_key() -> str:
    """API key from AGENTMAIL_API_KEY env, else the AgentMail MCP server
    URL configured in ~/.hermes/config.yaml (same key the harness injects)."""
    key = os.environ.get("AGENTMAIL_API_KEY")
    if key:
        return key
    try:
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        text = open(cfg_path).read()
        m = re.search(r"mcp\.agentmail\.to/mcp\?apiKey=([^&\s\"']+)", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    raise RuntimeError("AGENTMAIL_API_KEY not set (and not found in ~/.hermes/config.yaml)")


def _headers() -> dict:
    key = _resolve_api_key()
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{API_PREFIX}{path}", data=data, headers=_headers(), method=method
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _resolve_inboxes() -> list[str]:
    """All inbox ids to poll, SCHOOL_INBOX first when present.

    Scanning every inbox (not just the control-plane one) means a human reply
    is caught wherever it lands — including threads whose outbound notification
    was sent to a different inbox before routing was aligned.
    """
    env = os.environ.get("AGENTMAIL_INBOX")
    res = _req("GET", "/inboxes")
    inboxes = res.get("inboxes", []) if isinstance(res, dict) else []
    if not inboxes:
        raise RuntimeError("no AgentMail inboxes available")
    ids = [ib.get("inbox_id") for ib in inboxes if ib.get("inbox_id")]
    if env and INBOX_ID_RE.match(env) and env in ids:
        return [env]
    ordered = [i for i in ids if i == SCHOOL_INBOX] + [i for i in ids if i != SCHOOL_INBOX]
    return ordered


def _get_unprocessed_threads(inbox: str, limit: int = 100):
    """Return threads that lack school-processed AND school-error labels."""
    res = _req(
        "GET",
        f"/inboxes/{inbox}/threads?limit={limit}&label=unread",
    )
    threads = res.get("threads", []) if isinstance(res, dict) else []
    result = []
    for t in threads:
        labels = t.get("labels", [])
        if isinstance(labels, list) and (
            "school-processed" in labels or "school-error" in labels
        ):
            continue
        result.append(t)
    return result


def _get_thread(inbox: str, thread_id: str):
    return _req("GET", f"/inboxes/{inbox}/threads/{thread_id}")


def _patch_thread(
    inbox: str,
    thread_id: str,
    add_labels: list[str],
    remove_labels: list[str] | None = None,
):
    body: dict = {"add_labels": add_labels}
    if remove_labels:
        body["remove_labels"] = remove_labels
    _req("PATCH", f"/inboxes/{inbox}/threads/{thread_id}", body)


def _parse_command(body: str):
    """Return (command, bead) or None if no valid command found."""
    stripped = body.strip()
    if not stripped:
        return None
    m = re.match(r"^/(approve|reject|fix)\s+(\S+)", stripped)
    if not m:
        return None
    command, bead = m.group(1), m.group(2)
    if not BeadID_RE.match(bead):
        return None
    return command, bead


def _find_human_message(thread: dict) -> dict | None:
    """Return the latest message whose sender is NOT the inbox itself."""
    messages = thread.get("messages", [])
    inbox_id = thread.get("inbox_id", "")
    human = None
    for m in messages:
        sender = m.get("from", "")
        # Skip messages from the inbox/bot itself (contains inbox address)
        if inbox_id and inbox_id in sender:
            continue
        human = m
    return human


def _act_command(inbox: str, thread: dict, command: str, bead: str) -> str:
    """Apply the command to the bookbag. Returns an action string."""
    if command == "approve":
        _update_bookbag(bead, {"accepted": True, "human_verdict": "approved"})
        return f"/approve {bead} -> accepted"

    elif command == "reject":
        _update_bookbag(bead, {"accepted": False, "human_verdict": "rejected"})
        return f"/reject {bead} -> rejected"

    elif command == "fix":
        _update_bookbag(bead, {
            "accepted": False,
            "human_verdict": "fix_requested",
            "cto_verdict": None,
            "coo_verdict": None,
            "cto_score": 0,
            "coo_score": 0,
        })
        return f"/fix {bead} -> fix requested (next serve tick re-dispatches)"

    return f"unknown command: /{command} {bead}"


def _read_bookbag(bead: str) -> dict | None:
    """Read bookbag JSON from disk."""
    from pathlib import Path

    bookbag_dir = Path.home() / ".hermes" / "bookbag"
    path = bookbag_dir / f"{bead}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _update_bookbag(bead: str, updates: dict):
    """Apply updates to a bookbag JSON on disk."""
    from pathlib import Path

    bookbag_dir = Path.home() / ".hermes" / "bookbag"
    path = bookbag_dir / f"{bead}.json"
    if not path.exists():
        return
    try:
        bag = json.loads(path.read_text())
        bag.update(updates)
        bag["updated_at"] = _now_iso()
        path.write_text(json.dumps(bag, indent=2, ensure_ascii=False))
    except (json.JSONDecodeError, OSError):
        pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-School inbound command handler")
    parser.add_argument("--dry-run", action="store_true", help="Show what would act, but don't apply")
    args = parser.parse_args()

    try:
        inboxes = _resolve_inboxes()
    except Exception as e:
        sys.stderr.write(f"[school_inbound] cannot resolve inbox: {e}\n")
        return 1

    acted = 0
    for inbox in inboxes:
        try:
            threads = _get_unprocessed_threads(inbox)
        except Exception as e:
            sys.stderr.write(f"[school_inbound] thread list failed for {inbox}: {e}\n")
            continue

        for thread in threads:
            thread_id = thread.get("thread_id", "")
            try:
                full = _get_thread(inbox, thread_id)
                human_msg = _find_human_message(full)
                if not human_msg:
                    _patch_thread(inbox, thread_id, ["school-processed"])
                    continue
                # AgentMail returns message content in `text` (and `html`),
                # not `body` — read both so commands are never missed.
                body = human_msg.get("text") or human_msg.get("body") or ""
                cmd = _parse_command(body)
                if not cmd:
                    _patch_thread(inbox, thread_id, ["school-processed"])
                    continue

                if args.dry_run:
                    print(f"[dry-run] would act: {cmd[0]} {cmd[1]} (thread {thread_id})")
                    acted += 1
                    continue

                result = _act_command(inbox, full, cmd[0], cmd[1])
                label = "school-processed" if "ERROR" not in result else "school-error"
                try:
                    _patch_thread(inbox, thread_id, [label])
                except Exception:
                    pass
                print(result)
                acted += 1
            except Exception as e:
                try:
                    _patch_thread(inbox, thread_id, ["school-error"])
                except Exception:
                    pass
                sys.stderr.write(f"[school_inbound] thread {thread_id}: {e}\n")

    if acted == 0 and not args.dry_run:
        print("nothing to act on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
