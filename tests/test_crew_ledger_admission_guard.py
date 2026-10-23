"""The committed crew ledger must not deny crew admission.

Measured regression (2026-10-23): commit cfa1a76 added a SYNTHETIC probe row
to the checked-in ``data/crew_runs.json``::

    {"crew_id": "fm-probe-fm-agent-9999", "issue_number": 9999,
     "status": "running", "fleet_worktree_id": null, ...}

That row is inert as data but fatal as state. ``issue_bridge._crew_active_count``
(issue_bridge.py:1415) sums ledger rows whose status is in
``_CREW_ACTIVE_STATUSES = {"running", "blocked"}`` (issue_bridge.py:415) and
passes the total as ``active_claims`` to ``decide_admission``
(issue_bridge.py:2142 -> src/crew/crew_admission.py:39):

    if int(active_claims) >= max(1, int(runner_slots)):
        return AdmissionDecision(False, "crew_in_flight", ...)

At the default ``runner_slots=1`` (school_scheduler.py:214), ONE active row in
the committed ledger denies every crew — permanently. The ledger is
``git add -f``'d at the end of every cycle (school-loop.yml:580) and never
reset, so the row survives every future run.

Measured before the fix::

    $ python3 -c "from issue_bridge import _crew_active_count; ..."
    _crew_active_count = 1        # -> crew_in_flight, no crew ever starts

After removing the synthetic row::

    _crew_active_count = 0

The committed ledger is a QUIESCENT snapshot (written after the cycle's loop,
not during it), so it must carry no active rows. An active row is legitimate
ONLY as a transient, in-flight record — never as checked-in state.
"""

import json
from pathlib import Path

from src.crew import crew_dispatch
from issue_bridge import _CREW_ACTIVE_STATUSES, _crew_active_count


def _committed_ledger() -> Path:
    """The ledger as checked into the repo, independent of the process cwd."""
    return crew_dispatch._main_repo_root() / "data" / "crew_runs.json"


def test_committed_ledger_carries_no_active_rows():
    """A checked-in active row permanently denies crew admission at cap=1."""
    ledger = _committed_ledger()
    assert ledger.exists(), f"committed ledger missing: {ledger}"

    rows = json.loads(ledger.read_text())
    active = [r for r in rows if r.get("status") in _CREW_ACTIVE_STATUSES]

    assert active == [], (
        f"committed data/crew_runs.json carries {len(active)} active row(s) "
        f"({[r.get('crew_id') for r in active]}). "
        f"issue_bridge._crew_active_count() == "
        f"{_crew_active_count(ledger)} >= runner_slots(1) makes "
        f"decide_admission() return crew_in_flight for EVERY issue, so no crew "
        f"can ever start. An active row is a transient in-flight record — "
        f"never checked-in state."
    )


def test_committed_ledger_active_count_is_zero():
    """The admission gate must see a quiescent ledger: zero in-flight crews."""
    ledger = _committed_ledger()
    assert _crew_active_count(ledger) == 0, (
        "a non-zero active count in the committed ledger blocks crew admission "
        "at the default runner_slots=1 (crew_admission.py:39)"
    )
