"""The board-state commit must survive a cancelled job.

WHY THIS EXISTS
---------------
Live run 32330426471 admitted a crew for the FIRST time — issue #342 ran ~16
minutes of real crew work before hitting CREW_TIMEOUT_SECONDS (900s). Then the
30-minute job timeout cancelled the run.

``data/crew_runs.json`` has NO record for #342. Its newest entries are Aug-13
fixtures (issue numbers 50, 57, 58, 59, 60, 700001). So the single most
informative artifact from the first successful admission in the project's history
was destroyed.

``dispatch_crew`` DOES write a durable ``running`` record at spawn
(crew_dispatch.py:847), so the record existed on the runner's disk. The loss is
in the workflow: the commit step

    - name: Sanitize + commit board state (durable, PII-free)

has no ``if:`` condition, so it inherits the default ``success()``. A cancelled
job skips it entirely and every ``data/*`` mutation from that cycle — crew runs,
processed issues, scores, retries — is discarded with the runner's workspace.

This is the same durability class as the bug fixed in 83deae8 (success path
checkpointed only after the loop), but one layer out: there the LOSS was in
Python, here it is in the workflow's step conditions. Fixing the inner layer did
not fix the outer one.

WHY ``if: always()`` AND NOT ``if: success() || cancelled()``
------------------------------------------------------------
State written before a FAILURE is just as real as state written before a
cancellation. A crew that spawned, ran, and then crashed the bridge still
produced a genuine ``running`` record and genuine score updates. Persisting them
is what lets the next cycle see what happened instead of re-dispatching blind.

These tests read the workflow YAML directly. They are cheap, they need no runner,
and they fail loudly if someone removes the condition later — which is exactly
how this regressed into existence.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).parent.parent / ".github/workflows/school-loop.yml"


def _execute_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["execute"]["steps"]


def _step(name_fragment: str) -> dict:
    for step in _execute_steps():
        if name_fragment.lower() in (step.get("name") or "").lower():
            return step
    raise AssertionError(
        f"no step matching {name_fragment!r}; steps are "
        f"{[s.get('name') for s in _execute_steps()]}"
    )


class TestBoardStateSurvivesCancellation:
    def test_commit_step_runs_even_when_cancelled(self):
        """REGRESSION: #342's crew record was lost to a cancelled job.

        Without an explicit condition the step defaults to success(), so the
        30-minute timeout discarded every data/* mutation from the cycle.
        """
        step = _step("commit board state")
        cond = str(step.get("if", "")).lower()
        assert cond, (
            "the board-state commit step has no `if:` condition, so it defaults "
            "to success() and a cancelled job loses all data/* state for that "
            "cycle — including the crew_runs.json record for the first crew "
            "admission that ever happened (#342)"
        )
        assert "always()" in cond or ("cancelled()" in cond and "success()" in cond), (
            f"condition {cond!r} does not cover cancellation"
        )

    def test_commit_step_still_commits_crew_runs(self):
        """Guard: crew_runs.json must remain in the committed set.

        It is the only durable evidence of what the crew path actually did.
        """
        step = _step("commit board state")
        assert "crew_runs.json" in str(step.get("run", "")), (
            "crew_runs.json dropped from the git add list"
        )

    def test_bridge_loop_still_precedes_the_commit(self):
        """Ordering guard: committing before the loop would persist stale state."""
        names = [(s.get("name") or "") for s in _execute_steps()]
        bridge = next(i for i, n in enumerate(names) if "bridge loop" in n.lower())
        commit = next(i for i, n in enumerate(names) if "commit board state" in n.lower())
        assert bridge < commit, "bridge loop must run before the state commit"

    def test_gateway_preflight_is_not_weakened_to_advisory(self):
        """Guard on a DIFFERENT polarity, to keep the two straight.

        The gateway preflight must keep failing hard (it converts a 31-minute
        grind into an immediate failure). Only the residue audit is advisory.
        Making this one always() too would resurrect the silent-grind failure
        mode that 75ac20c fixed.
        """
        step = _step("Gateway preflight")
        cond = str(step.get("if", "")).lower()
        assert "always()" not in cond, (
            "gateway preflight must NOT be always() — a dead gateway has to stop "
            "the cycle, not be recorded and pressed past"
        )
