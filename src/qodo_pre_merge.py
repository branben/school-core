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
    from src.qodo_pre_merge import run_qodo_improve
    result = run_qodo_improve(worktree_path="/path/to/worktree")
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


class QodoFinding(NamedTuple):
    """Finding from the review (naming kept for bookbag compatibility)."""
    file: str
    line: int | None
    severity: str  # "CRITICAL", "HIGH", "MEDIUM", "LOW"
    message: str
    raw: str


def _get_entire_path() -> str | None:
    """Find the entire CLI binary."""
    return shutil.which("entire")


def _get_changed_files(worktree_path: str, base_branch: str = "main") -> list[str]:
    """Get list of changed files for targeted review."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def _parse_entire_output(content: str, changed_files: list[str]) -> list[QodoFinding]:
    """Parse entire review output into structured findings.

    Entire review outputs lines prefixed with severity:
      Critical: file.py:42 — description
      High: file.py:17 — description
      Medium: file.py:3 — description
      Low: file.py:88 — description
    """
    findings: list[QodoFinding] = []

    # Track current file context for findings without line numbers
    current_file = ""

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

                findings.append(QodoFinding(
                    file=file,
                    line=line_no,
                    severity=severity.upper(),
                    message=message,
                    raw=line,
                ))
                if file:
                    current_file = file
                break

        # Track file headers (Entire may output file sections)
        if line.startswith("### ") or line.startswith("# "):
            current_file = line.lstrip("# ").strip()

    # Deduplicate findings with same file+line+message
    seen = set()
    unique = []
    for f in findings:
        key = (f.file, f.line, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return unique


def run_qodo_improve(worktree_path: str, base_branch: str = "main") -> dict:
    """Run pre-merge review using `entire review`.

    Replaces the deprecated `qodo --improve` with Entire CLI's intent-aware
    review. Entire reads git checkpoints to understand developer intent,
    then audits the diff for mechanical + semantic issues.

    Returns dict matching the old qodo interface for bookbag compatibility:
    - status: "pass" | "fail" | "skipped" | "error"
    - findings: list of finding dicts (real bugs only)
    - skipped: True if entire CLI not available
    - error: str or None
    - qodo_replacement: "entire-review"
    - review_workspace_path: str (path to captured output)
    - timestamp: ISO datetime
    """
    worktree = Path(worktree_path)
    workspace = worktree / ".hermes" / "review_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    output_file = workspace / "qodo_improve.md"
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
            "qodo_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    changed_files = _get_changed_files(worktree_path, base_branch)
    if not changed_files:
        output_file.write_text("No git diff found — nothing to review.\n")
        return {
            "status": "pass",
            "findings": [],
            "skipped": False,
            "error": None,
            "qodo_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    # Run entire review
    try:
        result = subprocess.run(
            ["entire", "review", "--base", base_branch, "--format", "text"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        review_output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        review_output = "Entire review timed out (120s)"
        result = None
    except Exception as e:
        review_output = f"Entire review failed: {e}"
        result = None

    # Write raw output
    output_file.write_text(review_output)

    # Parse findings
    findings = _parse_entire_output(review_output, changed_files)

    # Write structured findings alongside the raw output
    findings_file = workspace / "qodo_findings.json"
    import json as _json
    findings_file.write_text(_json.dumps([f._asdict() for f in findings], indent=2))

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
    (workspace / "qodo_review_summary.md").write_text(summary_md)

    has_blocking = severity_counts["CRITICAL"] > 0 or severity_counts["HIGH"] > 0
    return {
        "status": "fail" if has_blocking else "pass",
        "findings": [f._asdict() for f in findings],
        "skipped": False,
        "error": None,
        "qodo_replacement": "entire-review",
        "review_workspace_path": str(output_file),
        "timestamp": timestamp,
    }


if __name__ == "__main__":
    import sys
    worktree = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    result = run_qodo_improve(worktree, base)
    print(f"Status: {result['status']}")
    print(f"Qodo replacement: {result['qodo_replacement']}")
    if result["error"]:
        print(f"Error: {result['error']}")
    if result["findings"]:
        print(f"Findings: {len(result['findings'])}")
        for f in result["findings"]:
            print(f"  {f['file']}:{f['line']} [{f['severity']}] {f['message']}")
