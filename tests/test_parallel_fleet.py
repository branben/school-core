"""TDD test: N-worktree fleet + correct active_claims admits N distinct crews.

Faithful to the real production path in issue_bridge.process_issues:
  - dispatch_crew writes a `running` record to crew_runs.json the moment it
    spawns (crew_dispatch.dispatch_crew, line ~827);
  - issue_bridge re-reads _crew_active_count(CREW_RUNS_FILE) PER ISSUE and
    passes it as active_claims (issue_bridge.py:1141,1154).
That durable registry (not just the office's synchronous lease) is what keeps
admission honest under parallelism. This test mirrors that: the fake dispatch
writes a running record, and we feed live active_claims per dispatch.

The real blockers for N-parallel students are CONFIG/INFRA, not code bugs:
  (a) fleet.json must be provisioned with N worktrees (default is 1);
  (b) CREW_RUNNER_SLOTS + crew_max_per_cycle must be >= N (default cap 1);
  (c) the cycle budget must cover cap*timeout+reserve or admission denies
      (this is correct budget-aware behavior, proven by the deny branch);
  (d) fm-spawn/Orca must actually create N concurrent worktrees (infra);
  (e) the OmniRoute gateway (localhost:20128) must survive N concurrent crews.
"""
from pathlib import Path

from crew_dispatch import CREW_RUNS_FILE as _CRF, CrewResult
from school_scheduler import DispatchOffice


def _make_result(i):
    return CrewResult(
        crew_id=f"fm-c-{i}", status="done", report_path=None,
        fallback_reason=None, teardown_ok=True,
    )


def _fake_dispatch_factory(monkeypatch, crew_runs_file):
    """Mimic dispatch_crew: return a result AND record a `running` entry in the
    durable registry (what real dispatch_crew does at spawn time), so that
    _crew_active_count reflects in-flight crews like production."""
    seq = []

    def fake(*, issue_number, fleet_worktree_id=None, **kw):
        # Append a running record so subsequent active_claims reads see it.
        # Record the fleet slot the office leased (what real dispatch_crew now
        # receives via fleet_worktree_id), so assign_worktree keeps the slot
        # occupied for the crew's async execution (N6.2).
        import json
        runs = []
        if crew_runs_file.exists():
            try:
                runs = json.loads(crew_runs_file.read_text())
            except Exception:
                runs = []
        runs.append({"crew_id": f"fm-c-{issue_number}", "issue_number": issue_number,
                     "status": "running", "fleet_worktree_id": fleet_worktree_id})
        crew_runs_file.write_text(json.dumps(runs))
        seq.append(issue_number)
        return _make_result(issue_number)

    monkeypatch.setattr("school_scheduler.dispatch_crew", fake)
    return seq


def test_four_worktree_fleet_admits_four_distinct_crews(monkeypatch, tmp_path):
    fleet = tmp_path / "fleet.json"
    fleet.write_text(
        '{"daemons": {"local": {"endpoint": "local", '
        '"worktrees": ["wt-1", "wt-2", "wt-3", "wt-4"], "capacity": 4}}}'
    )
    leases = tmp_path / "leases.json"
    crew_runs = tmp_path / "crew_runs.json"
    _fake_dispatch_factory(monkeypatch, crew_runs)

    # Replicate issue_bridge's per-issue pattern: re-read active claims before
    # each dispatch and pass them in.
    from issue_bridge import _crew_active_count

    office = DispatchOffice(fleet_file=fleet, lease_file=leases, crew_runs_file=crew_runs)
    outcomes = []
    for i in range(4):
        live_active = _crew_active_count(crew_runs)
        out = office.dispatch(
            issue_number=i, task_text="t", project_dir=Path("/tmp"),
            cycle_session_id="c1", capability=None, domain="python-coding",
            difficulty="easy", configured_cap=4, runner_slots=4,
            active_claims=live_active,
            remaining_seconds=4000, crew_timeout_seconds=900,
        )
        outcomes.append(out)

    denied = [o for o in outcomes if o.skip_reason is not None]
    assert not denied, f"expected 4 admissions, got denials: {[o.skip_reason for o in denied]}"

    assigned = [o.worktree_id for o in outcomes]
    assert len(assigned) == len(set(assigned)), f"worktree collision: {assigned}"
    assert set(assigned) == {"wt-1", "wt-2", "wt-3", "wt-4"}
