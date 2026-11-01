#!/usr/bin/env python3
"""Failure-shape lookup — matches a symptom to a known false-absence shape.

Usage:
  python scripts/failure_shape_lookup.py "empty grep on X means X is absent"
  python scripts/failure_shape_lookup.py --interactive

Exit 0 if a shape matched, 1 if no match. Prints the matched shape(s) to stdout.
"""
from __future__ import annotations

import re
import sys

# The 15 shapes from anchor: evidence-discipline-reference
SHAPES = [
    (1,  "An empty grep is not proof of absence",     "Your pattern did not match",                        "Widen the pattern or read the file directly; grep answers 'does this string appear', not 'is this thing absent'."),
    (2,  "An empty ledger is not proof of no work",   "The recorder was not recording",                    "Check the producer's own output directory before concluding from the consumer's records."),
    (3,  "A cat-file miss in ONE clone is not absence","The object is not in that clone",                  "Check every clone that could hold it before declaring an object gone."),
    (4,  "A log that records one branch is not evidence about the others", "What that branch did", "A pipeline can succeed on path A while path B's log shows only failures."),
    (5,  "Registration is not supervision",           "The process is known, not that it will restart",   "Verify the restart mechanism; a daemon alive now says nothing about its resilience."),
    (6,  "Do not repair a number by re-guessing it",  "Nothing — a guess replacing a guess",              "Run git rev-list --count or equivalent; count, never estimate."),
    (7,  "Same-shaped errors are not the same bug",   "The symptom is shared",                            "Count causes, not symptoms. Verify each before assigning a shared root."),
    (8,  "A stale fact reported honestly is not an error", "The state at the time it was read",           "State assertions need a timestamp or a re-check."),
    (9,  "A false premise handed to a subagent gets elaborated, not caught", "See Orchestrator section", "Invite refutation of the PREMISES, not just the conclusion. Mark inputs as VERIFIED or ASSUMED."),
    (10, "A status file's last line is not its state","The last event, not the terminal state",          "Scan for the last terminal verb (done/failed/blocked), not the last line."),
    (11, "A label derived from a string parse is not a fact about the system", "What the parser saw", "Find the code that produces the label before treating it as evidence."),
    (12, "A commit that coincides with a metric change may be a no-op", "Nothing — the code path may never execute", "Verify the code path actually executes before attributing causality."),
    (13, "A fix on a side branch is not landed",      "The branch the CI actually runs",                 "Close reasons must name the ref the CI actually runs. Verify with git branch -r --contains <sha>."),
    (14, "A probe payload that cannot exhibit the bug proves nothing", "Your payload was not vulnerable",          "Test with the real payload shape, not a sentinel."),
    (15, "A census taken downstream of a filter cannot detect what the filter removes", "The recognized output only", "Measure upstream of the filter, or apply the filter to raw text."),
]

# Keyword -> shape number mapping for fast lookup
_KEYWORDS: dict[str, list[int]] = {}
for num, name, proof, fix in SHAPES:
    words = re.findall(r"[A-Za-z]+", name.lower())
    for w in words:
        _KEYWORDS.setdefault(w, []).append(num)
    # Also index the proof line
    words2 = re.findall(r"[A-Za-z]+", proof.lower())
    for w in words2:
        _KEYWORDS.setdefault(w, []).append(num)


def lookup(symptom: str) -> list[tuple[int, str, str]]:
    words = re.findall(r"[A-Za-z]+", symptom.lower())
    scores: dict[int, int] = {}
    for w in words:
        for num in _KEYWORDS.get(w, []):
            scores[num] = scores.get(num, 0) + 1
    if not scores:
        return []
    max_score = max(scores.values())
    best = [num for num, s in scores.items() if s == max_score]
    result = []
    for num in sorted(best):
        name, proof, fix = next((n, p, f) for n2, n, p, f in SHAPES if n2 == num)
        result.append((num, name, fix))
    return result


def print_shape(num: int, name: str, fix: str) -> None:
    print(f"\n=== Shape {num}: {name} ===")
    print(f"  Fix: {fix}")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0

    if sys.argv[1] == "--interactive":
        print("Failure-shape lookup (Ctrl-C to exit)")
        try:
            while True:
                symptom = input("symptom> ").strip()
                if not symptom:
                    continue
                matches = lookup(symptom)
                if not matches:
                    print("  No shape matched.")
                else:
                    for num, name, fix in matches:
                        print_shape(num, name, fix)
        except KeyboardInterrupt:
            print()
        return 0

    symptom = " ".join(sys.argv[1:])
    matches = lookup(symptom)
    if not matches:
        print(f"No shape matched for: {symptom}")
        return 1
    for num, name, fix in matches:
        print_shape(num, name, fix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
