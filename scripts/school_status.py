#!/usr/bin/env python3
"""school_status.py — plain-text status report for Telegram.

Reads real data and outputs a structured, actionable report.
No styling, no HTML, just facts.
"""

import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
CRON_JOBS_FILE = Path.home() / ".hermes" / "cron" / "jobs.json"
TELEGRAM_CHAT_ID = "7434648418"


def send_telegram(message):
    import subprocess
    subprocess.run(["hermes", "send", "--to", "telegram:" + TELEGRAM_CHAT_ID, message],
                   capture_output=True, text=True, timeout=30)


def main():
    lines = []
    
    # Cron status
    cron_jobs = json.loads(CRON_JOBS_FILE.read_text())
    running = sum(1 for j in cron_jobs['jobs'] if j['enabled'])
    paused = sum(1 for j in cron_jobs['jobs'] if not j['enabled'])
    errors = sum(1 for j in cron_jobs['jobs'] if j.get('last_status') == 'error')
    
    lines.append(f"School Core Status\n")
    lines.append(f"Cron: {running} running, {paused} paused, {errors} errors")
    
    # Paused school crons
    paused_school = [j['name'] for j in cron_jobs['jobs'] 
                     if not j['enabled'] and 'school' in j.get('name', '').lower()]
    if paused_school:
        lines.append(f"Paused: {', '.join(paused_school[:3])}")
    
    # Board state
    board = json.loads((DATA_DIR / "board.json").read_text())
    lanes = board.get('lanes', {})
    now = len(lanes.get('now', []))
    next_ = len(lanes.get('next', []))
    cut = len(lanes.get('cut', []))
    total = now + next_ + cut
    lines.append(f"Board: {total} issues (NOW:{now} NEXT:{next_} CUT:{cut})")
    
    # Scores
    scores_db = DATA_DIR / "scores.db"
    if scores_db.exists():
        conn = sqlite3.connect(str(scores_db))
        cursor = conn.execute("SELECT role, score FROM scores WHERE domain='code-implementation' ORDER BY updated_at DESC LIMIT 3")
        scores = cursor.fetchall()
        if scores:
            trend = " → ".join([f"{s[1]:.1f}" for s in scores[::-1]])
            lines.append(f"Coder score: {trend}")
        conn.close()
    
    # Recent failures
    runs = json.loads((DATA_DIR / "last_run.json").read_text())
    failures = [r for r in runs[-20:] if r.get('status') in ('error', 'school-failed', 'blocked')]
    if failures:
        lines.append(f"Failures (last 20): {len(failures)}")
        for f in failures[-3:]:
            lines.append(f"  #{f.get('issue', '?')}: {f.get('status', '?')}")
    
    message = "\n".join(lines)
    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
