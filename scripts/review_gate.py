#!/usr/bin/env python3
"""Mechanical PR review gate for school-core.

Deliberately does NOT ask a model "does this look good?". An LLM opinion
is not a merge criterion. Every assertion here is mechanically checkable,
so the gate is deterministic, cheap, and cannot be rate-limited.

Checks (all must pass):
  1. no secrets in the diff (high-signal patterns only, on ADDED lines)
  2. every changed Python source file is named by at least one test in the
     repo -- NOT merely "a test file changed somewhere in this PR", which is
     what let three real defects through: a successful bot call returned as
     an MCP error, a parse_failed flag that was permanently false, and a test
     that left 11 scratch issues in the real database. Each of those PRs did
     change a test file; none exercised the broken path.
  3. no VCS / editor / build junk in the changed-file list
  4. changed files carry no trailing whitespace on added lines
  5. source.json (if present in the PR) carries the inner-loop chain-of-custody
     block — bead, spec, decisions, artifacts (four prior inner-loop files),
     and spec_acceptance. Every cited artifact must exist in the PR diff or
     base tree. Exit non-zero with `SOURCE-BLOCK MISSING — <which field>`.

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
from pathlib import Path

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
    """Run git and return stdout.

    Fails LOUDLY on a non-zero exit. Swallowing a git failure here is what
    let an unresolvable --head ref look like an empty diff, which turned
    this gate into a permanent PASS (see resolve_ref).
    """
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(
            "review_gate: git " + " ".join(args)
            + f" failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def resolve_ref(ref: str) -> str:
    """Verify `ref` names a real commit and return its sha.

    A gate that cannot resolve its own refs must FAIL, not pass: an
    unresolvable ref and a genuinely empty PR both yield an empty diff, and
    only one of those is a legitimate pass.
    """
    sha = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    if not sha:
        raise SystemExit(f"review_gate: cannot resolve ref {ref!r}")
    return sha


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


def _test_source_blob() -> str:
    """Concatenated text of every test file, for module-name lookup.

    Deliberately a plain substring scan: the only question is "does some test
    name this module". An import-graph parser would add a dependency and a
    new failure mode without adding signal.
    """
    # Resolve from the repo BEING GATED, not from this script's location.
    # The gate is exercised against fixture repos in tests/, and reading this
    # repo's own tests/ there made a fixture diff pass that should have failed
    # (the fixture's module name appeared as a substring of real test names).
    try:
        root = Path(git("rev-parse", "--show-toplevel").strip()) / "tests"
    except Exception:
        return ""
    if not root.is_dir():
        return ""
    chunks: list[str] = []
    for p in sorted(root.rglob("*.py")):
        # The filename is evidence too: by convention tests/test_module.py
        # covers module.py even when the file never spells the name out.
        chunks.append(p.name)
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _load_json(base: str, head: str, path: str) -> dict:
    """Return the parsed JSON of `path` at `head`, or from `base` if not changed."""
    # Try HEAD first (the version being gated), then base for pre-existing files.
    for ref in (head, base):
        try:
            blob = git("show", f"{ref}:{path}")
        except SystemExit:
            continue
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


def _check_source_block(base: str, head: str, files: list[str]) -> list[dict]:
    """Verify the PR's source.json sidecar carries the complete inner-loop chain.

    This check only fires when source.json is in the changed files. A PR that
    does not touch the REVIEW artifact is not affected.
    Returns a list of failures (empty = pass).
    """
    source_path = next((f for f in files if f.endswith("source.json")), None)
    if not source_path:
        return []

    source = _load_json(base, head, source_path)
    if not isinstance(source, dict):
        return [{"check": "source-block", "file": source_path,
                 "detail": "SOURCE-BLOCK MISSING — source.json is not a JSON object"}]

    missing = [k for k in ("bead", "spec", "decisions", "artifacts", "spec_acceptance")
               if not source.get(k) or (isinstance(source[k], list) and not source[k])]
    if missing:
        return [{"check": "source-block", "file": source_path,
                 "detail": f"SOURCE-BLOCK MISSING — required field(s) empty: {', '.join(missing)}"}]

    # Every cited artifact must exist in the PR diff or already in the base tree.
    try:
        tree = git("ls-tree", "-r", "--name-only", head).splitlines()
    except SystemExit:
        tree = []
    reachable = set(tree) | set(files)
    absent = [a for a in source["artifacts"] if a not in reachable]
    if absent:
        return [{"check": "source-block", "file": source_path,
                 "detail": f"SOURCE-BLOCK MISSING — cited artifact(s) not found in PR or base: {', '.join(absent)}"}]

    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # Fail closed before diffing: an unresolvable ref is a gate bug, not an
    # empty PR, and must never be reported as a clean pass.
    resolve_ref(args.base)
    resolve_ref(args.head)

    files = changed_files(args.base, args.head)
    added = added_lines(args.base, args.head)
    failures: list[dict] = []

    if not files:
        failures.append({
            "check": "empty-diff",
            "file": f"{args.base}...{args.head}",
            "detail": ("no changed files resolved between base and head; "
                       "the gate cannot pass on an empty diff"),
        })

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

    # 4. every changed source file must be NAMED by a test.
    #
    # The previous version required only that some test file changed anywhere
    # in the PR. Three real defects passed that gate: a successful bot call
    # returned as an MCP error, a parse_failed flag that was permanently
    # false, and a test that left 11 scratch issues in the real database.
    # Each PR did change a test file -- just not one that exercised the
    # broken path. "A test changed" is not "this code is tested", and a gate
    # that cannot tell the difference is a checklist, not a verification.
    src = [f for f in files
           if f.endswith(SRC_SUFFIXES)
           and not is_test(f)
           and not any(fnmatch.fnmatch(f, pat) for pat in NO_TEST_NEEDED)]
    if src:
        blob = _test_source_blob()
        if not blob:
            # Fail closed: a missing tests/ tree must never read as "clean".
            failures.append({
                "check": "untested",
                "file": "tests/",
                "detail": ("no test tree found; the gate cannot verify that "
                           "changed source is covered"),
            })
        else:
            for f in src:
                module = f.rsplit("/", 1)[-1][:-3]
                if module not in blob:
                    failures.append({
                        "check": "untested",
                        "file": f,
                        "detail": (f"no test names module {module!r}: changed "
                                   "source with no covering test"),
                    })

    # 5. guide.json source-block linkage (inner-loop chain of custody).
    failures.extend(_check_source_block(args.base, args.head, files))

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
