#!/usr/bin/env python3
"""A2A bridge: dispatch → poll → read for OpenHands automation webhook.

Stdlib only. CLI:
    python3 a2a_bridge.py --task "<text>"
    python3 a2a_bridge.py --poll-only --run-id <id>
    python3 a2a_bridge.py --read-only --run-id <id>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error

# --- Configuration (verified against source) ---
# Secrets come from the environment, never from source.

WEBHOOK_URL = (
    "http://localhost:18001/api/automation/v1/events/"
    "46d09c5c-7d7d-585f-8c37-a6b45a3de738/hermes-test"
)
API_BASE = "http://localhost:18001/api/automation"
AUTOMATION_ID = "5e872472-b4cb-43f0-a3f4-30b57fcc4203"
ORG_ID = "46d09c5c-7d7d-585f-8c37-a6b45a3de738"
AGENT_SERVER_URL = "http://localhost:18000"

WEBHOOK_SECRET = os.environ.get("A2A_WEBHOOK_SECRET", "")
SESSION_KEY = os.environ.get("A2A_SESSION_KEY", "")


def require_secrets() -> None:
    """Fail loudly if either secret is missing. Reads env at call time."""
    missing = [
        name
        for name in ("A2A_WEBHOOK_SECRET", "A2A_SESSION_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}"
        )

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 300.0


def compute_hmac_signature(secret: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of body with secret."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def dispatch(webhook_url: str, secret: str, task: str) -> dict:
    """POST task to webhook with HMAC signature. Returns parsed response."""
    body = json.dumps({"task": task}).encode("utf-8")
    signature = compute_hmac_signature(secret, body)

    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature-256": signature,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Dispatch failed: HTTP {exc.code} — {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dispatch failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Dispatch returned malformed JSON: {exc}") from exc


def poll_run_status(
    api_base: str,
    session_key: str,
    org_id: str,
    automation_id: str,
    run_id: str,
    *,
    interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = POLL_TIMEOUT_SECONDS,
) -> dict:
    """Poll GET /v1/{id}/runs until run reaches terminal status."""
    # Route contract (router.py:104 prefix="/v1", router.py:690 route,
    # mounted at app.py:300 under /api/automation) — NO "automations" segment.
    url = (
        f"{api_base}/v1/{automation_id}/runs?limit=5"
    )
    headers = {
        "X-Session-API-Key": session_key,
        "X-Org-Id": org_id,
    }

    deadline = time.monotonic() + timeout

    while True:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Poll failed: HTTP {exc.code} — {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Poll failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Poll returned malformed JSON: {exc}") from exc

        # Find our run in the list
        runs = data.get("runs", [])
        for run in runs:
            if run.get("id") == run_id:
                status = run.get("status", "")
                if status in TERMINAL_STATUSES:
                    return run
                break
        else:
            raise RuntimeError(f"Run {run_id} not found in poll response")

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Poll timeout after {timeout:.0f}s — run {run_id} "
                f"did not reach terminal status"
            )

        time.sleep(interval)


def extract_last_assistant_text(events: dict) -> str | None:
    """Extract text from the last assistant MessageEvent in a conversation."""
    items = events.get("items", [])
    # Events are returned newest-first when sort_order=TIMESTAMP_DESC
    for event in items:
        if event.get("role") != "assistant":
            continue
        content = event.get("content", [])
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    return part.get("text")
    return None


def read_agent_answer(
    agent_url: str,
    session_key: str,
    conversation_id: str,
) -> str:
    """GET events/search and extract the agent's final answer."""
    url = (
        f"{agent_url}/api/conversations/{conversation_id}/events/search"
        f"?source=agent&sort_order=TIMESTAMP_DESC&limit=100"
    )
    headers = {"X-Session-API-Key": session_key}

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Read failed: HTTP {exc.code} — {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Read failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Read returned malformed JSON: {exc}") from exc

    answer = extract_last_assistant_text(data)
    if answer is None:
        raise RuntimeError(
            "No assistant message found in conversation events"
        )
    return answer


def main() -> int:
    parser = argparse.ArgumentParser(description="A2A bridge for OpenHands")
    parser.add_argument("--task", help="Task text to dispatch")
    parser.add_argument(
        "--poll-only", action="store_true",
        help="Only poll an existing run (requires --run-id)"
    )
    parser.add_argument(
        "--read-only", action="store_true",
        help="Only read answer from a run's conversation (requires --run-id)"
    )
    parser.add_argument("--run-id", help="Run ID for --poll-only / --read-only")

    args = parser.parse_args()

    if not args.poll_only and not args.read_only:
        # Full pipeline: dispatch → poll → read
        if not args.task:
            parser.error("--task is required for full pipeline")

        print(f"Dispatching task: {args.task}", file=sys.stderr)
        resp = dispatch(WEBHOOK_URL, WEBHOOK_SECRET, args.task)
        print(f"Dispatch response: {resp}", file=sys.stderr)

        run_ids = resp.get("runs_created") or []
        run_id = run_ids[0] if run_ids else None
        if not run_id:
            print("Error: no run_id in dispatch response", file=sys.stderr)
            return 1

        print(f"Polling run {run_id}...", file=sys.stderr)
        run = poll_run_status(
            API_BASE, SESSION_KEY, ORG_ID, AUTOMATION_ID, run_id
        )
        print(f"Run status: {run.get('status')}", file=sys.stderr)

        if run.get("status") == "FAILED":
            print(f"Run failed: {run.get('error_detail', 'unknown')}", file=sys.stderr)
            return 1

        conversation_id = run.get("conversation_id")
        if not conversation_id:
            print("Error: no conversation_id in run", file=sys.stderr)
            return 1

        print(f"Reading answer from conversation {conversation_id}...", file=sys.stderr)
        answer = read_agent_answer(AGENT_SERVER_URL, SESSION_KEY, conversation_id)
        print(answer)
        return 0

    if args.poll_only:
        if not args.run_id:
            parser.error("--run-id is required for --poll-only")
        run = poll_run_status(
            API_BASE, SESSION_KEY, ORG_ID, AUTOMATION_ID, args.run_id
        )
        print(json.dumps(run, indent=2))
        return 0

    if args.read_only:
        if not args.run_id:
            parser.error("--run-id is required for --read-only")
        # For read-only we need the conversation_id — poll first
        run = poll_run_status(
            API_BASE, SESSION_KEY, ORG_ID, AUTOMATION_ID, args.run_id
        )
        conversation_id = run.get("conversation_id")
        if not conversation_id:
            print("Error: no conversation_id in run", file=sys.stderr)
            return 1
        answer = read_agent_answer(AGENT_SERVER_URL, SESSION_KEY, conversation_id)
        print(answer)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
