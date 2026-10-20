#!/usr/bin/env python3
"""principal_doubt.py — Doubt-Driven Development (DDD) cycle for Principal routing.

Rank 3 of the Layer B integration plan.

Before the Principal commits a routing decision (which gate, which model, which
lens, which role), it runs a short adversarial doubt cycle:

    CLAIM      → "I am routing this task to <role> using gate <difficulty> via <model>"
    EXTRACT    → the task description + gate criterion + model selection
    DOUBT      → an adversarial pass asks "what is wrong with this routing decision?"
    RECONCILE  → if doubt finds a valid issue, re-route (different gate / role / lens)
    STOP       → after 1 cycle (routing decisions are low-complexity; 3 max for edge cases)

The cycle is OFFLINE-TESTABLE: the adversarial "doubt" step is a pluggable
``doubt_fn``. In tests and by default it uses a deterministic local analyzer
(``_offline_doubt``) that returns no findings, so the suite never needs an LLM
or the OmniRoute gateway. A live deployment can pass a real OmniRoute-backed
``doubt_fn`` to get genuine adversarial review.

The result is a ``doubt_log`` dict:

    {
        "claim": str,
        "extract": dict,
        "findings": list[str],
        "reconciled": bool,        # True if doubt changed the routing decision
        "override_reason": str|None,
        "cycles": int,
    }

When doubt is disabled, callers skip the cycle entirely and no ``doubt_log`` is
attached (backward compat — see conductor._principal_dispatch).
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Routing tiers, low → high. Used by the reconcile step to "down-shift" an
# over-aggressive gate when doubt flags it as too hard for the role's readiness.
GATE_TIERS = ["easy", "medium", "hard", "diploma"]


def _offline_doubt(claim: str, extract: dict) -> List[str]:
    """Deterministic local doubt analyzer (no network, no LLM).

    Returns an empty finding list by default. A live deployment substitutes a
    real adversarial model call here. Kept dependency-free so the test suite
    runs offline.
    """
    return []


def _down_shift_gate(gate: str) -> str:
    """Return the next-easier gate tier, or the same gate if already easiest."""
    try:
        idx = GATE_TIERS.index(gate)
    except ValueError:
        return gate
    if idx <= 0:
        return gate
    return GATE_TIERS[idx - 1]


def run_doubt_cycle(
    claim: str,
    extract: dict,
    doubt_fn: Optional[Callable[[str, dict], List[str]]] = None,
    max_cycles: int = 1,
    override_reason: Optional[str] = None,
) -> dict:
    """Run the DDD doubt cycle on a principal routing decision.

    Args:
        claim: The routing CLAIM (e.g. "routing task to coder via gate hard").
        extract: The EXTRACTed routing context (task, gate, model, role, lens).
        doubt_fn: Pluggable adversarial analyzer. Signature
            ``fn(claim: str, extract: dict) -> list[str]``. Defaults to the
            offline analyzer (no findings). Pass a live OmniRoute-backed fn in
            production.
        max_cycles: Stop after this many cycles (1 for normal routing; 3 for
            edge cases). Routing decisions are low-complexity.
        override_reason: If the human explicitly wants to skip doubt, pass a
            reason; the cycle records it and returns no findings.

    Returns:
        doubt_log dict (see module docstring).
    """
    if override_reason is not None:
        # Human override: log the skip, do not run the doubt pass.
        return {
            "claim": claim,
            "extract": extract,
            "findings": [],
            "reconciled": False,
            "override_reason": override_reason,
            "cycles": 0,
        }

    analyzer = doubt_fn or _offline_doubt

    findings: List[str] = []
    reconciled = False
    cycles = 0

    while cycles < max_cycles:
        cycles += 1
        current_findings = analyzer(claim, extract)
        if not current_findings:
            break
        findings.extend(current_findings)
        # RECONCILE: doubt found an issue. Down-shift an over-aggressive gate
        # (the most common routing error) and re-claim. A richer deployment
        # could swap role/lens/model here too.
        gate = extract.get("gate")
        if gate and gate in GATE_TIERS:
            shifted = _down_shift_gate(gate)
            if shifted != gate:
                extract = dict(extract)
                extract["gate"] = shifted
                reconciled = True
                # Re-build the claim to reflect the reconciliation.
                claim = claim.replace(f"gate {gate}", f"gate {shifted}")
        # One cycle for routing decisions; loop only continues if max_cycles > 1
        # and the analyzer keeps returning findings after reconciliation.

    return {
        "claim": claim,
        "extract": extract,
        "findings": findings,
        "reconciled": reconciled,
        "override_reason": None,
        "cycles": cycles,
    }
