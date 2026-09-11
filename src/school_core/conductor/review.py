"""conductor/review.py — CTO/COO review orchestration.

Handles verdict persistence, acceptance computation, and verdict-record
validation at the principal reconcile point.
"""

from __future__ import annotations

import os

from bookbag import read_bookbag, locked_update_bookbag


def _persist_acceptance(bead: str, cto_v: str, coo_v: str, repo: str = "__global__") -> bool:
    """Compute and persist the bookbag 'accepted' flag from both verdicts.

    Must match director.run_task's acceptance contract (director.py:313):
    accepted requires BOTH judges PASS at score >= 50, with NO critical
    finding (a CRITICAL finding is an automatic veto). The teacher review
    loops write their individual verdicts/scores/findings but never set
    'accepted', and evaluate_and_update (which conductor calls) does not
    re-derive it — so this flag, once persisted, is authoritative.

    Returns the computed acceptance so callers can report it, but only
    returns True if the write actually reached disk (locked_update_bookbag
    returns None on lock-acquisition failure — it does not raise).
    """
    bag = read_bookbag(bead, repo) or {}
    try:
        cto_score = float(bag.get("cto_score", 0) or 0)
        coo_score = float(bag.get("coo_score", 0) or 0)
    except (TypeError, ValueError):
        cto_score = coo_score = 0.0
    findings = (bag.get("cto_findings", []) or []) + (bag.get("coo_findings", []) or [])
    # Entire gate: CRITICAL findings from entire review also veto acceptance
    entire_findings = bag.get("entire_findings") or []
    if entire_findings and bag.get("entire_status") not in ("skipped", "error", None):
        findings = findings + entire_findings
    has_critical = any(
        str(f.get("severity", "")).upper() == "CRITICAL" for f in findings
    )
    # HIGH findings veto only when ENTIRE_HIGH_STRICT=1
    if not has_critical and os.getenv("ENTIRE_HIGH_STRICT") == "1":
        has_critical = any(
            str(f.get("severity", "")).upper() == "HIGH" for f in entire_findings
        )
    accepted = (
        cto_v == "PASS"
        and coo_v == "PASS"
        and cto_score >= 50
        and coo_score >= 50
        and not has_critical
    )
    written = locked_update_bookbag(bead, repo, lock_timeout=10.0, accepted=accepted)
    if written is None:
        print(f"[principal] WARNING: accepted flag for {bead} NOT persisted "
              f"(lock timeout) — disk may show stale accepted=False")
        return False
    return accepted


def _validate_verdict(bead: str, repo: str = "__global__") -> None:
    """Enforce the Gap-B verdict-record contract at the principal reconcile point.

    The bookbag after the refactor holds ONLY the two-judge output. This is a
    guard, not a task blocker: a malformed record is logged (so the human sees
    it on the dashboard) but the dispatch still completes.

    Rank 1: when a teacher ran the diagnose loop (--diagnose), surface the
    recorded `{role}_diagnosis` in the bookbag so the principal note is
    traceable and visible on the dashboard.
    """
    try:
        from bookbag import validate_verdict_record
    except Exception:
        return
    bag = read_bookbag(bead, repo)
    if bag:
        cto_dx = bag.get("cto_diagnosis")
        coo_dx = bag.get("coo_diagnosis")
        if cto_dx:
            print(f"  🔬 CTO diagnose: {cto_dx.get('root_cause', '')[:80]}")
        if coo_dx:
            print(f"  🔬 COO diagnose: {coo_dx.get('root_cause', '')[:80]}")
    ok, issues = validate_verdict_record(bead, repo)
    if not ok:
        print(f"  ⚠️ verdict-record contract violation for {bead}: "
              f"{'; '.join(issues)}")
