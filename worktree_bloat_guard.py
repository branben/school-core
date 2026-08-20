"""N9 — worktree/terminal bloat boundary.

WHY THIS EXISTS
---------------
Observed 2026-08-19 on the live machine: Orca had accumulated **51 terminals**
(48 of them inside a single worktree) and **22 worktrees**, 11 of which were
suffix-sprayed teacher clones::

    teacher-coo-branben__sound-royale-ny{,-2,-3,-4,-5}
    teacher-cto-branben__sound-royale-ny{,-2,-3,-4,-5,-6}

All 11 were clean (``dirty=0 ahead_of_main=0``) — pure residue. Critically none
were flagged ``orphaned``, so nothing in Orca would ever reap them; the pile only
grows.

``orca_executor.create_worktree_persistent`` already documents and implements the
cure (rediscover-by-prefix, prune stale admin entries, never mint ``-2``). The
hazard is that the NON-persistent ``create_worktree`` is one keystroke away and
silently auto-suffixes when the name is taken — and ``teacher.py``'s own module
docstring demonstrates exactly that wrong call::

    # teacher.py:25  (docstring "Usage" example)
    path = mgr.create_worktree("teacher-cto")

Anyone following that example sprays. Likewise ``conductor.py:1848`` records that
"Orca's ``create_terminal`` never dedupes by title", so repeated boots stack
terminals without bound.

This module is the ERROR BOUNDARY: a cheap, callable audit that fails loudly when
residue crosses a threshold, so bloat is caught by a check rather than noticed by
a human three hours later.

DESIGN
------
* Read-only by default. It reports; it does not delete. Destructive cleanup of a
  developer's worktrees needs a human decision, and an auto-reaper that guesses
  wrong destroys work.
* Counts PERSISTENT-ROLE SPRAY specifically (``<role>-<n>`` siblings), not raw
  worktree count. A dozen legitimate project worktrees are not a defect; two
  ``teacher-cto`` clones are.
* Never raises on inspection failure — a broken audit must not break a pipeline.
  It reports ``ok=None`` (unknown) so a caller can tell "clean" from "couldn't
  tell", the same CONFIRMED/UNPROVEN discipline the rest of the school uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# A suffixed sibling of a persistent role, e.g. `teacher-cto-foo-2`.
# The suffix is a trailing `-<digits>` on an otherwise identical stem.
_SUFFIX_RE = re.compile(r"^(?P<stem>.+?)-(?P<n>\d+)$")

# Persistent roles that must exist exactly ONCE each (see orca_executor
# .create_worktree_persistent's Lifecycle invariant).
PERSISTENT_ROLE_PREFIXES = ("teacher-cto", "teacher-coo", "principal")

# Thresholds. Deliberately generous: this is a boundary against runaway growth,
# not a style rule. 48 terminals in one worktree was the observed failure.
MAX_TERMINALS_PER_WORKTREE = 8
MAX_TERMINALS_TOTAL = 40
MAX_SPRAY_PER_ROLE = 1  # one worktree per persistent role; a `-2` is the defect


@dataclass
class BloatReport:
    """Result of a residue audit.

    ``ok`` is tri-state on purpose: True (within bounds), False (breach), or
    None (could not determine). Collapsing None into True is how a broken audit
    silently becomes a passing one.
    """

    ok: Optional[bool]
    findings: list[str] = field(default_factory=list)
    terminal_count: int = 0
    worktree_count: int = 0
    spray: dict[str, list[str]] = field(default_factory=dict)
    detail: str = ""

    def as_text(self) -> str:
        if self.ok is None:
            return f"worktree/terminal audit UNKNOWN: {self.detail}"
        if self.ok:
            return (
                f"worktree/terminal audit OK "
                f"({self.terminal_count} terminal(s), {self.worktree_count} worktree(s))"
            )
        lines = [
            f"worktree/terminal BLOAT detected "
            f"({self.terminal_count} terminal(s), {self.worktree_count} worktree(s)):"
        ]
        lines.extend(f"  - {f}" for f in self.findings)
        return "\n".join(lines)


def find_spray(worktree_names: list[str]) -> dict[str, list[str]]:
    """Group suffixed siblings of persistent roles by their stem.

    Only names whose stem starts with a persistent-role prefix are considered:
    ephemeral crew worktrees (``fm-fm-loop-...-342``) legitimately carry a
    trailing number and must never be reported as spray.
    """
    groups: dict[str, list[str]] = {}
    for name in worktree_names:
        m = _SUFFIX_RE.match(name)
        if not m:
            continue
        stem = m.group("stem")
        if not any(stem.startswith(p) for p in PERSISTENT_ROLE_PREFIXES):
            continue
        groups.setdefault(stem, []).append(name)
    return {k: sorted(v) for k, v in groups.items() if v}


def audit_residue(
    list_terminals: Callable[[], list[dict]],
    list_worktrees: Callable[[], list[dict]],
    max_terminals_per_worktree: int = MAX_TERMINALS_PER_WORKTREE,
    max_terminals_total: int = MAX_TERMINALS_TOTAL,
    max_spray_per_role: int = MAX_SPRAY_PER_ROLE,
) -> BloatReport:
    """Audit Orca residue. Read-only; never raises.

    The two callables are injected so this is testable without a live Orca and
    reusable from any caller that can already enumerate them.
    """
    try:
        terminals = list(list_terminals() or [])
        worktrees = list(list_worktrees() or [])
    except Exception as e:
        return BloatReport(ok=None, detail=f"{type(e).__name__}: {e}")

    findings: list[str] = []

    per_wt: dict[str, int] = {}
    for t in terminals:
        key = (t.get("worktreePath") or t.get("worktreeId") or "?").split("/")[-1]
        per_wt[key] = per_wt.get(key, 0) + 1
    for wt, n in sorted(per_wt.items(), key=lambda kv: -kv[1]):
        if n > max_terminals_per_worktree:
            findings.append(
                f"{n} terminals in one worktree ({wt}) — limit {max_terminals_per_worktree}; "
                "create_terminal does not dedupe by title, so repeated boots stack"
            )

    if len(terminals) > max_terminals_total:
        findings.append(
            f"{len(terminals)} terminals total — limit {max_terminals_total}"
        )

    names = [(w.get("path") or "?").split("/")[-1] for w in worktrees]
    spray = find_spray(names)
    for stem, sibs in sorted(spray.items()):
        if len(sibs) > max_spray_per_role:
            findings.append(
                f"persistent role '{stem}' has {len(sibs)} suffixed clone(s) "
                f"({', '.join(sibs)}) — use create_worktree_persistent(), which "
                "rediscovers by prefix instead of minting -2"
            )

    return BloatReport(
        ok=not findings,
        findings=findings,
        terminal_count=len(terminals),
        worktree_count=len(worktrees),
        spray=spray,
    )


def _orca_json(args: list[str]) -> dict:
    """Run an `orca <args> --json` command and return its ``result`` object."""
    import json
    import subprocess

    out = subprocess.run(
        ["orca", *args, "--json"],
        capture_output=True,
        text=True,
        timeout=90,
    ).stdout
    return json.loads(out).get("result", {}) or {}


def audit_live() -> BloatReport:
    """Audit the running Orca daemon. Never raises; UNKNOWN when unreachable."""
    return audit_residue(
        list_terminals=lambda: _orca_json(["terminal", "list"]).get("terminals") or [],
        list_worktrees=lambda: _orca_json(["worktree", "list"]).get("worktrees") or [],
    )


def main() -> int:
    """CLI entry point for the CI preflight.

    ADVISORY BY DESIGN — always exits 0. Residue is a slow leak, not a reason to
    drop a cycle: killing a run because a stale worktree exists would trade a
    real problem (no issues processed) for a cosmetic one. Emits a GitHub
    ``::warning::`` so the breach is visible in the run summary without turning
    the board red.

    This is the opposite call from gateway_preflight.py, which exits 1 — a dead
    gateway means the cycle CANNOT do useful work, so failing fast is strictly
    better than a 30-minute grind. Bloat does not block work; it accumulates.
    """
    report = audit_live()

    if report.ok is None:
        # UNKNOWN, not clean. Say so rather than implying a pass.
        print(f"::warning::worktree/terminal audit could not run: {report.detail}")
        return 0

    if report.ok:
        print(report.as_text())
        return 0

    print(
        f"::warning::worktree/terminal bloat: {report.terminal_count} terminal(s), "
        f"{report.worktree_count} worktree(s)"
    )
    for finding in report.findings:
        print(f"::warning::  {finding}")
    print(report.as_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
