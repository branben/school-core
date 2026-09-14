"""trust_envelope.py — classify an issue into the cloud-lane trust envelope.

The cloud lane (U7) auto-applies routine, low-risk slices and pauses for human
approval on high-risk changes. This module is the small, testable classifier
that decides which bucket a GitHub issue falls into.

Input: primitives (the shape `github_fetcher.py` already extracts from a raw
issue dict) — nothing else, so it is unit-testable without an API.

Output: `{"decision": "auto-apply" | "human-approve", "reason": str, "risk": "low"|"high"}`
"""
from __future__ import annotations

import re

# High-risk signals (any → pause for human approval). A basic inline check on
# the title is intentional: "auth", "data", "public API", "curriculum" are the
# plan's envelope boundaries and surface in slice titles we control.
HIGH_RISK_TITLE_RE = re.compile(
    r"\b(auth|authentication|oauth|token|session|password|credential|"
    r"api[ -]?key|secret|pii|personally.?identif|billing|payment|refund|"
    r"encrypt|decrypt|sql|injection|xss|csrf|rate[ -]?limit|rollback|"
    r"migration(?! rollback debug)|curriculum)\b|security",
    re.IGNORECASE,
)


def classify(title: str, labels: list[str] | None = None, body: str = "") -> dict:
    """Return the trust-envelope decision for an issue.

    Risk is the primary signal (plan: auto-apply vs human on risk, not on
    type), then readiness: something tagged as not-ready (or lacking the
    ready-for-agent label) is held for human eyes even when low-risk.
    """
    labels = labels or []
    lower_labels = [str(l).lower() for l in labels]
    low = title.lower()
    risk = "high" if HIGH_RISK_TITLE_RE.search(low) else "low"

    # Not ready-for-agent → human holds it. This mirrors the school loop's
    # "state != ready-for-agent => continue" gate, but cloud-side we surface it
    # as an approval pause rather than silently skipping.
    if not any(l in ("ready-for-agent", "ready to work") for l in lower_labels):
        return {
            "decision": "human-approve",
            "reason": f"not marked ready-for-agent; risk={risk}",
            "risk": risk,
        }

    if risk == "high":
        return {
            "decision": "human-approve",
            "reason": "high-risk surface (auth/data/public-api/curriculum/security)",
            "risk": "high",
        }

    return {"decision": "auto-apply", "reason": "low-risk, ready-for-agent", "risk": "low"}