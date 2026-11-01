#!/usr/bin/env python3
"""TDD discipline check: new source files must have corresponding tests."""

import os
import subprocess
import sys


def _changed_files():
    """Return staged + unstaged changed files (vs HEAD)."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True
    )
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _is_source(path: str) -> bool:
    return path.startswith("src/") and path.endswith(".py")


def _is_test(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py")


def _test_for_source(src: str) -> str:
    """Map src/foo/bar.py -> tests/test_foo_bar.py"""
    parts = src.split("/")
    if len(parts) < 2:
        return ""
    module = "_".join(parts[1:]).replace(".py", "")
    return f"tests/test_{module}.py"


def main() -> int:
    changed = _changed_files()
    sources = [f for f in changed if _is_source(f)]
    tests = set(f for f in changed if _is_test(f))

    missing = []
    for src in sources:
        # Only flag NEW files, not modifications to existing files
        if not os.path.exists(src):
            continue
        # Check if file is newly added (not in HEAD)
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", src],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # New untracked file
            expected_test = _test_for_source(src)
            if expected_test not in tests and not os.path.exists(expected_test):
                missing.append((src, expected_test))

    if missing:
        print("TDD gate: new source files lack corresponding tests:")
        for src, test in missing:
            print(f"  {src} -> expected {test}")
        print("Fix: add the test, or run with TDDSKIP=1 to bypass.")
        return 1

    return 0


if __name__ == "__main__":
    if os.environ.get("TDDSKIP") == "1":
        sys.exit(0)
    sys.exit(main())
