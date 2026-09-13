"""Run-log management — append and query agent run records.

Each agent run is a JSONL record appended to ``director-runs/<YYYY-MM>.jsonl``.
This module wraps the low-level read/write so the rest of the Director Console
(and tests) have a single entry point.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from director_console import orca_bridge


def _run_log_dir() -> str:
    """Return RUN_LOG_DIR at runtime so monkeypatching works in tests."""
    return orca_bridge.RUN_LOG_DIR


def log_path_for(dt: Optional[datetime] = None) -> str:
    """Return the JSONL log path for a given datetime (default: now)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    date_str = dt.strftime("%Y-%m")
    return os.path.join(_run_log_dir(), f"{date_str}.jsonl")


def append_record(record: dict[str, Any]) -> str:
    """Append a run record dict to the appropriate monthly JSONL file.

    Returns the file path written.
    """
    os.makedirs(_run_log_dir(), exist_ok=True)
    path = log_path_for()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return path


def read_records(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Read all run records from the monthly JSONL files in the date range.

    Args:
        since: Inclusive lower bound on ``issued_at``. If None, no lower bound.
        until: Exclusive upper bound on ``issued_at``. If None, no upper bound.
    """
    records: list[dict[str, Any]] = []
    run_dir = _run_log_dir()
    if not os.path.isdir(run_dir):
        return records

    for fname in sorted(os.listdir(run_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(run_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Filter by issued_at if bounds given.
                    issued = rec.get("issued_at", "")
                    if since and issued < since.isoformat():
                        continue
                    if until and issued >= until.isoformat():
                        continue
                    records.append(rec)
        except OSError:
            continue

    return records
