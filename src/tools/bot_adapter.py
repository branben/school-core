#!/usr/bin/env python3
"""bot_adapter.py — Bridge School Core routing to Bot Mode bots.

School Core routes to "agents" which are currently raw model combos
(OmniRoute/A2A backends). Bot Mode bots are full Hermes profiles with
their own SOUL.md, memory, skills, and chat history.

This adapter lets School Core execute tasks against Bot Mode bots by
wrapping ``hermes -p <bot> chat`` as a transport. The result is captured,
trajectory is logged, and the response is returned — identical to the
direct model path, but the execution happens inside a full agent profile.

Usage::

    from src.tools.bot_adapter import bot_execute, bot_mention

    # Execute a task against a Bot Mode bot
    result = bot_execute("student-coder", "Write a function to fibonacci")
    if result["success"]:
        print(result["response"])

    # Send a bot-to-bot mention (escalation)
    bot_mention("student-coder", "teacher-cto", "Please review this PR")

This module is stdlib-only by design: it is imported by the MCP server,
which must start even when the repo's optional dependencies are absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

# Bot Mode bots = Hermes profiles at ~/.hermes/profiles/<name>/
HERMES_PROFILES_DIR = os.path.expanduser("~/.hermes/profiles")

# Canonical conversation for bot-to-bot mentions
AGENT_INBOX = "Agent Inbox"


class BotAdapterError(Exception):
    """Raised when the adapter cannot reach its transport."""


def _hermes_binary() -> str:
    """Locate the hermes CLI. Checks PATH first, then common locations."""
    from shutil import which

    path = which("hermes")
    if path:
        return path
    candidates = [
        os.path.expanduser("~/.hermes/bin/hermes"),
        "/usr/local/bin/hermes",
        "/opt/homebrew/bin/hermes",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise BotAdapterError("hermes CLI not found in PATH. Install Hermes Agent first.")


def bot_exists(bot_name: str) -> bool:
    """Check if a Bot Mode bot (Hermes profile) exists."""
    profile_dir = os.path.join(HERMES_PROFILES_DIR, bot_name)
    return os.path.isdir(profile_dir)


def list_bots() -> list[str]:
    """List all available Bot Mode bots (Hermes profiles)."""
    if not os.path.isdir(HERMES_PROFILES_DIR):
        return []
    return sorted(
        d for d in os.listdir(HERMES_PROFILES_DIR)
        if os.path.isdir(os.path.join(HERMES_PROFILES_DIR, d))
    )


def bot_info(bot_name: str) -> dict:
    """Get info about a Bot Mode bot: model, SOUL.md preview, etc."""
    profile_dir = os.path.join(HERMES_PROFILES_DIR, bot_name)
    if not os.path.isdir(profile_dir):
        return {"exists": False, "name": bot_name}

    soul_path = os.path.join(profile_dir, "SOUL.md")
    soul_preview = ""
    if os.path.isfile(soul_path):
        with open(soul_path, "r") as handle:
            soul_preview = handle.read()[:500]

    config_path = os.path.join(profile_dir, "config.yaml")
    model = "unknown"
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r") as handle:
                for line in handle:
                    if "default:" in line and "model" not in line.split("default:")[0]:
                        model = line.split("default:")[1].strip()
                        break
        except OSError:
            pass

    return {
        "exists": True,
        "name": bot_name,
        "profile_dir": profile_dir,
        "model": model,
        "soul_preview": soul_preview,
    }


def bot_execute(
    bot_name: str,
    prompt: str,
    *,
    conversation: Optional[str] = None,
    timeout: int = 120,
    model: Optional[str] = None,
) -> dict:
    """Execute a prompt against a Bot Mode bot via CLI handoff.

    This starts a NEW chat session each time (isolated execution).
    For ongoing conversations, use ``conversation="Agent Inbox"``.

    Returns dict with keys:
        success: bool
        response: str  (stdout of the bot)
        bot_name: str
        error: Optional[str]
        duration_seconds: float
    """
    if not bot_exists(bot_name):
        return {
            "success": False,
            "response": "",
            "bot_name": bot_name,
            "error": f"Bot '{bot_name}' not found at {HERMES_PROFILES_DIR}/{bot_name}",
            "duration_seconds": 0.0,
        }

    hermes = _hermes_binary()
    cmd = [hermes, "-p", bot_name, "chat", "-Q", "-q", prompt]
    if conversation:
        cmd.extend(["-c", conversation])
    if model:
        cmd.extend(["-m", model])

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Inherit minimal env; hermes reads profile config itself
            env={**os.environ, "HERMES_QUIET": "1"},
        )
        duration = time.monotonic() - start

        if result.returncode == 0:
            return {
                "success": True,
                "response": result.stdout.strip(),
                "bot_name": bot_name,
                "error": None,
                "duration_seconds": round(duration, 2),
            }
        stderr = result.stderr.strip() or f"exit code {result.returncode}"
        return {
            "success": False,
            "response": "",
            "bot_name": bot_name,
            "error": stderr[:1000],
            "duration_seconds": round(duration, 2),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "response": "",
            "bot_name": bot_name,
            "error": f"timeout after {timeout}s",
            "duration_seconds": float(timeout),
        }
    except OSError as exc:
        return {
            "success": False,
            "response": "",
            "bot_name": bot_name,
            "error": str(exc),
            "duration_seconds": round(time.monotonic() - start, 2),
        }


def bot_mention(
    from_bot: str,
    to_bot: str,
    message: str,
    *,
    conversation: str = AGENT_INBOX,
    timeout: int = 60,
) -> dict:
    """Send a bot-to-bot mention message.

    In Bot Mode, @mention in the UI triggers a handoff. This function
    sends a direct message to another bot's Agent Inbox conversation,
    equivalent to an @mention but callable from Python (e.g., from
    School Core escalation logic).

    Returns dict with keys: success, response, from, to, error
    """
    if not bot_exists(to_bot):
        return {
            "success": False,
            "response": "",
            "from": from_bot,
            "to": to_bot,
            "error": f"Bot '{to_bot}' not found",
        }

    hermes = _hermes_binary()
    formatted = f"Message from 🤖 {from_bot} (@{from_bot}): {message}"
    cmd = [hermes, "-p", to_bot, "chat", "-Q", "-q", formatted, "-c", conversation]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HERMES_QUIET": "1"},
        )
        return {
            "success": result.returncode == 0,
            "response": result.stdout.strip(),
            "from": from_bot,
            "to": to_bot,
            "error": result.stderr.strip() if result.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "response": "",
            "from": from_bot,
            "to": to_bot,
            "error": f"timeout after {timeout}s",
        }
    except OSError as exc:
        return {
            "success": False,
            "response": "",
            "from": from_bot,
            "to": to_bot,
            "error": str(exc),
        }


def bot_invoke_mentions(
    from_bot: str,
    mentions: list[dict],
    *,
    conversation: str = AGENT_INBOX,
) -> list[dict]:
    """Send multiple @mentions from one bot to several others.

    Each dict in ``mentions`` has keys:
        to: str     — target bot name
        message: str — the message to send

    Returns list of result dicts.
    """
    results = []
    for mention in mentions:
        results.append(
            bot_mention(
                from_bot,
                mention["to"],
                mention["message"],
                conversation=conversation,
            )
        )
    return results


def main() -> None:
    """CLI for testing the adapter."""
    import argparse

    parser = argparse.ArgumentParser(description="School Core <-> Bot Mode adapter")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List available Bot Mode bots")

    p_info = sub.add_parser("info", help="Show bot info")
    p_info.add_argument("bot", help="Bot name")

    p_exec = sub.add_parser("execute", help="Execute a prompt against a bot")
    p_exec.add_argument("bot", help="Bot name")
    p_exec.add_argument("prompt", help="Prompt to send")
    p_exec.add_argument("--timeout", type=int, default=120)
    p_exec.add_argument("--conversation", "-c", default=None)

    p_ment = sub.add_parser("mention", help="Send a bot-to-bot mention")
    p_ment.add_argument("from_bot", help="Sender bot name")
    p_ment.add_argument("to_bot", help="Target bot name")
    p_ment.add_argument("message", help="Message to send")
    p_ment.add_argument("--conversation", "-c", default=AGENT_INBOX)

    args = parser.parse_args()

    if args.command == "list":
        print(json.dumps(list_bots(), indent=2))
    elif args.command == "info":
        print(json.dumps(bot_info(args.bot), indent=2, default=str))
    elif args.command == "execute":
        print(json.dumps(
            bot_execute(
                args.bot,
                args.prompt,
                timeout=args.timeout,
                conversation=args.conversation,
            ),
            indent=2,
            default=str,
        ))
    elif args.command == "mention":
        print(json.dumps(
            bot_mention(
                args.from_bot,
                args.to_bot,
                args.message,
                conversation=args.conversation,
            ),
            indent=2,
            default=str,
        ))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
