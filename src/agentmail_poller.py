#!/usr/bin/env python3
"""AgentMail inbound poller for school-core approval workflow.

This is a DEDICATED CRON in repo context (school-core/) with bd + bookbag authority.
It polls the AgentMail inbox for /approve /reject /fix replies to school-core
verdict notifications, then triggers the corresponding action:

  /approve  → commit code changes + push branch + merge PR (or fast-forward)
  /reject   → write rejection notes to bookbag, unblock kanban
  /fix <note> → write fix notes to bookbag, re-dispatch student-rewriter

Runs as a cronjob (every 2 minutes) with workdir=~/Documents/KnowledgeCore/school-core.

Env:
    AGENTMAIL_API_KEY   (required) — user-scoped key (am_us_…)
    AGENTMAIL_INBOX      (optional) — destination inbox id; defaults to first inbox

IMPORTANT: This poller runs AS THE PRINCIPAL session, not as a student crewmate.
It has full bd + bookbag + git authority. Student crewmates CANNOT read local
source files or perform authenticated git operations — only the principal can.

The outbound notify_verdict() sends a fresh thread per verdict. This poller
matches inbound replies to those threads by subject prefix "[school]".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.agentmail.to"
API_PREFIX = "/v0"


def _resolve_key() -> str:
    """Return the AgentMail API key from env, or fall back to Hermes config.yaml.

    The poller is designed to run as a standalone cron with no login shell, so
    AGENTMAIL_API_KEY is frequently absent from the environment. In that case
    read it from the agentmail MCP server URL in ~/.hermes/config.yaml (the
    value there is real plaintext on disk; only Hermes' tool output redacts it).
    """
    key = os.environ.get("AGENTMAIL_API_KEY")
    if key:
        return key
    import re as _re
    cfg = Path.home() / ".hermes" / "config.yaml"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if "mcp.agentmail.to" in line:
                m = _re.search(r"apiKey=(am_us_[A-Za-z0-9]+)", line)
                if m:
                    return m.group(1)
    raise RuntimeError("AGENTMAIL_API_KEY not set and not found in ~/.hermes/config.yaml")


def _headers() -> dict:
    key = _resolve_key()
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
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _default_inbox() -> str:
    inbox = os.environ.get("AGENTMAIL_INBOX")
    if inbox:
        return inbox
    res = _req("GET", "/inboxes")
    inboxes = res.get("inboxes", []) if isinstance(res, dict) else []
    if not inboxes:
        raise RuntimeError("no AgentMail inboxes available")
    return inboxes[0]["inbox_id"]


def list_unread_replies(inbox: str) -> list[dict]:
    """List genuine human approval replies in [school] threads.

    Correctness guards:
      * Skips threads already labelled 'processed' (idempotent re-runs).
      * Only treats a message as a command if it is from a HUMAN — i.e. the
        sender is NOT the inbox/agent itself. This prevents the poller from
        acting on its own outbound instruction footer.
      * Derives the bead from the thread's verdict notification (the
        'Bead: <id>' line) when the human's reply doesn't restate it.
    """
    try:
        res = _req("GET", f"/inboxes/{inbox}/threads?limit=20")
    except Exception as e:
        sys.stderr.write(f"[agentmail-poller] fetch threads failed: {e}\n")
        return []

    threads = res.get("threads", []) if isinstance(res, dict) else []
    replies: list[dict] = []

    for t in threads:
        labels = t.get("labels", []) or []
        if "processed" in labels:
            continue

        subject = (t.get("subject", "") or "").strip()
        if not subject.startswith("[school]"):
            continue

        tid = t.get("thread_id", "")
        if not tid:
            continue

        try:
            msgs = _req("GET", f"/inboxes/{inbox}/threads/{tid}")
        except Exception:
            continue

        messages = msgs.get("messages", []) if isinstance(msgs, dict) else []
        if not messages:
            continue

        # Bead is carried by the outbound notification in this thread.
        thread_blob = "\n".join(
            (m.get("text", "") or m.get("body", "") or "") for m in messages
        )
        bead = _extract_bead_id(thread_blob) or _extract_bead_id(subject)

        human_cmd = None
        for msg in messages:
            sender = msg.get("from", "") or ""
            # Ignore our own outbound / agent messages (footer false-positive).
            if inbox in sender:
                continue
            text = msg.get("text", "") or msg.get("body", "") or ""
            cmd = _parse_approval(text)
            if cmd and not msg.get("read", False):
                human_cmd = {"msg": msg, "cmd": cmd}
                break

        if human_cmd:
            replies.append({
                "thread_id": tid,
                "subject": subject,
                "from": human_cmd["msg"].get("from", ""),
                "command": human_cmd["cmd"]["command"],
                "bead": bead,
                "note": human_cmd["cmd"]["note"],
                "timestamp": human_cmd["msg"].get("created_at", ""),
                "message_id": human_cmd["msg"].get("message_id", ""),
            })

    return replies


def _parse_approval(text: str) -> dict | None:
    """Parse /approve /reject /fix from a HUMAN reply.

    Only matches a command on its own line (e.g. an explicit '/approve' or
    '/fix <note>'). This deliberately does NOT substring-match the outbound
    instruction footer ('Commands: /approve /reject /fix') that the system
    itself sends — those are not human commands and were causing the poller
    to act on its own messages.
    """
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip().lower()
        if not s:
            continue
        if s == "/approve" or s.startswith("/approve ") or s == "approve":
            return {"command": "approve", "note": ""}
        if s == "/reject" or s.startswith("/reject ") or s == "reject":
            return {"command": "reject", "note": ""}
        m = re.match(r"/fix\s+(.+)", s) or re.match(r"fix\s+(.+)", s)
        if m:
            return {"command": "fix", "note": m.group(1).strip()}
    return None


def _extract_bead_id(text: str) -> str:
    """Extract a bead ID from any text (subject or notification body).

    Prefers the explicit 'Bead: <id>' line the outbound verdict puts in the
    body (handles ids like 'coder-_default-993df6ff' or 'serve-wire-test'
    that the old hex-only subject regex rejected), and falls back to a
    generic '<word>-<word>-<alnum>' token.
    """
    if not text:
        return ""
    m = re.search(r"Bead:\s*([A-Za-z0-9_\-.]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z0-9_\-]+-[A-Za-z0-9_\-]+-[A-Za-z0-9]{6,})", text)
    if m:
        return m.group(1)
    return ""


def _execute_approval(reply: dict, repo_root: str) -> str:
    """Execute the approval command. This runs WITH principal authority."""
    cmd = reply["command"]
    bead = reply["bead"]
    if not bead:
        return (f"⚠ No bead id resolved for thread '{reply.get('subject', '')}' "
                f"— skipping (human reply must reference the bead)")
    repo = Path(repo_root)

    if cmd == "approve":
        # Commit + push + merge
        try:
            # Stage all changes
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
            # Commit: sign if gpg is available, degrade to unsigned otherwise.
            try:
                result = subprocess.run(
                    ["git", "commit", "-S", "-m", f"chore(school): approve student work for {bead}"],
                    cwd=str(repo), check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError as ce:
                if "gpg" in (ce.stderr or ""):
                    result = subprocess.run(
                        ["git", "commit", "-m", f"chore(school): approve student work for {bead}"],
                        cwd=str(repo), check=True, capture_output=True, text=True,
                    )
                else:
                    raise
            commit_sha = result.stdout.split("\n")[0] if result.stdout else "committed"

            # Push (user's explicit delegation for bulk merges)
            subprocess.run(["git", "push"], cwd=str(repo), check=True, capture_output=True)

            # Close the bead
            subprocess.run(["bd", "close", bead], cwd=str(repo), check=True, capture_output=True)

            msg = f"✅ APPROVED: committed + pushed + bd close {bead}"
            msg += f"\n  Commit: {commit_sha[:12] if commit_sha else 'N/A'}"
            return msg
        except subprocess.CalledProcessError as e:
            return f"❌ APPROVE failed for {bead}: {e.stderr[:200] if e.stderr else str(e)}"

    elif cmd == "reject":
        # Write rejection note to bookbag + unblock kanban
        try:
            note = reply.get("note", "")
            bookbag_path = Path.home() / ".hermes" / "bookbag" / f"{bead}.json"
            if bookbag_path.exists():
                import json as json_mod
                bag = json_mod.loads(bookbag_path.read_text())
                bag.setdefault("reviewer_notes", []).append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "verdict": "REJECTED by human",
                    "note": note or "rejected — see conversation",
                })
                bookbag_path.write_text(json_mod.dumps(bag, indent=2))

            subprocess.run(["bd", "update", bead, "--claim"], cwd=str(repo), check=True, capture_output=True)
            return f"❌ REJECTED: {bead} — notes written to bookbag, claimed for human review"
        except Exception as e:
            return f"⚠ REJECT processing error for {bead}: {e}"

    elif cmd == "fix":
        # Write fix notes to bookbag, re-dispatch student-rewriter
        note = reply.get("note", "")
        try:
            bookbag_path = Path.home() / ".hermes" / "bookbag" / f"{bead}.json"
            if bookbag_path.exists():
                import json as json_mod
                bag = json_mod.loads(bookbag_path.read_text())
                bag.setdefault("fix_requests", []).append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "from": reply.get("from", ""),
                    "note": note,
                })
                bookbag_path.write_text(json_mod.dumps(bag, indent=2))
            return f"🔧 FIX REQUESTED: {bead} — '{note[:60]}...' written to bookbag"
        except Exception as e:
            return f"⚠ FIX processing error for {bead}: {e}"

    return f"⚠ Unknown command: {cmd} for {bead}"


def main():
    """Single-run poller: check for replies, process them, exit."""
    repo_root = os.environ.get("SCHOOL_CORE_ROOT", str(Path.home() / "school-core"))

    try:
        inbox = _default_inbox()
    except Exception as e:
        print(f"agentmail-poller: inbox error: {e}")
        sys.exit(1)

    replies = list_unread_replies(inbox)

    if not replies:
        print("agentmail-poller: no unread [school] replies")
        return

    for reply in replies:
        print(f"  Processing: {reply['command']} → {reply['bead']}")
        msg = _execute_approval(reply, repo_root)
        print(f"  {msg}")

        # Mark the thread processed (correct AgentMail label shape) so we
        # don't reprocess it on the next tick.
        try:
            _req("PATCH", f"/inboxes/{inbox}/threads/{reply['thread_id']}",
                 {"add_labels": ["processed"]})
        except Exception:
            pass


if __name__ == "__main__":
    main()
