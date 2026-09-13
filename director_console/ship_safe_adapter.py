"""Ship-safe adapter — read verdicts.json for calibration ground truth.

Reads the ``verdicts.json`` artifact that ship-safe emits in CI and
extracts the verdict for a given PR/issue. Returns ``None`` when ship-safe
did not run on this PR (no crash — calibration simply skips it).
"""

from __future__ import annotations

import json
import os
from typing import Optional


def read_ship_safe_verdict(
    verdicts_path: str,
    *,
    pr_id: Optional[str] = None,
    issue_id: Optional[str] = None,
) -> Optional[str]:
    """Return the ship-safe verdict (``"PASS"`` or ``"FAIL"``) for a PR.

    Args:
        verdicts_path: Absolute path to ship-safe's ``verdicts.json``.
        pr_id: The PR identifier to look up (e.g. ``"school-core#123"``).
        issue_id: Fallback — issue id to match against verdict metadata.

    Returns:
        ``"PASS"``, ``"FAIL"``, or ``None`` if no matching verdict exists.
    """
    if not os.path.isfile(verdicts_path):
        return None

    try:
        with open(verdicts_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # verdicts.json can be a list of verdicts or a single dict.
    if isinstance(data, dict):
        verdicts = data.get("verdicts", [data])
    elif isinstance(data, list):
        verdicts = data
    else:
        return None

    for v in verdicts:
        if not isinstance(v, dict):
            continue
        # Match by PR id or issue id in metadata.
        v_pr = v.get("pr_id") or v.get("pull_request") or v.get("pr")
        v_issue = v.get("issue_id") or v.get("issue") or v.get("bd_issue")
        if pr_id and v_pr and str(v_pr) == str(pr_id):
            return _normalize(v.get("verdict"))
        if issue_id and v_issue and str(v_issue) == str(issue_id):
            return _normalize(v.get("verdict"))

    return None


def _normalize(raw: Optional[str]) -> Optional[str]:
    """Normalize a raw verdict string to PASS / FAIL / None."""
    if not raw:
        return None
    v = raw.strip().upper()
    if v in ("PASS", "PASSED", "OK", "CLEAN", "SAFE"):
        return "PASS"
    if v in ("FAIL", "FAILED", "REJECT", "UNSAFE", "BLOCK"):
        return "FAIL"
    return v  # Preserve INCONCLUSIVE etc. as-is
