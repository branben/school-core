"""Calibration queries — aggregate run-log stats and compare Sentinel vs ship-safe.

The ``director calibration --since 30d`` command reads the run log,
compares each Sentinel verdict against ship-safe ground truth, and prints
a calibration report. False refutations (Sentinel PASS but ship-safe FAIL)
are flagged for Director review.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from director_console.run_log import read_records


def _parse_dt(raw: str) -> datetime:
    """Parse an ISO datetime string, defaulting to UTC."""
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _avg_run_time_minutes(records: list[dict[str, Any]]) -> Optional[float]:
    """Compute average run time in minutes (dispatched_at → completed_at).

    Returns None if no records have both timestamps.
    """
    durations: list[float] = []
    for rec in records:
        d_raw = rec.get("dispatched_at")
        c_raw = rec.get("completed_at")
        if not d_raw or not c_raw:
            continue
        d = _parse_dt(str(d_raw))
        c = _parse_dt(str(c_raw))
        delta = (c - d).total_seconds()
        if delta >= 0:
            durations.append(delta / 60.0)
    if not durations:
        return None
    return sum(durations) / len(durations)


def compute_calibration(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict[str, Any]:
    """Aggregate calibration stats from the run log.

    Returns a dict with:
        total, settled, false_refutations, false_refutation_rate,
        ship_safe_agreement, ship_safe_agreement_rate, avg_run_time_min
    """
    records = read_records(since=since, until=until)
    total = len(records)

    settled = sum(1 for r in records if r.get("settled"))

    # False refutation: Sentinel said PASS but ship-safe said FAIL.
    false_ref = 0
    ship_safe_matched = 0
    ship_safe_total = 0

    for rec in records:
        sentinel = rec.get("verdict")
        ship = rec.get("ship_safe_verdict")

        if ship:
            ship_safe_total += 1
            if sentinel == ship:
                ship_safe_matched += 1
            elif sentinel == "PASS" and ship == "FAIL":
                false_ref += 1

    false_ref_rate = (false_ref / total * 100) if total else 0.0
    settled_rate = (settled / total * 100) if total else 0.0
    ship_agree_rate = (
        (ship_safe_matched / ship_safe_total * 100) if ship_safe_total else None
    )
    avg_rt = _avg_run_time_minutes(records)

    return {
        "total": total,
        "settled": settled,
        "settled_rate": settled_rate,
        "false_refutations": false_ref,
        "false_refutation_rate": false_ref_rate,
        "ship_safe_total": ship_safe_total,
        "ship_safe_agreement": ship_safe_matched,
        "ship_safe_agreement_rate": ship_agree_rate,
        "avg_run_time_min": avg_rt,
    }


def format_report(stats: dict[str, Any]) -> str:
    """Render a calibration report as a human-readable string."""
    lines: list[str] = []
    lines.append("Director Calibration Report")
    lines.append("=" * 40)
    lines.append(f"Total runs: {stats['total']}")
    lines.append(
        f"Settled: {stats['settled']}/{stats['total']} "
        f"({stats['settled_rate']:.1f}%)"
    )
    lines.append(
        f"False refutations: {stats['false_refutations']} "
        f"({stats['false_refutation_rate']:.1f}%)"
    )
    if stats["ship_safe_agreement_rate"] is not None:
        lines.append(
            f"Ship-safe agreement: {stats['ship_safe_agreement']}/"
            f"{stats['ship_safe_total']} "
            f"({stats['ship_safe_agreement_rate']:.1f}%)"
        )
    else:
        lines.append("Ship-safe agreement: N/A (no ship-safe verdicts found)")
    if stats["avg_run_time_min"] is not None:
        lines.append(f"Avg run time: {stats['avg_run_time_min']:.1f} min")
    else:
        lines.append("Avg run time: N/A (no completed runs)")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for ``director calibration``."""
    parser = argparse.ArgumentParser(
        prog="director calibration",
        description="Aggregate run-log stats and compare Sentinel vs ship-safe.",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=30,
        help="Number of days back to include (default: 30).",
    )
    parser.add_argument(
        "--until",
        type=int,
        default=0,
        help="Number of days back to end (exclusive, default: 0 = now).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of a formatted report.",
    )

    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=args.since)
    until_dt = now - timedelta(days=args.until) if args.until else None

    stats = compute_calibration(since=since_dt, until=until_dt)

    if args.json:
        import json

        print(json.dumps(stats, indent=2, default=str))
    else:
        print(format_report(stats))

    return 0


if __name__ == "__main__":
    sys.exit(main())
