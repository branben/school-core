#!/usr/bin/env python3
"""Record a structured deviation in School Core's activity timeline.

Shell-based workers can call this without editing JSON by hand:

    python scripts/record_deviation.py \
      --issue school-core-s6k \
      --agent student-coder \
      --summary "The lease gate is byte-budget based" \
      --plan-expected "Inherit the count cap" \
      --code-revealed "The count cap is disabled" \
      --decision "Hold the byte-budget lease" \
      --revisit "Revisit if count-cap admission returns" \
      --evidence crew_dispatch.py:123
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activity_log import ActivityLog, get_log  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--plan-expected", required=True)
    parser.add_argument("--code-revealed", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--revisit", required=True)
    parser.add_argument("--domain", default="")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument(
        "--log-path",
        type=Path,
        help="Write to this activity log instead of the default data/activity_log.json",
    )
    args = parser.parse_args()

    try:
        log = ActivityLog(args.log_path) if args.log_path else get_log()
        entry = log.record_deviation(
            issue=args.issue,
            agent=args.agent,
            summary=args.summary,
            plan_expected=args.plan_expected,
            code_revealed=args.code_revealed,
            decision=args.decision,
            revisit=args.revisit,
            evidence=args.evidence,
            domain=args.domain,
        )
    except (OSError, ValueError) as exc:
        print(f"record_deviation: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
