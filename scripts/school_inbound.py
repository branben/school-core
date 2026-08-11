#!/usr/bin/env python3
"""AgentMail inbound handler for the Agent-School Principal.

Reads threads in the AgentMail inbox, applies /approve /reject /fix
commands, and labels threads school-processed or school-error.

The AgentMail transport (key + inbox resolution, request helper, logging) is
shared with the notifier + poller via agentmail_client — this handler no
longer carries its own copy of SCHOOL_INBOX / _req / key resolution.

Note: ``src/agentmail_poller.py`` is the active inbound path (cronned every
2 min). This script is the legacy handler, kept operational for manual runs /
dry-runs and for scanning every inbox (it polls all inboxes, not just the
control-plane one, so a reply is caught wherever it lands).

Env:
    AGENTMAIL_API_KEY        (required) — user-scoped key (am_us_…)
    AGENTMAIL_SCHOOL_INBOX   (optional) — preferred inbox (polled first)
    AGENTMAIL_INBOX          (optional) — inbox id to poll. Defaults to
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
import logging
import os
import re
import sys
from pathlib import Path

# Make the shared client importable whether run from repo root or scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmail_client import (
    configure_logging,
    req,
)

logger = logging.getLogger("school_inbound")

INBOX_ID_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@agentmail\.to$")
BeadID_RE = re.compile(r"^[a-zA-Z0-9_\-.]{3,64}$")


def _resolve_inboxes() -> list[str]:
    """All inbox ids to poll, AGENTMAIL_SCHOOL_INBOX first when present.

    Scanning every inbox (not just the control-plane one) means a human reply
    is caught wherever it lands — including threads whose outbound notification
    was sent to a different inbox before routing was aligned.
    """
    env = os.environ.get("AGENTMAIL_INBOX")
    res = req("GET", "/inboxes")
    inboxes = res.get("inboxes", []) if isinstance(res, dict) else []
    if not inboxes:
        raise RuntimeError("no AgentMail inboxes available")
    ids = [ib.get("inbox_id") for ib in inboxes if ib.get("inbox_id")]
    if env and INBOX_ID_RE.match(env) and env in ids:
        return [env]
    school = os.environ.get("AGENTMAIL_SCHOOL_INBOX", "").strip()
    if school and school in ids:
        return [school] + [i for i in ids if i != school]
    return ids


def _get_unprocessed_threads(inbox: str, limit: int = 100):
    """Return threads that lack school-processed AND school-error labels."""
    res = req(
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
    return req("GET", f"/inboxes/{inbox}/threads/{thread_id}")


def _patch_thread(
    inbox: str,
    thread_id: str,
    add_labels: list[str],
    remove_labels: list[str] | None = None,
):
    body: dict = {"add_labels": add_labels}
    if remove_labels:
        body["remove_labels"] = remove_labels
    req("PATCH", f"/inboxes/{inbox}/threads/{thread_id}", body)


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
    configure_logging()
    parser = argparse.ArgumentParser(description="Agent-School inbound command handler")
    parser.add_argument("--dry-run", action="store_true", help="Show what would act, but don't apply")
    args = parser.parse_args()

    try:
        inboxes = _resolve_inboxes()
    except Exception as e:
        logger.error("cannot resolve inbox: %s", e)
        return 1

    acted = 0
    for inbox in inboxes:
        try:
            threads = _get_unprocessed_threads(inbox)
        except Exception as e:
            logger.warning("thread list failed for %s: %s", inbox, e)
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
                logger.warning("thread %s: %s", thread_id, e)

    if acted == 0 and not args.dry_run:
        print("nothing to act on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
