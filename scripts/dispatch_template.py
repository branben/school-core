"""Dispatch template — forces premise verification before any subagent dispatch.

Usage:
  python scripts/dispatch_template.py --premises 2 --conclusion 1

Returns exit 0 only if every premise is marked VERIFIED or explicitly
labeled UNPROVEN with a named check that would settle it. A premise marked
ASSUMED without a named verification step fails the gate.

This implements the rule from debug-school-core shape 9:
"A false premise handed to a subagent does not get caught by the subagent;
it gets elaborated."
"""
from __future__ import annotations

import argparse
import re
import sys


class Premise:
    def __init__(self, text: str, status: str, verification: str = ""):
        self.text = text
        self.status = status.upper()  # VERIFIED | ASSUMED | UNPROVEN
        self.verification = verification

    def is_valid(self) -> bool:
        if self.status == "VERIFIED":
            return True
        if self.status == "UNPROVEN" and self.verification.strip():
            return True
        return False

    def __repr__(self) -> str:
        return f"Premise({self.text!r}, {self.status}, {self.verification!r})"


def parse_premises(text: str) -> list[Premise]:
    """Parse a premise block into Premise objects.

    Expected format (one per block):
      - <status>: <text> [verification: <how to verify>]
    Status must be VERIFIED, ASSUMED, or UNPROVEN.
    Continuation lines (indented) append to the previous field.
    """
    premises = []
    current_status = None
    current_text = []
    current_verification = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        # Start of a new premise
        m = re.match(r"-\s*(VERIFIED|ASSUMED|UNPROVEN)\s*:\s*(.+)", stripped, re.IGNORECASE)
        if m:
            # Save the previous premise if any
            if current_status is not None:
                verification = " ".join(current_verification).strip()
                premises.append(Premise(
                    " ".join(current_text).strip(),
                    current_status,
                    verification,
                ))
            current_status = m.group(1).upper()
            rest = m.group(2).strip()
            # Check if the rest contains 'verification:'
            vm = re.match(r"(.+?)\s*verification:\s*(.+)", rest, re.IGNORECASE)
            if vm:
                current_text = [vm.group(1).strip()]
                current_verification = [vm.group(2).strip()]
            else:
                current_text = [rest]
                current_verification = []
        elif current_status is not None and (stripped.startswith("verification:") or current_verification):
            # Continuation of verification
            if stripped.startswith("verification:"):
                current_verification = [stripped.split(":", 1)[1].strip()]
            else:
                current_verification.append(stripped)
        elif current_status is not None:
            # Continuation of text
            current_text.append(stripped)

    # Save the last premise
    if current_status is not None:
        verification = " ".join(current_verification).strip()
        premises.append(Premise(
            " ".join(current_text).strip(),
            current_status,
            verification,
        ))
    return premises


def main() -> int:
    ap = argparse.ArgumentParser(description="Dispatch template gate: validate premises before dispatching")
    ap.add_argument("--premises", type=int, required=True, help="Minimum number of premises required")
    ap.add_argument("--conclusion", type=int, required=True, help="Minimum number of conclusions to check")
    ap.add_argument("--premise-text", default="", help="Premise block text (or read from stdin)")
    args = ap.parse_args()

    text = args.premise_text
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()

    if not text:
        print("ERROR: no premise text provided (use --premise-text or stdin)", file=sys.stderr)
        return 2

    premises = parse_premises(text)
    if len(premises) < args.premises:
        print(f"FAIL: {len(premises)} premises provided, {args.premises} required", file=sys.stderr)
        return 1

    invalid = [p for p in premises if not p.is_valid()]
    if invalid:
        print("FAIL: the following premises are not adequately verified:", file=sys.stderr)
        for p in invalid:
            print(f"  [{p.status}] {p.text}", file=sys.stderr)
            if p.status == "ASSUMED":
                print(f"    -> add a 'verification: <command>' step or mark VERIFIED", file=sys.stderr)
        return 1

    print(f"PASS: {len(premises)} premises verified, {args.conclusion} conclusion(s) to check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
