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

import json
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


def _escape_stray_quotes(block: str) -> str:
    """Escape unescaped quotes inside JSON string values.

    Measured 2026-10-22 from live run ``real-77844``: the reviewer emits

        "failure_scenario": "The `feature_func` only contains a `pass" statement."

    A bare ``"`` inside a string value closes it early, so ``json.loads``
    rejects the WHOLE block and ``_parse_json_findings`` silently drops the
    finding — the review then reports PASS with zero findings, which is the
    exact defect ``tests/test_entire_review.py::test_real_json_findings_review_
    is_not_pass`` exists to prevent.

    A quote is a real delimiter only when the next non-space character is one
    of ``,`` ``}`` ``]`` ``:`` or end-of-input; anything else is a stray quote
    inside a value and gets escaped. Already-escaped sequences are passed
    through untouched, so well-formed blocks are unchanged.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(block)
    while i < n:
        ch = block[i]
        if ch == "\\" and in_string and i + 1 < n:
            out.append(ch)
            out.append(block[i + 1])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
            else:
                j = i + 1
                while j < n and block[j] in " \t\r\n":
                    j += 1
                if j >= n or block[j] in ",}]:+":
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _load_json_block(block: str):
    """Parse a fenced block, tolerating the reviewer's malformed JSON.

    Strict parse first; on failure, retry after escaping stray quotes. Returns
    ``None`` only when the block is not parseable even after repair — the
    caller then drops it, which is the correct outcome for a block that is not
    a findings array at all.
    """
    try:
        return json.loads(block)
    except (ValueError, TypeError):
        pass
    try:
        return json.loads(_escape_stray_quotes(block))
    except (ValueError, TypeError):
        return None


def _parse_json_findings(content: str) -> list[tuple[str, int | None, str, str]]:
    """Findings from a fenced ```json array — the format real runs emit.

    Measured 2026-10-21 from live run `real-77844`: the reviewer's final report
    is a fenced JSON array of `{file, line, summary, failure_scenario}` objects,
    with NO severity field. The review task asks only for "real, actionable
    defects", so a finding the reviewer chose to emit is blocking until a human
    says otherwise; an explicit `severity` field overrides that default.

    The block is parsed with :func:`_load_json_block`, which repairs the
    unescaped-quote malformation the reviewer actually emits (see
    :func:`_escape_stray_quotes`). Without that repair every real block was
    dropped and the review reported PASS.
    """
    out: list[tuple[str, int | None, str, str]] = []
    for block in re.findall(r"```(?:json)?\s*(.*?)```", content, re.DOTALL):
        data = _load_json_block(block)
        if data is None:
            continue
        if isinstance(data, dict):
            data = data.get("findings") or [data]
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            file = str(item.get("file") or "").strip()
            if not file:
                continue
            raw_line = item.get("line")
            try:
                line_no = int(raw_line) if raw_line not in (None, "") else None
            except (TypeError, ValueError):
                line_no = None
            message = str(item.get("summary") or item.get("message") or "").strip()
            if not message:
                continue
            severity = str(item.get("severity") or "").strip().upper()
            if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                # No severity field → the reviewer chose to emit this finding,
                # and the task asked only for "real, actionable defects".
                # Blocking (CRITICAL) until a human says otherwise; an explicit
                # severity field overrides this default. This is the label the
                # docstring promises and the gate at issue_bridge.py:2700 gates
                # on — the previous HIGH default made the sensor inert.
                severity = "CRITICAL"
            out.append((file, line_no, severity, message))
    return out


_BULLET_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(?P<sev>high|medium|low)\]\s*(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_bullet_findings(content: str) -> list[tuple[str, int | None, str, str]]:
    """Findings from `- [high] file.py:42 — msg` bullets (the prompt's format)."""
    out: list[tuple[str, int | None, str, str]] = []
    for m in _BULLET_RE.finditer(content):
        severity = m.group("sev").upper()
        rest = m.group("rest").strip()
        fm = re.match(r"(?P<file>[^\s:]+):(?P<line>\d+)\s*[—–-]?\s*(?P<msg>.+)",
                      rest, re.DOTALL)
        if fm:
            out.append((fm.group("file"), int(fm.group("line")), severity,
                        fm.group("msg").strip()))
        else:
            parts = rest.split(None, 1)
            out.append((parts[0] if parts else "", None, severity,
                        parts[1] if len(parts) > 1 else rest))
    return out


_SEVERITY_LINE_RE = re.compile(r"^(?P<sev>Critical|High|Medium|Low):\s*(?P<rest>.*)$")


def _parse_severity_line_findings(content: str) -> list[tuple[str, int | None, str, str]]:
    """Findings from the legacy `Critical: file.py:42 — msg` shape."""
    out: list[tuple[str, int | None, str, str]] = []
    for line in content.split("\n"):
        m = _SEVERITY_LINE_RE.match(line.strip())
        if not m:
            continue
        severity = m.group("sev").upper()
        rest = m.group("rest").strip()
        fm = re.match(r"(?P<file>[^\s:]+):(?P<line>\d+)\s*[—–-]?\s*(?P<msg>.+)",
                      rest, re.DOTALL)
        if fm:
            out.append((fm.group("file"), int(fm.group("line")), severity,
                        fm.group("msg").strip()))
        else:
            parts = rest.split(None, 1)
            out.append((parts[0] if parts else "", None, severity,
                        parts[1] if len(parts) > 1 else rest))
    return out


def _parse_entire_output(content: str, changed_files: list[str]) -> list[EntireFinding]:
    """Parse entire review output into structured findings.

    The reviewer's output format is NOT fixed. Measured 2026-10-21 from live
    runs, it emitted a fenced ```json array of findings on one run and a bare
    verdict line on another; the review prompt additionally asks for
    `- [high] file:line — msg` bullets, and an older `Critical: file.py:42 —
    msg` shape is still accepted. All three are parsed. Anything the reviewer
    emitted in a shape none of them match is simply absent — which is why
    `_extract_verdict` matters: a run that produced no readable verdict must
    not be reported as a clean diff.
    """
    candidates: list[tuple[str, int | None, str, str]] = []
    candidates.extend(_parse_json_findings(content))
    candidates.extend(_parse_bullet_findings(content))
    candidates.extend(_parse_severity_line_findings(content))

    allowed_basenames = {os.path.basename(cf) for cf in changed_files}

    findings: list[EntireFinding] = []
    seen: set[tuple[str, int | None, str]] = set()
    for file, line_no, severity, message in candidates:
        # Filter to changed files only
        basename = os.path.basename(file) if file else ""
        if file not in changed_files and basename not in allowed_basenames:
            continue

        key = (file, line_no, message)
        if key in seen:
            continue
        seen.add(key)

        findings.append(EntireFinding(
            file=file,
            line=line_no,
            severity=severity,
            message=message,
            raw=f"{severity}: {file}:{line_no} — {message}",
        ))

    return findings


_VERDICT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\*{0,2}\s*(approve with nits|request changes|approve)\b",
    re.MULTILINE,
)


def _extract_verdict(content: str) -> Optional[str]:
    """Return the reviewer's verdict, or None if it never stated one.

    The review prompt asks the reviewer to open with a verdict line
    (`approve` / `approve with nits` / `request changes`). Measured 2026-10-21
    from live run `er-reg-58284`, which emitted exactly that line and nothing
    else. When the line is absent the run reached no conclusion — and
    `entire review` still exits 0, so the exit code cannot distinguish
    "reviewed, found nothing" from "never reviewed". Callers must treat a
    missing verdict as no verdict, not as a clean diff.
    """
    m = _VERDICT_RE.search(content or "")
    return m.group(1) if m else None


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

# The reviewer agent resolves its model from its own config when the profile
# does not pin one. On this machine that is ~/.claude/settings.json's
# ANTHROPIC_MODEL = nvidia/nvidia/nemotron-3-super-120b-a12b, whose upstream
# egress times out — so the reviewer prints "There's an issue with the selected
# model", exits 0, and reports "1 succeeded" with zero findings. Pin a model
# the agent can actually reach. Measured 2026-10-21 against OmniRoute :20128:
# auto/best-coding and auto/fast returned a completion; the nemotron model and
# auto/best-free both timed out. Override with ENTIRE_REVIEW_MODEL.
_DEFAULT_REVIEW_MODEL = "auto/best-coding"

# Markers meaning the review ran but produced no usable verdict. Any of these
# with zero parsed findings is an ERROR, never a pass.
_REVIEW_ERROR_MARKERS = (
    "no agents with review runner adapters",
    "hooks are not installed",
    "API Error:",
    "timed out",
    "Failed:",
    # `entire review` exits 0 and prints "1 succeeded" even when the reviewer
    # agent could not run, so this is the only signal that no review happened.
    "There's an issue with the selected model",
)


def _review_model() -> str:
    raw = os.getenv("ENTIRE_REVIEW_MODEL", "").strip()
    return raw or _DEFAULT_REVIEW_MODEL


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
         # Pin the model: without this the reviewer falls back to its own
         # config, which here names an upstream that times out.
         "--set-model", f"{_REVIEW_AGENT}={_review_model()}",
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

    # FAIL-CLOSED. Defects that previously all reported "pass":
    #   * a missing profile/hooks made `entire review` exit 1;
    #   * the 120s cap killed a review that legitimately takes ~200s;
    #   * an unreachable reviewer API produced zero findings;
    #   * a real review that FOUND a defect was reported as PASS because its
    #     output format (` ```json ` array) was not one the parser understood.
    # A review that did not reach a verdict must never look like a clean diff.
    exit_code = result.returncode if result is not None else None
    marker = _review_error_marker(review_output)
    verdict = _extract_verdict(review_output)
    no_conclusion = verdict is None and not findings
    if result is None or exit_code != 0 or (not findings and marker) or no_conclusion:
        error = f"entire review produced no verdict (exit={exit_code}, findings={len(findings)})"
        if marker:
            error += f"; {marker}"
        return {
            "status": "error",
            "findings": [f._asdict() for f in findings],
            "skipped": False,
            "error": error,
            "verdict": verdict,
            "entire_replacement": "entire-review",
            "review_workspace_path": str(output_file),
            "timestamp": timestamp,
        }

    # Build summary
    severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    # The reviewer's own verdict outranks the parsed findings: `request changes`
    # means it found something to fix even when its findings were emitted in a
    # shape the parser could not read.
    has_blocking = (
        severity_counts["CRITICAL"] > 0
        or severity_counts["HIGH"] > 0
        or verdict == "request changes"
    )

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

**Status:** {'FAIL' if has_blocking else 'PASS'}

**Reviewer verdict:** {verdict or '(none stated)'}

"""
    if findings:
        for f in findings:
            summary_md += f"\n### [{f.severity}] {f.file}:{f.line}\n{f.message}\n"

    summary_md += f"\n\n---\nRaw output: `{output_file.name}`\nFindings JSON: `{findings_file.name}`"
    (workspace / "entire_review_summary.md").write_text(summary_md)

    return {
        "status": "fail" if has_blocking else "pass",
        "findings": [f._asdict() for f in findings],
        "skipped": False,
        "error": None,
        "verdict": verdict,
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
