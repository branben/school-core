"""Director Console — CLI entry point.

Usage:
    director queue [--format=md|json|brief] [--bucket=ready_to_work|...]
                   [--limit=N] [--include-context]

The command reads ``bd ready --json``, classifies each issue into an
attention bucket, and renders a single-screen projection of what needs
the Director's attention right now.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from director_console.queue import (
    BUCKET_LABELS,
    Bucket,
    build_queue,
    fetch_ready_issues,
)


def _render_brief(items, file=None) -> None:
    """Single-screen text output (default)."""
    if file is None:
        file = sys.stdout
    if not items:
        print("Nothing needs Director attention.", file=file)
        return

    print(f"Director Queue — {len(items)} item(s) need attention\n", file=file)

    # Group by bucket for display.
    by_bucket: dict[str, list] = {}
    for item in items:
        by_bucket.setdefault(item.bucket.value, []).append(item)

    bucket_order = [
        Bucket.READY_TO_WORK.value,
        Bucket.AGENT_IN_FLIGHT.value,
        Bucket.NEEDS_REVIEW.value,
        Bucket.WAITING_ON_EXTERNAL.value,
        Bucket.NEEDS_TRIAGE.value,
    ]

    for bucket_key in bucket_order:
        bucket_items = by_bucket.get(bucket_key)
        if not bucket_items:
            continue
        label = BUCKET_LABELS.get(bucket_key, bucket_key)
        print(f"[{label}]", file=file)
        for item in items_in_bucket(bucket_items):
            print(f"  ○ {item.id} P{item.priority} {item.title}", file=file)
            print(f"    → {item.recommended_action}", file=file)
            if item.kc_plan_anchor:
                active_str = "active" if item.kc_plan_active else "inactive/missing"
                print(f"    plan: {item.kc_plan_anchor} ({active_str})", file=file)
            if item.blast_radius:
                # Show first line of blast-radius output.
                summary = item.blast_radius.splitlines()[0][:120] if item.blast_radius else ""
                if summary:
                    print(f"    ripwire: {summary}", file=file)
        print(file=file)


def items_in_bucket(bucket_items):
    """Filter helper — returns items as-is, kept separate for testability."""
    return bucket_items


def _render_json(items, file=None) -> None:
    """Machine-readable JSON output."""
    if file is None:
        file = sys.stdout
    payload = {
        "count": len(items),
        "items": [i.to_dict() for i in items],
    }
    json.dump(payload, file, indent=2, default=str)
    print(file=file)


def _render_md(items, file=None) -> None:
    """Markdown output for pasting into issues/PRs."""
    if file is None:
        file = sys.stdout
    if not items:
        print("**Director Queue:** Nothing needs Director attention.\n", file=file)
        return

    print(f"## Director Queue — {len(items)} item(s)\n", file=file)
    for item in items:
        print(f"- **{item.id}** (P{item.priority}, {item.bucket.value})  ", file=file)
        print(f"  {item.title}  ", file=file)
        print(f"  → *{item.recommended_action}*", file=file)
    print(file=file)


FORMATTERS = {
    "brief": _render_brief,
    "json": _render_json,
    "md": _render_md,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="director queue",
        description="Director Console — attention queue projection.",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json", "brief"],
        default="brief",
        help="Output format (default: brief)",
    )
    parser.add_argument(
        "--bucket",
        choices=[b.value for b in Bucket],
        default=None,
        help="Filter to a single attention bucket",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of items to display",
    )
    parser.add_argument(
        "--include-context",
        action="store_true",
        default=False,
        help="Attach ripwire blast-radius context to each item",
    )

    args = parser.parse_args(argv)

    # 1. Fetch candidate issues from bd.
    raw_issues = fetch_ready_issues()

    # 2-4. Classify into buckets.
    items = build_queue(raw_issues, include_context=args.include_context)

    # 5. Apply bucket filter if requested.
    if args.bucket:
        items = [i for i in items if i.bucket.value == args.bucket]

    # 6. Apply limit.
    if args.limit is not None:
        items = items[: args.limit]

    # 7. Render.
    formatter = FORMATTERS.get(args.format, _render_brief)
    formatter(items)

    return 0


if __name__ == "__main__":
    sys.exit(main())
