#!/usr/bin/env python3
"""Mechanical PR review gate for school-core.

Deliberately does NOT ask a model "does this look good?". An LLM opinion
is not a merge criterion. Every assertion here is mechanically checkable,
so the gate is deterministic, cheap, and cannot be rate-limited.

Checks (all must pass):
  1. no secrets in the diff (high-signal patterns only, on ADDED lines)
  2. changed Python source has a corresponding changed test, or the PR
     touches only tests/docs/config (the "is this tested?" question)
  3. no VCS / editor / build junk in the changed-file list
  4. changed files carry no trailing whitespace on added lines

Exit 0 = pass. Exit 1 = fail, with every failure printed (never the
first one only -- a partial report hides the rest of the work).

Usage:
  review_gate.py --base origin/main --head HEAD
  review_gate.py --base origin/main --head HEAD --json
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys

# Patterns chosen for low false-positive rate: these are shapes that are
# almost always a committed credential. Generic "password" matches were
# rejected -- the repo has variables named *_SECRET legitimately.
SECRET_PATTERNS = [
    (r"ghp_[A-Za-z0-9]{36,}", "GitHub classic PAT"),
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub fine-grained PAT"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"sk-[A-Za-z0-9]{32,}", "OpenAI-style API key"),
    (r"sk-ant-[A-Za-z0-9_-]{40,}", "Anthropic API key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"AIza[0-9A-Za-z_-]{35}", "Google API key"),
    (r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "private key"),
    (r"eyJhbGciOi[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "JWT"),
]

# Allowlist of globs: changes confined to these need no test.
NO_TEST_NEEDED = [
    "*.md", "docs/*", "*.txt", "*.rst",
    "*.yml", "*.yaml", ".gitignore", ".gitattributes",
    "LICENSE*", "CHANGELOG*", "CODEOWNERS",
    "tests/*", "test/*", "*_test.py", "test_*.py",
    "*.cfg", "*.ini", "*.toml", "*.lock",
]

JUNK_PATTERNS = [
    "*.pyc", "*.pyo", "*.so", "*.o", "*.a", "*.class",
    ".DS_Store", "Thumbs.db", "*.swp", "*~",
    "*.log", "coverage.xml", ".coverage", "htmlcov/*",
    "node_modules/*", "__pycache__/*", ".pytest_cache/*",
]

TEST_SUFFIXES = (".py",)
SRC_SUFFIXES = (".py",)
# Files where trailing whitespace is a real defect (code), as opposed to
# markdown tables where it is legitimate formatting.
CODE_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".js", ".ts")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    ).stdout


def changed_files(base: str, head: str) -> list[str]:
    out = git("diff", "--name-only", f"{base}...{head}")
    return [f for f in out.splitlines() if f.strip()]


def added_lines(base: str, head: str) -> list[tuple[str, str]]:
    """Return (file, added-line) for every added line in the diff."""
    out = git("diff", "--unified=0", f"{base}...{head}")
    pairs: list[tuple[str, str]] = []
    current = ""
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            pairs.append((current, line[1:]))
    return pairs


def is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files = changed_files(args.base, args.head)
    added = added_lines(args.base, args.head)
    failures: list[dict] = []

    # 1. secrets
    for path, line in added:
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, line):
                failures.append({
                    "check": "secret",
                    "file": path,
                    "detail": f"possible {label} in an added line",
                })

    # 2. junk files
    for f in files:
        if any(fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], pat)
               for pat in JUNK_PATTERNS):
            failures.append({"check": "junk", "file": f,
                             "detail": "build/VCS/editor junk in changed files"})

    # 3. trailing whitespace on added lines.
    # Scoped to code: markdown tables legitimately use padding spaces, and
    # flagging them would fail every docs PR for a non-problem.
    for path, line in added:
        if not path.endswith(CODE_SUFFIXES):
            continue
        if line != line.rstrip() and line.strip():
            failures.append({"check": "whitespace", "file": path,
                             "detail": "added line has trailing whitespace"})

    # 4. changed source without a changed test
    if files and not any(is_test(f) for f in files):
        src = [f for f in files
               if f.endswith(SRC_SUFFIXES)
               and not is_test(f)
               and not any(fnmatch.fnmatch(f, pat) for pat in NO_TEST_NEEDED)]
        if src:
            failures.append({
                "check": "untested",
                "file": ", ".join(src[:5]),
                "detail": (f"{len(src)} source file(s) changed with no test "
                           "changed in the same PR"),
            })

    if args.json:
        print(json.dumps({
            "base": args.base, "head": args.head,
            "changedFiles": len(files), "addedLines": len(added),
            "pass": not failures, "failures": failures,
        }, indent=2))
        return 1 if failures else 0

    print(f"review-gate: {len(files)} changed file(s), {len(added)} added line(s)")
    if not failures:
        print("PASS - all mechanical checks clean")
        return 0
    print(f"FAIL - {len(failures)} finding(s):\n")
    for f in failures:
        print(f"  [{f['check']}] {f['file']}: {f['detail']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
