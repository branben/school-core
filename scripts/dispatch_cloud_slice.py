"""dispatch_cloud_slice.py — U7 cloud-lane event handler.

Given a GitHub issue number in the dispatch repository, decide via the trust
envelope whether to (a) start an OpenHands Cloud conversation to implement the
slice and open a PR, or (b) add/confirm the human-approval label and stop.

Transport is pure stdlib (urllib) so the helper is dependency-free inside the
GitHub Action container.

Auth:
  OPENHANDS_CLOUD_API_KEY (or legacy OPENHANDS_API_KEY) — OpenHands Cloud.
  GITHUB_TOKEN                           — muted pipeline token (comment-only).

Usage (as run by .github/workflows/cloud-lane.yml):
  python3 scripts/dispatch_cloud_slice.py --repo owner/repo --issue 123
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# Reuse the repo's readiness classifier; the envelope risk logic lives in
# trust_envelope.py so it stays independently testable.
from trust_envelope import classify as envelope_classify  # type: ignore

APP_BASE = os.environ.get("OH_CLOUD_BASE", "https://app.all-hands.dev/api/v1")
GITHUB_BASE = os.environ.get("OH_GH_BASE", "https://api.github.com")
APPROVAL_LABEL = "human-approve"
ENVELOPE_APPROVE = "cloud-auto-apply"


def _base(name: str, default: str) -> str:
    """Resolve a test-overridable base URL at call time (env may be set after import)."""
    return os.environ.get(name, default)


def _post(url: str, payload: dict, headers: dict, timeout: int = 60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(url: str, headers: dict, timeout: int = 60):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_issue(repo: str, number: int, token: str) -> dict:
    return _get(f"{_base('OH_GH_BASE', GITHUB_BASE)}/repos/{repo}/issues/{number}",
                {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})


def add_label(repo: str, number: int, label: str, token: str) -> None:
    _post(f"{_base('OH_GH_BASE', GITHUB_BASE)}/repos/{repo}/issues/{number}/labels",
          {"labels": [label]}, {"Authorization": f"Bearer {token}"})


def comment(repo: str, number: int, body: str, token: str) -> None:
    _post(f"{_base('OH_GH_BASE', GITHUB_BASE)}/repos/{repo}/issues/{number}/comments",
          {"body": body}, {"Authorization": f"Bearer {token}"})


def envelope_verdict(issue: dict) -> tuple[str, str, str]:
    """Return (decision, risk, reason)."""
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    labels = [l.get("name", "") for l in (issue.get("labels") or [])]
    result = envelope_classify(title, labels, body)
    return result["decision"], result["risk"], result["reason"]


def start_cloud_conversation(issue: dict, repo: str, api_key: str, dry_run: bool) -> str | None:
    """Start an OpenHands Cloud conversation to implement+PR the slice."""
    number = issue["number"]
    title = issue.get("title") or f"issue {number}"
    prompt = (
        f"Implement GitHub issue {repo}#{number} in the `{repo}` repository.\n"
        f"Issue title: {title}\n"
        f"Issue body:\n{issue.get('body') or ''}\n\n"
        f"Constraints:\n"
        f"- Make the minimal change needed to resolve the issue.\n"
        f"- Add or update focused tests.\n"
        f"- Run the relevant test suite and fix failures before opening the PR.\n"
        f"- Open a pull request to the default branch when done (repo write access "
        f"is configured).\n"
        f"- Report the PR URL."
    )
    if dry_run:
        print(f"[dry-run] would start cloud conversation for {repo}#{number}")
        return None
    payload = {
        "selected_repository": repo,
        "initial_message": {
            "content": [{"type": "text", "text": prompt}],
            "run": True,
        },
        "title": f"[cloud-lane] {title[:80]}",
    }
    try:
        start = _post(f"{_base('OH_CLOUD_BASE', APP_BASE)}/app-conversations", payload,
                      {"Authorization": f"Bearer {api_key}"})
    except Exception as exc:  # noqa: BLE001  report and fall through to comment
        print(f"[dispatch] failed to start cloud conversation: {exc}", file=sys.stderr)
        return None
    # Asynchronous start: `app_conversation_id` may only arrive after polling
    # the start-task. We do one poll here so the URL we comment is valid.
    conv_id = start.get("app_conversation_id") or start.get("id")
    if not start.get("app_conversation_id"):
        try:
            items = _post(f"{_base('OH_CLOUD_BASE', APP_BASE)}/app-conversations/start-tasks",
                          {"ids": [start.get("id")]},
                          {"Authorization": f"Bearer {api_key}"})
            if isinstance(items, list) and items:
                item = items[0] if not isinstance(items[0], dict) else items[0]
                conv_id = (item or {}).get("app_conversation_id") or conv_id
        except Exception as exc:  # noqa: BLE001
            print(f"[dispatch] start-task poll failed (still commenting URL): {exc}", file=sys.stderr)
    if not conv_id:
        return None
    url = f"https://app.all-hands.dev/conversations/{conv_id}"
    print(f"[dispatch] started cloud conversation: {url}")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo that hosts the issue")
    parser.add_argument("--issue", required=True, type=int, help="issue number")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    api_key = os.environ.get("OPENHANDS_CLOUD_API_KEY") or os.environ.get("OPENHANDS_API_KEY")
    if not token:
        print("[dispatch] GITHUB_TOKEN not set", file=sys.stderr)
        return 2
    if not api_key and not args.dry_run:
        print("[dispatch] OPENHANDS_CLOUD_API_KEY not set", file=sys.stderr)
        return 2

    issue = None
    try:
        issue = fetch_issue(args.repo, args.issue, token)
    except Exception as exc:  # noqa: BLE001 — HTTPError / JSON decode / network
        print(f"[dispatch] could not fetch {args.repo}#{args.issue}: {exc}", file=sys.stderr)
        return 2
    decision, risk, reason = envelope_verdict(issue)
    print(f"[dispatch] {args.repo}#{args.issue} → {decision} (risk={risk}) {reason}")

    if decision == "human-approve":
        add_label(args.repo, args.issue, APPROVAL_LABEL, token)
        comment(args.repo, args.issue,
                f"[cloud-lane] Paused for human approval. Envelope: {reason}. "
                f"(Review the slice, then run the dispatch again or implement locally.)",
                token)
        return 1  # not an error: this is the intended pause path

    url = start_cloud_conversation(issue, args.repo, api_key or "", dry_run=args.dry_run)
    if url:
        comment(args.repo, args.issue,
                f"[cloud-lane] Auto-apply: OpenHands is implementing this slice. "
                f"Monitoring at {url}",
                token)
        return 0
    comment(args.repo, args.issue,
            "[cloud-lane] Envelope said auto-apply but the cloud conversation "
            "failed to start. Investigate OPENHANDS_API_KEY/config and retry.",
            token)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())