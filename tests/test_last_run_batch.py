"""D1: batched last_run persistence regression coverage."""

import json
import os
from unittest.mock import patch

from issue_bridge import RunBatch, bridge_issues


def test_run_batch_flushes_multiple_entries_with_one_atomic_replace(tmp_path):
    path = tmp_path / "last_run.json"
    batch = RunBatch(path)

    with patch("issue_bridge.os.replace", wraps=os.replace) as replace:
        batch.append({"issue": 1, "status": "success"})
        assert replace.call_count == 1
        assert path.exists()
        assert batch.pending_count == 0

        batch.append({"issue": 2, "status": "retry"})
        assert replace.call_count == 2
        assert batch.pending_count == 0

    runs = json.loads(path.read_text())
    assert [entry["issue"] for entry in runs] == [1, 2]
    assert all("timestamp" in entry for entry in runs)


def test_run_batch_preserves_existing_history_and_corrupt_state_recovery(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text("not json")
    batch = RunBatch(path)
    batch.append({"issue": 3, "status": "school-failed"})
    batch.flush()

    runs = json.loads(path.read_text())
    assert runs == [{"issue": 3, "status": "school-failed", "timestamp": runs[0]["timestamp"]}]


def test_run_batch_filters_malformed_history_entries(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text(json.dumps([None, {"issue": 7, "status": "success"}, "bad"]))
    batch = RunBatch(path)
    batch.append({"issue": 8, "status": "retry"})
    batch.flush()

    runs = json.loads(path.read_text())
    assert [entry["issue"] for entry in runs] == [7, 8]


def test_run_batch_retains_pending_entries_when_write_fails(tmp_path):
    batch = RunBatch(tmp_path / "last_run.json")

    with patch("issue_bridge._write_run_entries", side_effect=OSError("disk full")):
        batch.append({"issue": 9, "status": "retry"})

    assert batch.pending_count == 1


def test_run_batch_caps_growth_to_last_run_max(tmp_path, monkeypatch):
    """Gap-G5: the append-only journal is bounded to LAST_RUN_MAX newest runs."""
    monkeypatch.setattr("issue_bridge.LAST_RUN_MAX", 3)
    path = tmp_path / "last_run.json"
    batch = RunBatch(path)

    for i in range(6):
        batch.append({"issue": i, "status": "success"})

    runs = json.loads(path.read_text())
    assert [entry["issue"] for entry in runs] == [3, 4, 5]
    # Each flush truncated to the newest cap — the oldest entries are gone.
    assert len(runs) == 3


def test_run_batch_cap_disabled_when_zero(tmp_path, monkeypatch):
    """Gap-G5: LAST_RUN_MAX=0 disables capping (unbounded history)."""
    monkeypatch.setattr("issue_bridge.LAST_RUN_MAX", 0)
    path = tmp_path / "last_run.json"
    batch = RunBatch(path)

    for i in range(6):
        batch.append({"issue": i, "status": "success"})

    runs = json.loads(path.read_text())
    assert [entry["issue"] for entry in runs] == [0, 1, 2, 3, 4, 5]


def test_bridge_cycle_flushes_all_run_outcomes_once(tmp_path, monkeypatch, store):
    """A multi-issue cycle writes its append-only journal in one replacement."""
    issues = [
        {
            "issue_number": 101,
            "title": "First retry",
            "body": "",
            "domain": "debugging",
            "difficulty": "easy",
            "prompt": "retry",
            "category": "bug",
            "state": "ready-for-agent",
        },
        {
            "issue_number": 102,
            "title": "Second retry",
            "body": "",
            "domain": "debugging",
            "difficulty": "easy",
            "prompt": "retry",
            "category": "bug",
            "state": "ready-for-agent",
        },
    ]
    monkeypatch.setattr("issue_bridge.PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr("issue_bridge.RETRY_FILE", tmp_path / "retry.json")

    with (
        patch("issue_bridge.fetch_issues", return_value=issues),
        patch("src.core.repo_reader.cleanup_stale_caches"),
        patch("src.core.repo_reader.clone_repo", return_value=tmp_path / "repo"),
        patch("src.core.repo_reader.build_codebase_context", return_value=""),
        patch("src.core.director.run_task", return_value={"status": "error", "error": "temporary"}),
        patch("issue_bridge.notify_issue_alert"),
        patch("issue_bridge.os.replace", wraps=os.replace) as replace,
    ):
        results = bridge_issues("owner/repo", store=store, crew_enabled=False)

    assert [result["status"] for result in results] == ["retry", "retry"]
    assert replace.call_count == 2
    runs = json.loads((tmp_path / "last_run.json").read_text())
    assert [entry["issue"] for entry in runs] == [101, 102]
    assert [entry["status"] for entry in runs] == ["retry", "retry"]
