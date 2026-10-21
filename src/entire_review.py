#!/usr/bin/env python3
"""Pre-merge review shim for student-reviewer.

Replaces the deprecated qodo --improve approach with `entire review` —
an intent-aware code review that reads git checkpoints (session transcripts)
to understand developer intent before auditing the diff.

Qodo Command CLI is discontinued (v0.36.0 EOL). Entire CLI provides
superior intent-aware review with no API key needed.

Runs in the student worktree after leaf.signal_ready() and before
the two-judge review. Output is captured to review_workspace/ and
passed to the bookbag for the teacher-cto/coo personas.

Usage:
    from src.entire_review import run_entire_review
    result = run_entire_review(worktree_path="/path/to/worktree")
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional


class EntireFinding(NamedTuple):
    """Finding from the review (return shape kept bookbag-compatible)."""
    file: str
    line: int | None
    severity: str  # "CRITICAL", "HIGH", "MEDIUM", "LOW"
    message: str
    raw: str


def _get_entire_path() -> str | None:
    """Find the entire CLI binary.

    `entire` lives at ~/.local/bin/entire on this machine, but Orca worktree
    shells (and the runner's minimal LaunchAgent PATH) don't always have
    ~/.local/bin on PATH — a bare shutil.which() would report "skipped" for a
    tool that is actually installed. Probe PATH first, then the two standard
    home-bin locations.
    """
    which = shutil.which("entire")
    if which:
        return which
    for candidate in (Path.home() / ".local" / "bin" / "entire",
                      Path.home() / "bin" / "entire"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _get_changed_files(worktree_path: str, base_branch: str = "main") -> list[str] | None:
    """Get list of changed files for targeted review.

    Returns None when the diff could NOT be computed (not a git repo, missing
    base ref, git failure). That is distinct from a genuine empty diff ([]):
    an empty diff means "nothing to review", while None means "we do not know".
    A caller must never report the second as the first.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return [f for f in result.stdout.strip().split("\n") if f]


def _parse_entire_output(content: str, changed_files: list[str]) -> list[EntireFinding]:
    """Parse entire review output into structured findings.

    Entire review outputs lines prefixed with severity:
      Critical: file.py:42 — description
      High: file.py:17 — description
      Medium: file.py:3 — description
      Low: file.py:88 — description
    """
    findings: list[EntireFinding] = []

    # Track current file context for findings without line numbers

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Match severity-prefixed findings
        for severity in ["Critical", "High", "Medium", "Low"]:
            prefix = f"{severity}:"
            if line.startswith(prefix):
                finding_text = line[len(prefix):].strip()
                # Extract file:line if present (format: file.py:42 — or file.py:42)
                file_match = re.match(
                    r'(?P<file>[^\s:]+):(?P<line>\d+)\s*[—–-]?\s*(?P<msg>.+)',
                    finding_text,
                    re.DOTALL
                )
                if file_match:
                    file = file_match.group("file")
                    line_no = int(file_match.group("line"))
                    message = file_match.group("msg").strip()
                else:
                    # Maybe just a file reference
                    parts = finding_text.split(None, 1)
                    file = parts[0] if parts else ""
                    line_no = None
                    message = parts[1] if len(parts) > 1 else finding_text

                # Filter to changed files only
                file_basename = os.path.basename(file) if file else ""
                if file not in changed_files and file_basename not in [os.path.basename(cf) for cf in changed_files]:
                    continue

                findings.append(EntireFinding(
                    file=file,
                    line=line_no,
                    severity=severity.upper(),
                    message=message,
                    raw=line,
                ))
                break

        # Track file headers (Entire may output file sections)
        # (file-header tracking removed: the value was never read)

    # Deduplicate findings with same file+line+message
    seen = set()
    unique = []
    for f in findings:
        key = (f.file, f.line, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return unique


# ── Review profile bootstrap ────────────────────────────────────────────────
#
# `entire review` needs two things a fresh clone does not have:
#   1. a review profile (reviewers + judge), and
#   2. the reviewer agent's hooks.
# Both live in `.entire/settings.local.json` / `.claude/settings.json`, and
# `.entire/.gitignore` excludes `settings.local.json` — so the profile is never
# committed and every clone starts without one. Without this bootstrap
# `entire review` exits 1 ("no agents with review runner adapters and hooks
# installed"), which the caller cannot distinguish from a clean diff.
_REVIEW_AGENT = "claude-code"
_REVIEW_TASK = (
    "Review this change for correctness, regressions, API design, missing "
    "tests, maintainability, and user-facing behavior changes. Return only "
    "real, actionable defects with concrete evidence and an exact code pointer."
)
# Measured 2026-10-21 on a 1-file change: a full review takes ~201s, and the
# consolidating judge alone defaults to a 20m cap. The previous hard-coded 120s
# killed the review before it finished. Override with ENTIRE_REVIEW_TIMEOUT.
_DEFAULT_REVIEW_TIMEOUT_S = 900

# Markers meaning the review ran but produced no usable verdict. Any of these
# with zero parsed findings is an ERROR, never a pass.
_REVIEW_ERROR_MARKERS = (
    "no agents with review runner adapters",
    "hooks are not installed",
    "API Error:",
    "timed out",
    "Failed:",
)


def _review_timeout_s() -> int:
    raw = os.getenv("ENTIRE_REVIEW_TIMEOUT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_REVIEW_TIMEOUT_S


def _ensure_review_profile(worktree: Path, entire_path: str) -> Optional[str]:
    """Idempotently make `entire review --profile general` runnable in `worktree`.

    Creates the profile non-interactively (into the gitignored
    `settings.local.json`, so a repo's shared `settings.json` is never touched)
    and installs the reviewer agent's hooks — but only when the CLI reports
    them missing, because `entire agent add` rewrites `.claude/settings.json`
    and would clobber a checkout that already carries one. Returns an error
    string on failure, else None.
    """
    cfg = subprocess.run(
        [entire_path, "review", "--configure",
         "--set-agents", _REVIEW_AGENT,
         "--set-judge", _REVIEW_AGENT,
         "--set-task", _REVIEW_TASK,
         "--local"],
        cwd=str(worktree), capture_output=True, text=True, timeout=120,
    )
    if cfg.returncode != 0:
        return f"entire review --configure failed: {(cfg.stderr or cfg.stdout).strip()[:300]}"

    listing = subprocess.run(
        [entire_path, "review", "--agents"],
        cwd=str(worktree), capture_output=True, text=True, timeout=120,
    )
    # Anchor to the agent name: a bare substring would also match a DIFFERENT
    # agent that happens to have hooks installed, and we would then skip
    # installing ours.
    if f"{_REVIEW_AGENT}: hooks installed" in (listing.stdout or ""):
        return None
    add = subprocess.run(
        [entire_path, "agent", "add", _REVIEW_AGENT],
        cwd=str(worktree), capture_output=True, text=True, timeout=120,
    )
    if add.returncode != 0:
        return f"entire agent add {_REVIEW_AGENT} failed: {(add.stderr or add.stdout).strip()[:300]}"
    return None


def _review_error_marker(output: str) -> Optional[str]:
    """Return the first error marker present in the review output, if any."""
    for marker in _REVIEW_ERROR_MARKERS:
        if marker in output:
            return marker
    return None


def run_entire_review(worktree_path: str, base_branch: str = "main") -> dict:
    """Run pre-merge review using `entire review`.

    Entire reads git checkpoints to understand developer intent, then audits
    the diff for mechanical + semantic issues.

    Returns a bookbag-compatible dict (shape kept stable for the teacher/
    bookbag consumers that predate the rename):
    - status: "pass" | "fail" | "skipped" | "error"
    - findings: list of finding dicts (real bugs only)
    - skipped: True if entire CLI not available
    - error: str or None
    - entire_replacement: "entire-review"  # marker for the pre-Entire sensor
    - review_workspace_path: str (path to captured output)
    - timestamp: ISO datetime
    """
    worktree = Path(worktree_path)
    workspace = worktree / ".hermes" / "review_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    output_file = workspace / "entire_review.md"
    timestamp = datetime.now(timezone.utc).isoformat()

    # Check if entire CLI is available
    entire_path = _get_entire_path()
    if not entire_path:
        output_file.write_text("Pre-merge review SKIPPED — entire CLI not installed.\n")
        return {
            "status": "skipped",
            "findings": [],
            "skipped": True,
            "error": "entire CLI not found on PATH",
            "entire_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    changed_files = _get_changed_files(worktree_path, base_branch)
    if changed_files is None:
        # The diff could not be computed (not a repo, missing base ref, git
        # failure). This is NOT an empty diff — reporting it as a pass would
        # launder "we never looked" into "we looked and found nothing".
        error = f"could not compute git diff vs {base_branch!r}"
        output_file.write_text(f"Pre-merge review ERROR — {error}\n")
        return {
            "status": "error",
            "findings": [],
            "skipped": False,
            "error": error,
            "entire_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }
    if not changed_files:
        output_file.write_text("No git diff found — nothing to review.\n")
        return {
            "status": "pass",
            "findings": [],
            "skipped": False,
            "error": None,
            "entire_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    # A fresh clone ships neither the review profile nor the agent hooks, so
    # `entire review` would exit 1 before reviewing anything. Bootstrap both —
    # a setup failure is an ERROR, never a silent pass.
    setup_error = _ensure_review_profile(worktree, entire_path)
    if setup_error:
        output_file.write_text(f"Pre-merge review ERROR — {setup_error}\n")
        return {
            "status": "error",
            "findings": [],
            "skipped": False,
            "error": setup_error,
            "entire_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    # Run entire review.
    #
    # `--profile general` is required: the CLI is interactive by default and
    # (on first run) opens a guided-setup wizard. The profile itself is created
    # by _ensure_review_profile above. `--format` is NOT passed — this CLI
    # version (0.11.x) rejects it as an unknown flag and would abort the review
    # before producing output.
    timeout_s = _review_timeout_s()
    try:
        result = subprocess.run(
            [entire_path, "review", "--profile", "general", "--base", base_branch],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        review_output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        review_output = f"Entire review timed out ({timeout_s}s)"
        result = None
    except Exception as e:
        review_output = f"Entire review failed: {e}"
        result = None

    # Write raw output
    output_file.write_text(review_output)

    # Parse findings
    findings = _parse_entire_output(review_output, changed_files)

    # Write structured findings alongside the raw output
    findings_file = workspace / "entire_findings.json"
    import json as _json
    findings_file.write_text(_json.dumps([f._asdict() for f in findings], indent=2))

    # FAIL-CLOSED. Three defects previously all reported "pass":
    #   * a missing profile/hooks made `entire review` exit 1;
    #   * the 120s cap killed a review that legitimately takes ~200s;
    #   * an unreachable reviewer API produced zero findings.
    # A review that did not reach a verdict must never look like a clean diff.
    exit_code = result.returncode if result is not None else None
    marker = _review_error_marker(review_output)
    if result is None or exit_code != 0 or (not findings and marker):
        error = f"entire review produced no verdict (exit={exit_code})"
        if marker:
            error += f"; {marker}"
        return {
            "status": "error",
            "findings": [f._asdict() for f in findings],
            "skipped": False,
            "error": error,
            "entire_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    # Build summary
    severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    summary_md = f"""## Pre-merge Review (Entire CLI — intent-aware, replaces qodo --improve)

**Note:** Qodo Command CLI is discontinued. Using `entire review` which reads
git checkpoints for intent-aware analysis. No API key required.

**Changed files:** {len(changed_files)}
  {', '.join(changed_files) if changed_files else '(none)'}

**Findings: {len(findings)} total**
  ⚠️ CRITICAL: {severity_counts['CRITICAL']}
  🔴 HIGH: {severity_counts['HIGH']}
  🟡 MEDIUM: {severity_counts['MEDIUM']}
  🔵 LOW: {severity_counts['LOW']}

**Status:** {'FAIL' if severity_counts['CRITICAL'] + severity_counts['HIGH'] > 0 else 'PASS'}

"""
    if findings:
        for f in findings:
            summary_md += f"\n### [{f.severity}] {f.file}:{f.line}\n{f.message}\n"

    summary_md += f"\n\n---\nRaw output: `{output_file.name}`\nFindings JSON: `{findings_file.name}`"
    (workspace / "entire_review_summary.md").write_text(summary_md)

    has_blocking = severity_counts["CRITICAL"] > 0 or severity_counts["HIGH"] > 0
    return {
        "status": "fail" if has_blocking else "pass",
        "findings": [f._asdict() for f in findings],
        "skipped": False,
        "error": None,
        "entire_replacement": "entire-review",
        "review_workspace_path": str(output_file),
        "timestamp": timestamp,
    }


if __name__ == "__main__":
    import sys
    worktree = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    result = run_entire_review(worktree, base)
    print(f"Status: {result['status']}")
    print(f"Entire replacement: {result['entire_replacement']}")
    if result["error"]:
        print(f"Error: {result['error']}")
    if result["findings"]:
        print(f"Findings: {len(result['findings'])}")
        for f in result["findings"]:
            print(f"  {f['file']}:{f['line']} [{f['severity']}] {f['message']}")
