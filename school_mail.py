#!/usr/bin/env python3
"""AgentMail notify client for the Agent-School Principal.

Sends a verdict notification to the human operator after the Principal
reconciles a bead's two-judge review, plus alert cards for problem issues
(retry / school-failed) and CI failures on main.

The AgentMail transport (key + inbox resolution, request helper, logging) is
shared with the inbound pollers via :mod:`agentmail_client` — see that module
for the env contract. This module only composes and sends the cards.

Every card is best-effort: any failure degrades to a log line and a ``False``
return so the Principal / bridge / CI never crashes mid-notify.

Env:
    AGENTMAIL_API_KEY        (required) — user-scoped key (am_us_…)
    AGENTMAIL_SCHOOL_INBOX   (optional) — destination inbox; defaults to
                              AGENTMAIL_INBOX, then the first inbox.
    AGENTMAIL_INBOX          (optional) — fallback destination inbox.
"""

from __future__ import annotations

import logging

from agentmail_client import (
    configure_logging,
    req as _req,          # patched by tests; thin alias to the shared client
    resolve_dest_inbox as _resolve_dest_inbox,  # patched by tests
)

logger = logging.getLogger("school_mail")

# Response affordance footer shared by every card that can be acted on. Each
# command must sit on its own line so the inbound poller's parser
# (agentmail_poller._parse_approval) sees it as a human command and not as
# part of this instruction footer.
RESPONSE_FOOTER = (
    "Reply with one of:\n"
    "/approve — accept this work and merge it\n"
    "/reject — mark it rejected\n"
    "/fix <note> — send it back with your note"
)


def _format_findings_table(findings: list[dict]) -> str:
    """Render findings as a compact, column-aligned table."""
    if not findings:
        return "  (none)"
    lines = ["  file:line                 sev  message"]
    for f in findings:
        file = f.get("file", "?")
        line = f.get("line", "?")
        sev = f.get("severity", f.get("level", "?"))
        msg = f.get("message", f.get("description", ""))[:60]
        loc = f"{file}:{line}"
        lines.append(f"  {loc:<26} {sev:<4} {msg}")
    return "\n".join(lines)


def _plain_verdict(accepted: bool) -> str:
    """One plain-English sentence about what the verdict means."""
    if accepted:
        return "The work passed both teacher reviews and is ready to merge."
    return "The work did not pass review — see the findings below before deciding."


def notify_verdict(
    bead: str,
    accepted: bool,
    cto_verdict: str,
    coo_verdict: str,
    summary: str = "",
    repo: str = "__global__",
    entire_findings: list[dict] | None = None,
    entire_status: str | None = None,
    cto_findings: list | None = None,
    coo_findings: list | None = None,
) -> bool:
    """Send a two-judge verdict notification to the human operator.

    Returns True if the send succeeded, False if it degraded (missing key,
    network error, etc.) — never raises, so the Principal's reconcile loop
    stays resilient.

    Sends a rubber-stamp card: a plain-English "What happened" line, the
    verdict + findings, and the response footer the human replies to with
    /approve /reject /fix. The AgentMail inbound poller
    (src/agentmail_poller.py) processes the reply and triggers merge/dispose.
    """
    try:
        inbox = _resolve_dest_inbox()
    except Exception as e:
        logger.warning("notify skipped (no inbox): %s", e)
        return False

    mark = "✅ ACCEPTED" if accepted else "❌ REJECTED"

    # Build the rubber-stamp card body
    parts = [
        f"[Agent-School] {bead} — {mark}",
        "",
        f"Repo: {repo}",
        f"CTO: {cto_verdict}  |  COO: {coo_verdict}",
    ]

    # ELI5 layer: what happened, in one plain sentence.
    parts += ["", f"What happened: {summary.strip() or _plain_verdict(accepted)}"]

    if entire_status:
        parts.append("")
        parts.append(f"Pre-merge check: {entire_status}")
        if entire_findings:
            parts.append("  Findings (real bugs only):")
            parts.append(_format_findings_table(entire_findings))

    if cto_findings or coo_findings:
        parts.append("")
        parts.append("Review findings:")
        if cto_findings:
            parts.append(f"  CTO: {_format_findings_table(cto_findings)}")
        if coo_findings:
            parts.append(f"  COO: {_format_findings_table(coo_findings)}")

    parts.append("")
    parts.append(RESPONSE_FOOTER)

    text = "\n".join(parts)

    try:
        _req(
            "POST",
            f"/inboxes/{inbox}/messages/send",
            {"to": [inbox], "subject": f"[school] {bead} — {mark}", "text": text},
        )
        return True
    except Exception as e:  # noqa: BLE001 — degrade, never crash the principal
        logger.warning("send failed: %s", e)
    return False


def notify_issue_alert(
    issue_number: int,
    title: str,
    status: str,          # "retry" | "school-failed"
    error: str = "",
    repo: str = "__global__",
    attempt: int = 1,
    retry_limit: int = 2,
) -> bool:
    """Alert the human operator when the Agent-School hits a problem issue.

    - ``status == "retry"`` — transient failure (gateway/Orca hiccup); the issue
      will be retried automatically on the next cycle (attempt N of retry_limit).
    - ``status == "school-failed"`` — retry budget exhausted; the issue is
      labeled school-failed and needs human review.

    Fired exactly once per issue per transition (the bridge retries at most
    once), so it never spams. Best-effort: missing key, network errors, etc.
    degrade to a log line + False — never raises, so the bridge stays resilient.
    """
    try:
        inbox = _resolve_dest_inbox()
    except Exception as e:
        logger.warning("notify skipped (no inbox): %s", e)
        return False

    if status == "school-failed":
        mark = "❌ SCHOOL-FAILED"
        subject = f"[school] #{issue_number} — SCHOOL-FAILED"
        parts = [
            f"[Agent-School] Issue #{issue_number} — {mark}",
            "",
            f"Repo: {repo}",
            f"Title: {title}",
            "",
            # ELI5: what happened and what the human should do.
            "What happened: the school tried this issue and could not finish "
            f"it after {attempt}/{retry_limit} attempts.",
            "",
            "Next step: open the issue (link below) and decide — re-open it, "
            "triage it yourself, or close it. Needs human review.",
        ]
    else:  # "retry"
        mark = "🔄 RETRY PENDING"
        subject = f"[school] #{issue_number} — RETRY ({attempt}/{retry_limit})"
        parts = [
            f"[Agent-School] Issue #{issue_number} — {mark}",
            "",
            f"Repo: {repo}",
            f"Title: {title}",
            "",
            # ELI5: transient, nothing to do.
            "What happened: a temporary failure (the gateway or school tools "
            f"hiccuped) on attempt {attempt}/{retry_limit}. No action needed — "
            "the issue will be retried automatically on the next cycle.",
        ]

    if error:
        parts += ["", f"Error: {str(error)[:500]}"]

    parts += ["", f"Issue: https://github.com/{repo}/issues/{issue_number}"]
    text = "\n".join(parts)

    try:
        _req(
            "POST",
            f"/inboxes/{inbox}/messages/send",
            {"to": [inbox], "subject": subject, "text": text},
        )
        return True
    except Exception as e:  # noqa: BLE001 — degrade, never crash the bridge
        logger.warning("send failed: %s", e)
    return False


def notify_pipeline_alert(
    component: str,
    reason: str,
    repo: str = "__global__",
    run_url: str = "",
) -> bool:
    """Alert when the school pipeline is blocked before issue execution.

    This is distinct from an issue failure: no issue ran, so the message names
    the blocked component and gives the operator a direct diagnostic next step.
    Best-effort, like every other notification surface.
    """
    try:
        inbox = _resolve_dest_inbox()
    except Exception as e:
        logger.warning("pipeline alert skipped (no inbox): %s", e)
        return False

    subject = f"[school] PIPELINE BLOCKED — {component}"[:120]
    parts = [
        f"[Agent-School] Pipeline blocked — {component}",
        "",
        f"Repo: {repo}",
        "",
        "What happened: the school could not start issue execution this cycle.",
        f"Reason: {reason}",
        "",
        "Next step: restore the named component, then run the school loop again.",
    ]
    if run_url:
        parts += ["", f"Run: {run_url}"]

    try:
        _req(
            "POST",
            f"/inboxes/{inbox}/messages/send",
            {"to": [inbox], "subject": subject, "text": "\n".join(parts)},
        )
        return True
    except Exception as e:  # noqa: BLE001 — notification must not block the loop
        logger.warning("pipeline alert send failed: %s", e)
    return False


def notify_build_failure(
    workflow: str,
    run_url: str,
    commit_sha: str,
    branch: str,
    failed_jobs: list[str],
    repo: str = "__global__",
) -> bool:
    """Alert the human operator when CI fails on the default branch.

    Fired once per failed push-to-main run (the CI workflow's notify job
    guards on ``failure() && event == push && ref == main``). ``failed_jobs``
    is the list of job names that went red — the message adds an infra hint
    when the live-Orca integration job is among them (that failure usually
    means the gateway/Orca on the Mac needs attention, not just code).

    Best-effort: never raises. Same AgentMail channel as
    :func:`notify_verdict` / :func:`notify_issue_alert`.
    """
    try:
        inbox = _resolve_dest_inbox()
    except Exception as e:
        logger.warning("notify skipped (no inbox): %s", e)
        return False

    jobs = ", ".join(failed_jobs) if failed_jobs else "unknown job(s)"
    subject = f"[school] CI FAILED on {branch} — {jobs}"[:120]

    parts = [
        f"[Agent-School] CI FAILED — {workflow}",
        "",
        f"Branch: {branch}",
        f"Commit: {commit_sha[:12]}",
        f"Repo: {repo}",
        "",
        # ELI5: what happened + what to do.
        "What happened: the automated checks failed on the main branch.",
        "",
        "Failed jobs:",
        *(f"  - {j}" for j in (failed_jobs or ["(unknown)"])),
    ]
    if any("integration" in j.lower() for j in failed_jobs):
        parts += [
            "",
            "Note: the live integration job failed — check the OmniRoute",
            "gateway (localhost:20128) and Orca on the Mac runner; it may be",
            "an infrastructure issue rather than a code regression.",
        ]
    parts += [
        "",
        "Next step: open the failing run (link below) to see what broke.",
        "",
        f"Run: {run_url}",
    ]
    text = "\n".join(parts)

    try:
        _req(
            "POST",
            f"/inboxes/{inbox}/messages/send",
            {"to": [inbox], "subject": subject, "text": text},
        )
        return True
    except Exception as e:  # noqa: BLE001 — degrade, never crash the caller
        logger.warning("send failed: %s", e)
    return False


if __name__ == "__main__":
    # Safe demo: prints the card it WOULD send without touching the network.
    # Run `python school_mail.py --send` to actually send the sample.
    import sys

    configure_logging()
    if "--send" not in sys.argv:
        print("[dry-run] would send a verdict card; use --send to actually send")
        print("---")
        print(
            "[Agent-School] demo-bead — ❌ REJECTED\n\n"
            "Repo: __global__\n"
            "CTO: PASS  |  COO: FAIL\n\n"
            "What happened: The work did not pass review — see the findings "
            "below before deciding.\n\n"
            "Review findings:\n"
            "  CTO:   (none)\n"
            "  COO:   (none)\n\n"
            + RESPONSE_FOOTER
        )
        sys.exit(0)
    ok = notify_verdict(
        "demo-bead", False, "PASS", "FAIL", "teacher-coo found incomplete acceptance criteria"
    )
    print("notify_verdict ->", ok)
