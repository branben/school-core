#!/usr/bin/env python3
"""Enforce the curated vault allowlist.

The repository's knowledge vault (`data/vault/`) must contain ONLY files that
match the allowed patterns declared in `config/vault_allowlist.yaml`. This is
the privacy/security contract that keeps personal Obsidian data out of the
repo and out of agent prompts.

Usage:
    python scripts/check_vault_allowlist.py [--vault data/vault] [--strict]
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VAULT = REPO_ROOT / "data" / "vault"
DEFAULT_ALLOWLIST = REPO_ROOT / "config" / "vault_allowlist.yaml"
MANIFEST_NAME = ".vault_manifest.json"


def load_allowlist(path: Path) -> list[str]:
    """Load allowed glob patterns from the allowlist YAML (top-level `patterns:`)."""
    if not path.exists():
        raise FileNotFoundError(f"allowlist not found: {path}")
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    patterns = data.get("patterns") or []
    if not isinstance(patterns, list):
        raise ValueError(f"`patterns:` must be a list in {path}")
    # Sanity guard: the manifest marker is always allowed so the check can run.
    if MANIFEST_NAME not in patterns:
        patterns.append(MANIFEST_NAME)
    return [str(p) for p in patterns]


def allowed(path: Path, patterns: list[str]) -> bool:
    """True if the path matches any allowlist glob.

    `**` crosses directory boundaries; single `*` does not. This mirrors
    shell glob semantics (unlike fnmatch, which lets `*` match `/`).
    """
    rel = path.as_posix()
    for pat in patterns:
        if "**" in pat:
            rx = re.escape(pat).replace(r"\*\*", r".*")
            if re.match(f"^{rx}$", rel):
                return True
        elif path.match(pat):
            return True
    return False


def scan_vault(vault: Path, patterns: list[str]) -> list[Path]:
    """Return all files in the vault that violate the allowlist."""
    violations = []
    for p in vault.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(vault).as_posix()
        if rel == MANIFEST_NAME:
            continue
        if not allowed(Path(rel), patterns):
            violations.append(p)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero even if vault dir is missing")
    args = parser.parse_args()

    if not args.vault.exists():
        if args.strict:
            print(f"ERROR: vault directory missing: {args.vault}", file=sys.stderr)
            return 1
        print("vault directory absent — OK (nothing to enforce)")
        return 0

    patterns = load_allowlist(DEFAULT_ALLOWLIST)
    if not patterns:
        print("ERROR: no allowed patterns found in allowlist", file=sys.stderr)
        return 1

    violations = scan_vault(args.vault, patterns)
    if violations:
        print(f"ERROR: {len(violations)} file(s) violate the vault allowlist:", file=sys.stderr)
        for v in violations:
            print(f"  - {v.relative_to(args.vault)}", file=sys.stderr)
        print("Personal/private data must not enter the repo vault.", file=sys.stderr)
        return 1

    print(f"OK: vault allowlist clean ({args.vault})")
    return 0


if __name__ == "__main__":
    sys.exit(main())