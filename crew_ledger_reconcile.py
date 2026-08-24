"""N10 — producer/consumer reconciliation boundary (bead school-core-1u9 / B7).

WHY THIS EXISTS
---------------
Measured on this machine 2026-08-20:

    producer  ~/.hermes/school-core-fm-config/state/*.status   108 files, 54 `done:`
    consumer  data/crew_runs.json                                6 records

48 COMPLETED crews were never recorded anywhere. Because the consumer's ledger
was near-empty, it was asserted for an entire session — to the user AND to three
subagent dispatches — that "the crew path has never completed a real issue."
That claim was false. The crews had been working for days across 6 real issues;
the LEDGER was broken, not the crew.

The loss mechanism is already fixed: the workflow's board-state commit step had
no ``if:`` condition, so it inherited ``success()`` and a cancelled job discarded
every ``data/`` mutation (fixed in 5cc4ae0, guarded by
tests/test_board_state_durability.py). This module is the BOUNDARY, so the same
class of silent loss cannot regrow undetected — by that mechanism or a future one
nobody has thought of yet.

THE INVARIANT — and why it is not a ratio
-----------------------------------------
    every status file with a TERMINAL verb (done/failed) MUST have a ledger record

A producer/consumer *ratio* was the first proposal and it is the wrong signal: it
has no true value, so any threshold is arbitrary and gets tuned upward until the
check is silent. It also false-alarms on crews that are legitimately still
running (a `working:` status file has no terminal record yet, correctly).

The terminal-verb invariant is provable, needs no calibration, and would have
fired at 54-vs-6 on day one. Credit: student-scribe, in design review.

DESIGN RULES, each one earned
-----------------------------
1. Report the SET DIFFERENCE, not a count. "48 unreconciled" is a number; the
   list of 48 crew ids is actionable.
2. FAIL LOUD on an empty producer set. Reporting "reconciled" from two empty
   sets is the original blindness reimplemented as a guard — it would look green
   precisely when it had learned nothing.
3. Tri-state ``ok``: True / False / None. None means "could not determine", and
   must never read as pass (the UNKNOWN-as-verdict collapse fixed three times in
   one night: ca400aa, 066b383, 813a838).
4. READ ONLY. Report; never delete. A reaper that guesses wrong destroys the
   evidence this exists to protect.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The producer: FirstMate writes one status file per crew here. Resolved the same
# way crew_dispatch does (FM_HOME/state, overridable) so the audit and the writer
# can never disagree about where the truth lives.
_FM_HOME = Path(
    os.environ.get("FM_HOME", str(Path.home() / ".hermes" / "school-core-fm-config"))
).expanduser()
STATE_DIR = Path(os.environ.get("FM_STATE", str(_FM_HOME / "state"))).expanduser()

# The consumer: the bridge's durable registry.
CREW_RUNS_FILE = Path(
    os.environ.get("CREW_RUNS_FILE", str(Path(__file__).parent / "data/crew_runs.json"))
).expanduser()

TERMINAL_VERBS = ("done", "failed")
_TERMINAL_RE = re.compile(r"^(done|failed):", re.MULTILINE)


@dataclass
class ReconcileReport:
    """Tri-state audit result. ``ok is None`` means "could not determine"."""

    ok: Optional[bool]
    findings: list[str] = field(default_factory=list)
    terminal_count: int = 0
    recorded_count: int = 0
    unreconciled: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok is None:
            return "reconciliation audit could not run: " + "; ".join(self.findings)
        if self.ok:
            return (
                f"crew ledger reconciled ({self.terminal_count} terminal status "
                f"file(s), all recorded)"
            )
        return (
            f"crew ledger MISSING {len(self.unreconciled)} of {self.terminal_count} "
            f"terminal crew run(s)"
        )


def _terminal_crew_ids(state_dir: Path) -> Optional[set[str]]:
    """Crew ids whose status file reached a terminal verb. None if unreadable."""
    if not state_dir.is_dir():
        return None
    ids: set[str] = set()
    for path in state_dir.glob("*.status"):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            # One unreadable file is not grounds to fail the whole audit, but it
            # must not be silently counted as non-terminal either.
            continue
        if _TERMINAL_RE.search(text):
            ids.add(path.name[: -len(".status")])
    return ids


def _recorded_crew_ids(runs_file: Path) -> Optional[set[str]]:
    """Crew ids present in the ledger. None if the ledger cannot be read."""
    try:
        raw = json.loads(runs_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rows = raw if isinstance(raw, list) else raw.get("runs", [])
    return {
        str(r["crew_id"])
        for r in rows
        if isinstance(r, dict) and r.get("crew_id")
    }


def reconcile(
    state_dir: Path = STATE_DIR,
    runs_file: Path = CREW_RUNS_FILE,
) -> ReconcileReport:
    """Assert every terminal crew status file has a ledger record.

    Returns a tri-state report and never raises: an audit that crashes the
    caller is worse than one that reports it could not look.
    """
    findings: list[str] = []

    terminal = _terminal_crew_ids(state_dir)
    if terminal is None:
        findings.append(
            f"producer state dir missing or unreadable: {state_dir} — cannot "
            "conclude anything about crew completion from the ledger alone"
        )
        return ReconcileReport(ok=None, findings=findings)

    # An empty producer set must NEVER reconcile. Two empty sets agreeing is the
    # exact blindness this guard exists to prevent: it would report green while
    # having learned nothing at all.
    if not terminal:
        findings.append(
            f"no terminal crew status files found under {state_dir} — refusing to "
            "report 'reconciled' from an empty producer set (0 vs 0 is not "
            "agreement, it is a failure to observe)"
        )
        return ReconcileReport(ok=None, findings=findings)

    recorded = _recorded_crew_ids(runs_file)
    if recorded is None:
        findings.append(
            f"ledger missing or unreadable: {runs_file} — {len(terminal)} terminal "
            "crew run(s) exist on disk with nothing to reconcile against"
        )
        return ReconcileReport(
            ok=None, findings=findings, terminal_count=len(terminal)
        )

    missing = sorted(terminal - recorded)
    if missing:
        findings.append(
            f"{len(missing)} terminal crew run(s) have NO ledger record — the "
            "bridge ran them and failed to record them. Do not read the ledger "
            "as evidence of what the crew did."
        )
        # The list, not just the count: a count is a number, the ids are work.
        for crew_id in missing[:20]:
            findings.append(f"  unreconciled: {crew_id}")
        if len(missing) > 20:
            findings.append(f"  … and {len(missing) - 20} more")

    return ReconcileReport(
        ok=not missing,
        findings=findings,
        terminal_count=len(terminal),
        recorded_count=len(recorded),
        unreconciled=missing,
    )


def main() -> int:
    """CI entry point. ADVISORY: always exits 0.

    Deliberately unlike gateway_preflight.py, which exits 1. A dead gateway means
    the cycle cannot do useful work, so failing fast is the whole point there. An
    unreconciled ledger is a bookkeeping loss: real, worth surfacing, but killing
    the cycle over it would trade a reporting problem for a work-stoppage.
    """
    report = reconcile(STATE_DIR, CREW_RUNS_FILE)
    if report.ok:
        print(report.summary())
        return 0
    prefix = "::warning::"
    print(prefix + report.summary())
    for finding in report.findings:
        print(prefix + "  " + finding)
    if report.ok is None:
        print(
            prefix
            + "  UNKNOWN is not a pass — this audit did not verify anything."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
