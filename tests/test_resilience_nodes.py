"""Worst-day-ever resilience guard primitives + wired-node tests.

Covers nodes N1.1, N1.2, N1.3, N2.2, N3.1, N3.2, N4.1, N4.3, N6.1, N6.2,
N6.3 (config), N7.1, N7.2, N7.3, N8.2, plus the wired bookbag atomicity and
the director force_agent allowlist. The CRITICAL nodes N5.1/N5.2/N5.3 have
their own test file (test_worst_day_critical_nodes.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from resilience import (
    sanitize_input_text,
    safe_float,
    grading_dedup_key,
    grader_score_key,
    is_stale_monotonic,
    verify_worktree_isolated,
    force_agent_allowed,
    bounded_grader_pool_size,
    worktree_lease,
    RetryBudget,
    LabelWriteQueue,
    BackpressureSemaphore,
    assert_resources_clean,
)


# ── N1.1: Input Boundary sanitization ───────────────────────────────────────

def test_sanitize_strips_rtl_and_null_and_caps():
    evil = "solve\u202Ethis\u202D ; rm -rf /\u0000" + "x" * 9000
    out = sanitize_input_text(evil, max_len=100)
    assert "\u202e" not in out and "\u202d" not in out  # RTL removed
    assert "\x00" not in out  # null removed
    assert len(out) <= 100  # capped


def test_sanitize_handles_none_and_empty():
    assert sanitize_input_text(None) == ""
    assert sanitize_input_text("") == ""


# ── N1.2: shell metachar in title must NOT execute (quoting contract) ───────

def test_spawn_seam_is_list_args_no_shell():
    """dispatch_crew shells out via _spawn -> _run(list(args)), never shell=True.
    A title like '; rm -rf /' must reach fm-spawn as a literal argv element
    inside the brief, never as an executed command. We assert the spawn seam
    builds a LIST (not a shell string) and never sets shell=True — the
    contract that prevents injection. (End-to-end dispatch_crew is not run here
    to avoid the real fm-spawn poll loop; the shell-safety guarantee is entirely
    at this seam.)"""
    import crew_dispatch

    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        captured["shell"] = kwargs.get("shell", False)

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    with patch.object(crew_dispatch, "_run", fake_run):
        # _spawn builds the fm-spawn argv; with capability=None the payload is
        # skipped but the argv construction still runs (pure, no network).
        crew_dispatch._spawn("test-crew", Path("/tmp"), capability=None)

    assert captured.get("shell") is not True, "spawn seam must never use shell=True"
    assert isinstance(captured.get("args"), list), "spawn argv must be a list (not a shell string)"


# ── N1.3: numeric guard ──────────────────────────────────────────────────────

def test_safe_float_rejects_nan_inf():
    assert safe_float(float("nan")) == 0.0
    assert safe_float(float("inf")) == 0.0
    assert safe_float(float("-inf")) == 0.0
    assert safe_float("not-a-number") == 0.0
    assert safe_float(42) == 42.0
    assert safe_float(3.5, default=10.0) == 3.5


# ── N2.2: atomic bookbag writes ──────────────────────────────────────────────

def test_bookbag_write_is_atomic_no_partial_json(tmp_path, monkeypatch):
    """A reader must never see partial JSON. bookbag.py reads BOOKBAG_DIR at
    import time, so patch the module globals (not just env) to redirect."""
    import bookbag

    monkeypatch.setattr(bookbag, "BOOKBAG_DIR", tmp_path)
    monkeypatch.setattr(bookbag, "SIGNAL_DIR", tmp_path / "sig")
    monkeypatch.setattr(bookbag, "LOCK_DIR", tmp_path / "lock")

    bag = bookbag.write_bookbag("b1", student="coder", domain="python-coding", output="x" * 5000)
    # File on disk is valid JSON and equals what was returned (use bead_path so
    # we follow the same namespaced resolution the writer used).
    on_disk = json.loads(bookbag.bead_path("b1").read_text())
    assert on_disk["bead"] == "b1"
    # update is also atomic + consistent
    bookbag.update_bookbag("b1", output="y" * 5000)
    on_disk2 = json.loads(bookbag.bead_path("b1").read_text())
    assert on_disk2["output"].startswith("y")


# ── N3.1: budget-aware admission reserves cap*timeout ───────────────────────

def test_admission_reserves_cap_times_timeout():
    from crew_admission import decide_admission

    # cap=2, 1700s remaining, timeout 900s → needs 2*900+30=1830s → denied.
    d = decide_admission(
        dispatched=0, configured_cap=2, runner_slots=2, active_claims=0,
        remaining_seconds=1700, crew_timeout_seconds=900, retry_pressure=0,
    )
    assert d.admitted is False
    assert d.reason == "insufficient_cycle_time"
    # 1900s remaining → fits → admitted.
    d2 = decide_admission(
        dispatched=0, configured_cap=2, runner_slots=2, active_claims=0,
        remaining_seconds=1900, crew_timeout_seconds=900, retry_pressure=0,
    )
    assert d2.admitted is True


# ── N3.2: monotonic lifecycle ───────────────────────────────────────────────

def test_is_stale_monotonic_immune_to_wallclock():
    # start at monotonic 1000, now 1000+950, stale_after 900 → stale.
    assert is_stale_monotonic(1000.0, 1950.0, 900.0) is True
    assert is_stale_monotonic(1000.0, 1880.0, 900.0) is False


def test_sweep_uses_monotonic_when_present(tmp_path):
    """sweep_stale_runs must prefer started_monotonic so wall-clock skew can't
    reclaim a live crew. Build a record with a fresh monotonic start; even if
    the wall clock says 'old', monotonic age is small → NOT swept."""
    import crew_dispatch
    from pathlib import Path as _P

    reg = tmp_path / "crew_runs.json"
    now_mono = __import__("time").monotonic()
    runs = [{
        "crew_id": "fm-loop-1-1", "issue_number": 1, "status": "running",
        "started_at": "2099-01-01T00:00:00Z",  # absurd wall clock (would look ancient)
        "started_monotonic": now_mono,  # but just started on monotonic clock
    }]
    reg.write_text(json.dumps(runs))
    removed = crew_dispatch.sweep_stale_runs(
        now_monotonic=now_mono + 10,  # only 10s of monotonic age
        stale_after=900, path=reg,
    )
    assert removed == 0, "live crew with fresh monotonic start must NOT be swept"


# ── N4.1: worktree isolation ────────────────────────────────────────────────

def test_verify_worktree_isolated_rejects_foreign_diff():
    # Isolated: a crew on its own fm/ branch with its own new/untracked files.
    clean = ["## fm/abc...origin", "?? new_file.py", "A  added_file.py", "  M staged_clean.py"]
    ok, findings = verify_worktree_isolated(clean, issue_branch_prefix="fm/")
    assert ok is True, findings
    # NOT isolated: a modification to an existing tracked file (foreign diff
    # from a prior occupant) or a branch that isn't this crew's.
    dirty = [" M issue-2/unrelated.py", "D  deleted_track.py", "## main"]
    ok2, findings2 = verify_worktree_isolated(dirty, issue_branch_prefix="fm/")
    assert ok2 is False and len(findings2) == 3


# ── N4.3: force_agent allowlist ─────────────────────────────────────────────

def test_force_agent_allowlist_blocks_escalation():
    # allowed: equals capability profile or its lora twin
    assert force_agent_allowed("python-coder", "python-coder") is True
    assert force_agent_allowed(None, "python-coder") is True
    # escalation: a low-trust role forcing a high-trust profile is denied
    assert force_agent_allowed("principal", "python-coder") is False
    # no capability context + a forced agent → fail closed
    assert force_agent_allowed("principal", None) is False


def test_director_force_agent_denied_falls_back():
    """Wiring check (N4.3): the allowlist logic used inline in director.run_task
    must deny an escalated forced role and allow the capability's own profile.
    We assert the helper that backs the inline check, since run_task's full path
    requires the model/context machinery."""
    from resilience import force_agent_allowed

    # allowed: equals capability profile (or its lora twin)
    assert force_agent_allowed("python-coder", "python-coder") is True
    assert force_agent_allowed("lora-python-coding", "python-coder", lora_twin="lora-python-coding") is True
    # escalation: a low-trust role forcing a high-trust profile is denied
    assert force_agent_allowed("principal", "python-coder") is False
    # no capability context + a forced agent → fail closed
    assert force_agent_allowed("principal", None) is False


# ── N6.1: bounded grader pool ───────────────────────────────────────────────

def test_bounded_grader_pool_caps():
    assert bounded_grader_pool_size(desired=20, fleet_capacity=5, ledger_safe_max=8) == 5
    assert bounded_grader_pool_size(desired=3, fleet_capacity=10, ledger_safe_max=8) == 3
    assert bounded_grader_pool_size(desired=0, fleet_capacity=10) == 0


# ── N6.2: worktree lease ────────────────────────────────────────────────────

def test_worktree_lease_excludes_second_holder(tmp_path):
    lease_file = tmp_path / "leases.json"
    with worktree_lease(lease_file, "wt-1", "crew-a") as got:
        assert got is True
        with worktree_lease(lease_file, "wt-1", "crew-b") as got2:
            assert got2 is False  # second holder denied
    # released after context
    with worktree_lease(lease_file, "wt-1", "crew-c") as got3:
        assert got3 is True


# ── N6.3: per-daemon CI lock (config) ───────────────────────────────────────

def test_ci_lock_is_per_daemon():
    """The execute job's concurrency group must be keyed on the daemon, not a
    single global group, so a fleet runs N jobs without mutual lock-out."""
    yml = (Path(__file__).parent.parent / ".github/workflows/school-loop.yml").read_text()
    assert "school-core-live-orca-${{ inputs.orca_daemon || 'default' }}" in yml
    assert "group: school-core-live-orca\n" not in yml  # old global group gone


# ── N7.1: retry budget ──────────────────────────────────────────────────────

def test_retry_budget_caps_attempts():
    b = RetryBudget(limit=2)
    assert b.allow(0) is True
    assert b.allow(1) is True
    assert b.allow(2) is False
    assert b.remaining(1) == 1


# ── N7.2: non-fatal label queue ─────────────────────────────────────────────

def test_label_queue_keeps_failed_writes():
    q = LabelWriteQueue()
    q.enqueue("repo", 1, "school-done")

    def flaky(repo, issue_number, label):
        raise RuntimeError("github timeout")

    applied = q.drain(flaky)
    assert applied == 0
    assert len(q.pending()) == 1  # durable retry, not dropped


# ── N7.3: fallback backpressure ─────────────────────────────────────────────

def test_backpressure_limits_fallback():
    sem = BackpressureSemaphore(max_concurrent=2)
    with sem.acquire() as ok1, sem.acquire() as ok2, sem.acquire() as ok3:
        assert ok1 is True and ok2 is True and ok3 is False
    # released
    with sem.acquire() as ok4:
        assert ok4 is True


# ── N8.2: resource-clean assertion ──────────────────────────────────────────

def test_assert_resources_clean_flags_leftovers():
    clean, _ = assert_resources_clean()
    assert clean is True
    dirty, findings = assert_resources_clean(orca_worktrees=["wt-orphan"], fm_local_state=["state/x"])
    assert dirty is False and len(findings) == 2
