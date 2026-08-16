"""F6-concurrency / worst-day-ever CRITICAL nodes N5.1, N5.2, N5.3.

Each test proves a specific worst-day failure is now CAUGHT (fail-closed or
lock-safe), not silently swallowed. Run with the project venv pytest.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scoring import ScoreStore


# ── N5.1: registry corruption must fail CLOSED, not empty ────────────────────

def test_corrupt_registry_active_count_fails_closed(tmp_path):
    """A truncated crew_runs.json must NOT make admission think 0 crews are
    in flight (that was the over-admission bug). It must quarantine the file
    and report saturation (sys.maxsize) so decide_admission denies."""
    import issue_bridge

    reg = tmp_path / "crew_runs.json"
    reg.write_text("{ this is not valid json ")  # truncated mid-object

    count = issue_bridge._crew_active_count(reg)
    assert count == sys.maxsize, (
        f"corrupt registry should fail closed (sys.maxsize), got {count}"
    )
    # The bad file was quarantined (moved aside), not left in place.
    assert not reg.exists(), "corrupt registry should have been quarantined"
    assert list(tmp_path.glob("crew_runs.json.corrupt-*")), (
        "expected a .corrupt-<ts> quarantine file"
    )


def test_corrupt_registry_active_issue_fails_closed(tmp_path):
    """A corrupt registry must SKIP the issue (treat as in-flight) to avoid a
    double-spawn, not return False (which would allow re-dispatch)."""
    import issue_bridge

    reg = tmp_path / "crew_runs.json"
    reg.write_text('{"truncated": ')  # invalid

    assert issue_bridge._crew_active_issue(reg, 408) is True
    assert not reg.exists(), "corrupt registry should have been quarantined"


def test_missing_registry_still_empty(tmp_path):
    """A genuinely absent registry is the only valid empty state (no
    quarantine, returns 0 / False)."""
    import issue_bridge

    reg = tmp_path / "crew_runs.json"  # not created
    assert issue_bridge._crew_active_count(reg) == 0
    assert issue_bridge._crew_active_issue(reg, 408) is False
    assert not list(tmp_path.glob("crew_runs.json.corrupt-*"))


# ── N5.2: ScoreStore.save() must be lock-safe under concurrency ──────────────

def test_scorestore_concurrent_writes_no_corruption(tmp_path):
    """Two+ graders sharing ONE ScoreStore (the real bridge_issues pattern, where
    `store` is injected once and passed to every crew) writing concurrently must
    never torn-write / corrupt scores.json, and must not lose updates."""
    scores_file = tmp_path / "scores.json"
    scores_file.write_text("{}")
    store = ScoreStore(file_path=str(scores_file))  # single shared instance

    errors: list[Exception] = []

    def worker(i: int):
        try:
            store.update_score(f"student-{i}", "_default", 70.0 + i)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The file must remain valid JSON and reload cleanly.
    assert not errors, f"concurrent ScoreStore writes raised: {errors}"
    reloaded = ScoreStore(file_path=str(scores_file))
    # Every worker's update must be present (no lost updates / no corruption).
    # First EMA update from old=0 yields task*0.3, so student-i lands at
    # (70+i)*0.3 = 21.0 + 0.3*i — assert exact EMA, not a raw-set value.
    for i in range(12):
        expected = (70.0 + i) * 0.3
        val = reloaded.get_score(f"student-{i}", "_default")
        assert val == pytest.approx(expected), (
            f"student-{i} lost/corrupt after concurrency: got {val}, want ~{expected}"
        )


# ── N5.3: a CREW run must never re-gate the clean base after teardown ────────

def test_crew_run_never_regates_clean_base():
    """For a crew run with missing pre-merge verification, _select_verification
    must return a STRICT gate failure and MUST NOT call _run_verify_gate on the
    clean repo_path (that would be the false-pass hazard)."""
    import issue_bridge

    calls = []
    real_run_verify_gate = issue_bridge._run_verify_gate

    def spy_run_verify_gate(repo_path, issue):
        calls.append(repo_path)
        return real_run_verify_gate(repo_path, issue)

    issue_bridge._run_verify_gate = spy_run_verify_gate
    try:
        metrics = MagicMock()
        repo_path = Path("/tmp/clean_base_that_must_not_be_touched")
        result = issue_bridge._select_verification(
            crew_used=True,
            crew_premerge_verification=None,  # missing after teardown
            canonical_packet=None,
            issue={"issue_number": 408},
            repo_path=repo_path,
            metrics=metrics,
        )
    finally:
        issue_bridge._run_verify_gate = real_run_verify_gate

    assert result is not None, "missing crew verification must be a strict failure, not None"
    assert result.get("passed") is False, (
        f"crew run with no verification must FAIL strict, got {result}"
    )
    assert calls == [], (
        f"_run_verify_gate was called on the clean base — false-pass hazard! "
        f"calls={calls}"
    )


def test_direct_run_still_gates_base():
    """Sanity: the direct (non-crew) path is the ONLY one allowed to run the
    gate on repo_path — confirming we didn't break legitimate verification."""
    import issue_bridge

    calls = []
    real_run_verify_gate = issue_bridge._run_verify_gate

    def spy_run_verify_gate(repo_path, issue):
        calls.append(repo_path)
        return {"passed": True, "score": 100, "findings": []}

    issue_bridge._run_verify_gate = spy_run_verify_gate
    try:
        metrics = MagicMock()
        repo_path = Path("/tmp/legit_clone")
        result = issue_bridge._select_verification(
            crew_used=False,
            crew_premerge_verification=None,
            canonical_packet=None,
            issue={"issue_number": 409},
            repo_path=repo_path,
            metrics=metrics,
        )
    finally:
        issue_bridge._run_verify_gate = real_run_verify_gate

    assert calls == [repo_path], "direct path should gate the clone as before"
    assert result is not None and result.get("passed") is True
