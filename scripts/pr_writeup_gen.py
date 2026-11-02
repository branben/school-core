#!/usr/bin/env python3
"""Generate docs/pr-writeup.md from source.json + review evidence, then render to HTML.

This is M2: the Tier-3 PR artifact renderer. It reads the REVIEW sidecar
(source.json) and the review/verify evidence, writes a self-contained
pr-writeup.md, and invokes html_render_md.py to produce pr-writeup.html.

Inputs:
  --source-json   path to docs/source.json
  --review-json   path to a JSON file with review evidence (cto_verdict,
                  coo_verdict, combined_score, accepted, verify_result, etc.)
  --pr-number     PR number
  --pr-title      PR title
  --pr-author     PR author
  --out-md        output markdown path (default: docs/pr-writeup.md)
  --out-html      output HTML path (default: docs/pr-writeup.html)
  --skip-render   write the .md only, do not invoke html_render_md.py

Fail-closed: missing source.json or any required field exits non-zero.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path


REQUIRED_SOURCE_FIELDS = ("bead", "spec", "decisions", "artifacts", "spec_acceptance")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _load_source(source_path: Path) -> dict:
    source = _load_json(source_path)
    if not isinstance(source, dict):
        print("ERROR: source.json is not a JSON object", file=sys.stderr)
        sys.exit(2)
    missing = [k for k in REQUIRED_SOURCE_FIELDS if not source.get(k)]
    if missing:
        print(f"ERROR: source.json missing required field(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    return source


def _render_review_section(review: dict) -> str:
    """Build the Tested section from review evidence."""
    cto = review.get("cto_verdict", "not run")
    coo = review.get("coo_verdict", "not run")
    score = review.get("combined_score")
    score_label = f"quality {score:.0f}/100 _(quality only; the verdict is the gate)_" if score is not None else "not scored"

    accepted = review.get("accepted")
    if accepted is True:
        acceptance = "**ACCEPTED** — both judges passed and no veto fired"
    elif accepted is False:
        acceptance = "**REJECTED** — see findings; a CRITICAL finding vetoes acceptance even when both verdicts read PASS"
    else:
        acceptance = "not recorded"

    verify = review.get("verify_result") or {}
    verify_cmds = verify.get("commands", [])
    verify_exit = verify.get("exit")
    if verify_cmds:
        verify_lines = "\n".join(f"    - `{c}`" for c in verify_cmds)
        verify_md = f"{verify_lines}\n    - exit: {verify_exit}"
    else:
        verify_md = "not run"

    entire = review.get("entire_review") or {}
    entire_status = entire.get("status", "not run")
    entire_findings = entire.get("findings", [])
    if entire_findings:
        entire_md = f"{entire_status} ({len(entire_findings)} finding(s))"
    else:
        entire_md = f"{entire_status} (no findings)"

    lines = [
        "| Check | How | Result |",
        "|-------|-----|--------|",
        f"| Acceptance | two-judge review | {acceptance} |",
        f"| Review quality | CTO `{cto}` / COO `{coo}` | {score_label} |",
        f"| Verify gate | commands run | {verify_md} |",
        f"| Pre-merge check (Entire) | | {entire_md} |",
    ]
    return "\n".join(lines)


def build_pr_writeup_md(
    source: dict,
    review: dict,
    pr_number: int,
    pr_title: str,
    pr_author: str,
) -> str:
    """Return the full pr-writeup.md content."""
    today = date.today().isoformat()
    decisions_str = ", ".join(source.get("decisions", []) or [])
    artifacts_str = "\n".join(f"  - `{a}`" for a in source.get("artifacts", []) or [])
    acceptance_text = source.get("spec_acceptance", "")

    tested_md = _render_review_section(review)

    return f"""---
title: "PR writeup — #{pr_number}: {pr_title}"
bead: {source.get('bead', '')}
spec: {source.get('spec', '')}
decisions: {decisions_str}
prev: review-guide.json
---

# PR writeup — #{pr_number}: {pr_title}

_Tier-3 PR-writeup render (report register). A human-facing page a reviewer can point at: what changed, why, how, and what was tested._

- **PR:** #{pr_number}
- **bead:** `{source.get('bead', '')}`
- **author:** {pr_author}
- **updated:** {today}

## What changed

- {pr_title}

## Why

This PR implements the inner-loop work for `{source.get('bead', '')}`.
The spec it lands is `{source.get('spec', '')}`.

## How

- Inner-loop chain: PRIME → PLAN → BUILD → TEST → REVIEW → PR
- Decisions implemented: {decisions_str}
- Prior artifacts cited:
{artifacts_str}

## Tested

{tested_md}

## Acceptance

{acceptance_text}

## Source chain

- **bead:** `{source.get('bead', '')}`
- **spec:** {source.get('spec', '')}
- **decisions:** {decisions_str}
- **artifacts:**
{artifacts_str}
- **acceptance:** {acceptance_text}

> Canonical source: the markdown file in this repo is authoritative.
> Regenerate with `python3 scripts/pr_writeup_gen.py ...`.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate pr-writeup.md + .html from source.json + review evidence.")
    ap.add_argument("--source-json", required=True, help="Path to docs/source.json")
    ap.add_argument("--review-json", required=True, help="Path to review evidence JSON")
    ap.add_argument("--pr-number", required=True, help="PR number")
    ap.add_argument("--pr-title", required=True, help="PR title")
    ap.add_argument("--pr-author", default="agent-school", help="PR author (default: agent-school)")
    ap.add_argument("--out-md", default="docs/pr-writeup.md", help="Output markdown path")
    ap.add_argument("--out-html", default="docs/pr-writeup.html", help="Output HTML path")
    ap.add_argument("--skip-render", action="store_true", help="Write .md only, do not render HTML")
    args = ap.parse_args(argv)

    source = _load_source(Path(args.source_json))
    review = _load_json(Path(args.review_json))

    md = build_pr_writeup_md(
        source=source,
        review=review,
        pr_number=int(args.pr_number),
        pr_title=args.pr_title,
        pr_author=args.pr_author,
    )

    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    print(f"pr_writeup_gen: wrote {out_md}")

    if args.skip_render:
        return 0

    # Render via html_render_md.py
    repo_root = Path(__file__).resolve().parent.parent
    render_script = repo_root / "scripts" / "html_render_md.py"
    if not render_script.is_file():
        print(f"ERROR: renderer not found at {render_script}", file=sys.stderr)
        return 2

    proc = subprocess.run(
        [
            sys.executable,
            str(render_script),
            str(out_md),
            "--template",
            "pr-writeup",
            "--out",
            args.out_html,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"ERROR: html_render_md.py failed:\n{proc.stderr}", file=sys.stderr)
        return proc.returncode

    print(f"pr_writeup_gen: rendered {args.out_html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
