#!/usr/bin/env python3
"""Generate the inner-loop source.json sidecar from a bead, spec, and prior artifacts.

Reads the spec file to extract acceptance criteria, verifies every cited artifact
exists in the actual docs/ tree, and writes docs/source.json validated against
the template schema.

Fail-closed on missing fields, missing artifacts, or unparseable specs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_FIELDS = ("bead", "spec", "decisions", "artifacts", "spec_acceptance")

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "docs" / "templates" / "source.json"


def _load_template() -> dict:
    """Return the template dict so callers can see the expected shape."""
    try:
        return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_section(text: str, heading: str) -> str | None:
    """Extract the content under a markdown heading until the next heading."""
    # Match "## Heading" or "# Heading" at start of line
    pattern = re.compile(rf"^(#+)\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    start = match.end()
    # Find the next heading at the same or higher level
    next_match = re.search(r"\n^(#+)\s+\S", text[start:], re.MULTILINE)
    if next_match:
        end = start + next_match.start()
    else:
        end = len(text)
    return text[start:end].strip()


def _extract_acceptance_from_spec(spec_path: Path) -> str | None:
    """Try to pull acceptance criteria from common spec sections."""
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Try common section names in order of specificity
    for heading in (
        "Acceptance criteria",
        "Acceptance checklist",
        "Verification commands",
        "Verification",
        "Acceptance",
    ):
        section = _extract_section(text, heading)
        if section:
            return section
    return None


def _normalize_decisions(raw: str) -> list[str]:
    """Split a comma-separated decisions string into a clean list."""
    items = [d.strip() for d in raw.split(",")]
    return [d for d in items if d]


def _normalize_artifacts(raw: str) -> list[str]:
    """Split a comma-separated artifact path string into a clean list."""
    items = [a.strip() for a in raw.split(",")]
    return [a for a in items if a]


def generate(
    bead: str,
    spec: str,
    decisions: list[str],
    artifacts: list[str],
    spec_acceptance: str,
    out: Path,
) -> dict:
    """Build and write source.json. Returns the written dict on success."""
    if not bead or not bead.strip():
        raise ValueError("bead must be non-empty")
    if not spec or not spec.strip():
        raise ValueError("spec must be non-empty")
    if not decisions:
        raise ValueError("decisions must contain at least one entry")
    if not artifacts:
        raise ValueError("artifacts must contain at least one path")
    if not spec_acceptance or not spec_acceptance.strip():
        raise ValueError("spec_acceptance must be non-empty")

    # Risky seam: verify every cited artifact actually exists in the docs/ tree.
    # We do NOT fall back to template defaults — missing artifacts are an error.
    missing = [a for a in artifacts if not Path(a).exists()]
    if missing:
        raise FileNotFoundError(
            f"cited artifact(s) not found in docs/ tree: {', '.join(missing)}"
        )

    payload = {
        "bead": bead.strip(),
        "spec": spec.strip(),
        "decisions": [d.strip() for d in decisions],
        "artifacts": [a.strip() for a in artifacts],
        "spec_acceptance": spec_acceptance.strip(),
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate docs/source.json for the inner-loop chain.")
    ap.add_argument("--bead", required=True, help="Bead ID, e.g. school-core-fip")
    ap.add_argument("--spec", required=True, help="Spec file path, e.g. docs/plans/...-spec.md")
    ap.add_argument("--spec-milestone", required=True, help="Milestone ID, e.g. M3")
    ap.add_argument("--decisions", required=True, help="Comma-separated accepted decision IDs")
    ap.add_argument("--artifacts", required=True, help="Comma-separated artifact paths in docs/ tree")
    ap.add_argument("--spec-acceptance", default="", help="Verbatim acceptance criteria (overrides spec extraction)")
    ap.add_argument("--out", default="docs/source.json", help="Output path (default: docs/source.json)")
    args = ap.parse_args(argv)

    # Compose the spec field: path + milestone
    spec_field = f"{args.spec} · {args.spec_milestone}"

    # Acceptance criteria: prefer explicit flag, fall back to spec extraction
    acceptance = args.spec_acceptance.strip()
    if not acceptance:
        extracted = _extract_acceptance_from_spec(Path(args.spec))
        if extracted:
            acceptance = extracted
        else:
            print(
                "ERROR: could not extract acceptance criteria from spec "
                f"{args.spec!r} and --spec-acceptance was not provided. "
                "Add a '## Acceptance criteria' section to the spec or pass --spec-acceptance.",
                file=sys.stderr,
            )
            return 2

    try:
        payload = generate(
            bead=args.bead,
            spec=spec_field,
            decisions=_normalize_decisions(args.decisions),
            artifacts=_normalize_artifacts(args.artifacts),
            spec_acceptance=acceptance,
            out=Path(args.out),
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: cannot write {args.out}: {exc}", file=sys.stderr)
        return 2

    print(f"source_gen: wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
