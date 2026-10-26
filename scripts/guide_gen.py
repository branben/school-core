#!/usr/bin/env python3
"""Generate a plannotator Guided Review ``guide.json`` from a unified diff.

The tool validates STRUCTURE, not prose: a guide that breaks any of its hard
rules is refused with exit 1 and writes no artifact. This generator's job is to
make those rules structurally impossible to break, so a CI job can gate on
``plannotator guide export`` and fail closed.

Invariants guaranteed here -- every one measured against the installed binary on
2026-10-26 (the probe suite lives in tests/test_guide_schema.py, which runs the
real tool):

  * ``title`` and ``intent`` are non-empty strings (empty is rejected).
  * ``sections`` has 1..6 entries (0 rejected as "must be a non-empty array",
    7 rejected).
  * every ``diffs[].file`` is the EXACT path from the diff with ``b/`` stripped
    (``b/src/a.py`` is rejected and the tool names the real paths).
  * no file appears in more than one section (duplicate placement is rejected).
  * files left out of the reading path go to ``unplacedFiles`` -- optional, and
    the tool does NOT require every changed file to be placed anywhere. But every
    path named in ``unplacedFiles`` must still appear in the diff: the tool rejects
    "unplacedFiles not in the patch: X". build_guide() enforces that locally.

Prose is derived mechanically from the diff: what changed, by how much, and which
top-level symbols were touched. It ORIENTS; it does not critique. That is the
calibration docs/templates/guided-review-guide.schema.json documents -- a guide,
not a review. Improve the wording by hand; do not let a generator weaken it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Reading order. The tool wants the heart first and the glue last; a deterministic
# proxy for that is: the product code, then its docs, then the tooling that
# supports them, then CI, then tests, then anything unclassified.
SECTION_ORDER = ("src", "docs", "scripts", ".github", "tests", "")
SECTION_TITLES = {
    "src": "Product code",
    "docs": "Documentation",
    "scripts": "Scripts and tooling",
    ".github": "CI and repository policy",
    "tests": "Tests",
    "": "Everything else",
}
MAX_SECTIONS = 6

_SYMBOL_PATTERNS = (
    re.compile(r"^\s*def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*async\s+def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:export\s+)?(?:function|const|let|var)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:def|defp|defmodule|defmacro)\s+([A-Za-z_]\w*)"),
)


@dataclass
class DiffFile:
    """One file in a unified diff, reduced to what a guide needs to say."""

    path: str
    old_path: str = ""
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    added: int = 0
    removed: int = 0
    symbols_added: list[str] = field(default_factory=list)
    symbols_removed: list[str] = field(default_factory=list)


def _strip_ab(path: str) -> str:
    """``b/src/a.py`` -> ``src/a.py``. The tool rejects the prefixed form."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_diff(text: str) -> list[DiffFile]:
    """Parse a unified diff into per-file records.

    Deliberately tolerant: an unparseable diff yields an empty list and the caller
    fails closed rather than emitting a guide against nothing.
    """
    files: list[DiffFile] = []
    current: DiffFile | None = None
    old_path = ""
    new_path = ""

    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                files.append(_finalize(current))
            current = None
            old_path = new_path = ""
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if match:
                old_path, new_path = match.group(1), match.group(2)
                current = DiffFile(path=_strip_ab(new_path), old_path=_strip_ab(old_path))
            continue

        if current is None:
            continue

        if line.startswith("new file mode"):
            current.is_new = True
        elif line.startswith("deleted file mode"):
            current.is_deleted = True
            current.path = _strip_ab(old_path) or current.path
        elif line.startswith("rename from "):
            current.is_rename = True
            current.old_path = _strip_ab(line[len("rename from "):].strip()) or current.old_path
        elif line.startswith("rename to "):
            current.is_rename = True
            current.path = _strip_ab(line[len("rename to "):].strip())
        elif line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                current.path = _strip_ab(target)
        elif line.startswith("--- "):
            source = line[4:].strip()
            if source != "/dev/null":
                current.old_path = _strip_ab(source)
        elif line.startswith("+") and not line.startswith("+++"):
            current.added += 1
            _collect_symbols(current.symbols_added, line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            current.removed += 1
            _collect_symbols(current.symbols_removed, line[1:])

    if current is not None:
        files.append(_finalize(current))
    return files


def _finalize(f: DiffFile) -> DiffFile:
    f.symbols_added = _dedupe(f.symbols_added)
    f.symbols_removed = _dedupe(f.symbols_removed)
    return f


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _collect_symbols(bucket: list[str], line: str) -> None:
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.match(line)
        if match:
            bucket.append(match.group(1))
            return


def section_key(path: str) -> str:
    """Map a path to its reading-order bucket."""
    top = path.split("/", 1)[0]
    if top == "src" and "/" in path:
        return "src"
    return top if top in SECTION_TITLES else ""


def _summary(f: DiffFile) -> str:
    """Mechanical, factual summary. Orientation, never critique."""
    bits: list[str] = []
    if f.is_new:
        bits.append("New file.")
    elif f.is_deleted:
        bits.append("Deleted.")
    elif f.is_rename:
        bits.append(f"Renamed from {f.old_path}.")
    else:
        bits.append(f"+{f.added} / -{f.removed} lines.")
    if f.symbols_added:
        shown = ", ".join(f.symbols_added[:6])
        more = f" (+{len(f.symbols_added) - 6} more)" if len(f.symbols_added) > 6 else ""
        bits.append(f"Adds: {shown}{more}.")
    if f.symbols_removed:
        shown = ", ".join(f.symbols_removed[:6])
        more = f" (+{len(f.symbols_removed) - 6} more)" if len(f.symbols_removed) > 6 else ""
        bits.append(f"Removes: {shown}{more}.")
    return " ".join(bits)


def group_sections(files: list[DiffFile]) -> list[dict]:
    """Bucket the files into 1..MAX_SECTIONS ordered sections.

    Merging is deterministic (smallest buckets first, ties by name) so the same
    diff always yields the same guide.
    """
    if not files:
        return []

    buckets: dict[str, list[DiffFile]] = {}
    for f in files:
        buckets.setdefault(section_key(f.path), []).append(f)

    while len(buckets) > MAX_SECTIONS:
        smallest = min(buckets, key=lambda k: (len(buckets[k]), k))
        target = next(
            (k for k in sorted(buckets) if k != smallest),
            None,
        )
        if target is None:
            break
        buckets[target].extend(buckets.pop(smallest))

    sections: list[dict] = []
    ordered = sorted(buckets, key=lambda k: (SECTION_ORDER.index(k) if k in SECTION_ORDER else len(SECTION_ORDER), k))
    for key in ordered:
        members = sorted(buckets[key], key=lambda f: f.path)
        sections.append(
            {
                "title": SECTION_TITLES.get(key, key or "Everything else"),
                "overview": _overview(key, members),
                "diffs": [{"file": f.path, "summary": _summary(f)} for f in members],
            }
        )
    return sections


def _overview(key: str, members: list[DiffFile]) -> str:
    names = ", ".join(f.path for f in members[:4])
    more = f" and {len(members) - 4} more" if len(members) > 4 else ""
    lead = {
        "src": "The product-code changes this guide orients you to.",
        "docs": "Documentation that describes or depends on the change.",
        "scripts": "Tooling that supports the change.",
        ".github": "CI and repository policy.",
        "tests": "Tests covering the change -- read these last, after the behaviour they lock in.",
        "": "Files that do not fit the other sections.",
    }.get(key, "Files in this section.")
    return f"{lead} Covers: {names}{more}."


def build_guide(files: list[DiffFile], title: str, intent: str, unplaced: list[str] | None = None) -> dict:
    """Assemble the guide dict. Fails closed if the input cannot satisfy the tool."""
    if not title or not title.strip():
        raise ValueError("guide title must be a non-empty string")
    if not intent or not intent.strip():
        raise ValueError("guide intent must be a non-empty string")

    sections = group_sections(files)
    if not sections:
        raise ValueError(
            "no files parsed from the diff; refusing to emit an empty guide"
        )
    if len(sections) > MAX_SECTIONS:
        raise ValueError(f"{len(sections)} sections exceeds the tool's maximum of {MAX_SECTIONS}")

    unplaced_set = set(unplaced or [])
    known = {f.path for f in files}
    unknown = sorted(unplaced_set - known)
    if unknown:
        raise ValueError(
            "unplacedFiles name(s) absent from the diff; the tool rejects them "
            f"with 'unplacedFiles not in the patch': {unknown}"
        )

    placed = [d["file"] for s in sections for d in s["diffs"]]
    duplicated = sorted({p for p in placed if placed.count(p) > 1})
    if duplicated:
        raise ValueError(f"file(s) placed in more than one section: {duplicated}")

    return {
        "title": title.strip(),
        "intent": intent.strip(),
        "sections": sections,
        "unplacedFiles": sorted(set(unplaced or [])),
    }


def get_patch(args: argparse.Namespace) -> str:
    """Resolve the diff text from a file, a git range, or a GitHub PR."""
    if args.patch:
        text = Path(args.patch).read_text(encoding="utf-8")
        if not text.strip():
            raise SystemExit(f"--patch {args.patch} is empty")
        return text
    if args.pr:
        out = subprocess.run(
            ["gh", "pr", "diff", str(args.pr), "--patch"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise SystemExit(f"gh pr diff {args.pr} failed: {out.stderr.strip()}")
        return out.stdout
    if args.base and args.head:
        out = subprocess.run(
            ["git", "diff", f"{args.base}...{args.head}"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise SystemExit(f"git diff failed: {out.stderr.strip()}")
        return out.stdout
    raise SystemExit("provide --patch, --pr, or both --base and --head")


def export(guide_path: Path, patch_path: Path, out_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "plannotator", "guide", "export",
            "--guide", str(guide_path),
            "--patch", str(patch_path),
            "--out", str(out_path),
        ],
        capture_output=True, text=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--patch", help="path to a unified diff file")
    parser.add_argument("--pr", type=int, help="GitHub PR number; diff fetched with `gh pr diff`")
    parser.add_argument("--base", help="git base ref (with --head)")
    parser.add_argument("--head", help="git head ref (with --base)")
    parser.add_argument("--title", default="Guided review", help="guide title")
    parser.add_argument("--intent", default="Orient the reviewer to what changed and in what order to read it.",
                        help="guide intent")
    parser.add_argument("--unplaced", action="append", default=[],
                        help="path to leave out of the reading path (repeatable)")
    parser.add_argument("--out", type=Path, default=Path("guide.json"), help="where to write guide.json")
    parser.add_argument("--export", type=Path, default=None,
                        help="also run `plannotator guide export` and write HTML here; fail closed on rejection")
    args = parser.parse_args(argv)

    text = get_patch(args)
    files = parse_diff(text)
    if not files:
        print("guide_gen: no files parsed from the diff", file=sys.stderr)
        return 1

    try:
        guide = build_guide(files, args.title, args.intent, args.unplaced)
    except ValueError as exc:
        print(f"guide_gen: {exc}", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(guide, indent=2) + "\n", encoding="utf-8")
    n_sections = len(guide["sections"])
    n_files = sum(len(s["diffs"]) for s in guide["sections"])
    print(f"guide_gen: wrote {args.out} -- {n_files} file(s) in {n_sections} section(s)")

    if args.export is not None:
        patch_path = args.patch or Path("/dev/null")
        if not args.patch:
            patch_path = args.out.with_suffix(".patch")
            patch_path.write_text(text, encoding="utf-8")
        proc = export(args.out, patch_path, args.export)
        if proc.returncode != 0:
            print(
                f"guide_gen: plannotator REJECTED the guide (exit {proc.returncode}):\n"
                f"{proc.stderr.strip() or proc.stdout.strip()}",
                file=sys.stderr,
            )
            return proc.returncode
        print(f"guide_gen: exported {args.export}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
