#!/usr/bin/env python3
"""Sanitize data/crew_runs.json before checkpoint.

Drops transient active rows (status in {running, blocked}) so the committed
ledger never carries an in-flight record. A checked-in active row permanently
denies crew admission at runner_slots=1.

Usage:
    python3 scripts/sanitize_ledger.py [path/to/crew_runs.json]
"""

import json
import sys
from pathlib import Path


ACTIVE_STATUSES = {"running", "blocked"}


def sanitize(ledger_path: Path) -> int:
    if not ledger_path.exists():
        return 0
    try:
        rows = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(rows, list):
        return 0
    clean = [r for r in rows if r.get("status") not in ACTIVE_STATUSES]
    removed = len(rows) - len(clean)
    if removed > 0:
        ledger_path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        print(f"[checkpoint] sanitized {ledger_path}: removed {removed} active row(s)")
    return removed


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/crew_runs.json")
    sys.exit(0 if sanitize(target) >= 0 else 1)
