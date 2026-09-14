#!/usr/bin/env python3
"""school_daily_digest.py — push a daily board digest to Telegram.

Reads data/board.json and sends a structured summary of what the school is doing.
Run via cron: python3 scripts/school_daily_digest.py
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOARD_FILE = REPO / "data" / "board.json"
TELEGRAM_CHAT_ID = "7434648418"


def send_telegram(message: str) -> None:
    """Send a message to Telegram."""
    try:
        subprocess.run(
            ["hermes", "send", "--to", "telegram:" + TELEGRAM_CHAT_ID, message],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def load_board() -> dict:
    """Load board.json."""
    try:
        return json.loads(BOARD_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lanes": {}}


def format_card(card: dict) -> str:
    """Format a single board card."""
    title = card.get("title", "Unknown")[:40]
    status = card.get("status", "?")
    score = card.get("score")
    score_str = f" ({score:.0f})" if score else ""
    return f"  • {title} [{status}]{score_str}"


def main():
    board = load_board()
    lanes = board.get("lanes", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"📊 School Core Board — {now}"]

    # NOW lane
    now_cards = lanes.get("now", [])
    lines.append(f"\n🔴 NOW ({len(now_cards)})")
    if now_cards:
        for card in now_cards[:5]:
            lines.append(format_card(card))
    else:
        lines.append("  (empty)")

    # NEXT lane
    next_cards = lanes.get("next", [])
    lines.append(f"\n🟡 NEXT ({len(next_cards)})")
    if next_cards:
        for card in next_cards[:5]:
            lines.append(format_card(card))
        if len(next_cards) > 5:
            lines.append(f"  ... and {len(next_cards) - 5} more")
    else:
        lines.append("  (empty)")

    # CUT lane (recently completed)
    cut_cards = lanes.get("cut", [])
    lines.append(f"\n🟢 CUT ({len(cut_cards)})")
    if cut_cards:
        for card in cut_cards[:3]:
            lines.append(format_card(card))
        if len(cut_cards) > 3:
            lines.append(f"  ... and {len(cut_cards) - 3} more")
    else:
        lines.append("  (empty)")

    # Summary
    total = len(now_cards) + len(next_cards) + len(cut_cards)
    lines.append(f"\n📈 Total tracked: {total}")

    message = "\n".join(lines)
    send_telegram(message)
    print("Digest sent to Telegram")


if __name__ == "__main__":
    main()
