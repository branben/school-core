"""F0 baseline contract: bounded metrics and one complete bridge path."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import issue_bridge
from pipeline_metrics import PipelineMetrics


def test_pipeline_metrics_snapshot_is_bounded_and_redacted():
    metrics = PipelineMetrics(max_events=3)

    with metrics.stage("context"):
        pass
    metrics.record_model("reviewer", prompt_chars=10_000, output_chars=20_000)
    metrics.record_model("reviewer", prompt_chars=10_000, output_chars=20_000)
    metrics.record_verification(invocations=2, shell_starts=2, commands=7, copied_bytes=99)
    metrics.record_context("cocoindex", hit=True, latency_ms=12.5)
    metrics.record_context("serena", hit=False, latency_ms=4.0)
    metrics.record_crew("fallback")
    metrics.record_quality(accepted=False, critical_findings=2, retry_count=1)

    snapshot = metrics.snapshot()

    assert snapshot["schema_version"] == 1
    assert snapshot["events"] <= 3
    assert snapshot["timings_ms"]["context"] >= 0
    assert snapshot["model"]["call_count"] == 2
    assert snapshot["model"]["prompt_chars"] == 20_000
    assert snapshot["model"]["output_chars"] == 40_000
    assert snapshot["verification"] == {
        "gate_invocations": 2,
        "shell_starts": 2,
        "commands": 7,
        "copied_bytes": 99,
    }
    assert snapshot["context"]["sources"] == {
        "cocoindex": {"hits": 1, "misses": 0, "latency_ms": 12.5},
        "serena": {"hits": 0, "misses": 1, "latency_ms": 4.0},
    }
    assert snapshot["crew"]["fallback_count"] == 1
    assert snapshot["quality"] == {
        "accepted": False,
        "critical_findings": 2,
        "retry_count": 1,
    }
    serialized = json.dumps(snapshot)
    assert "response" not in serialized.lower()
    assert "report" not in serialized.lower()
    assert "/Users/" not in serialized
    assert "/home/" not in serialized


def test_bridge_persists_one_issue_characterization_packet(monkeypatch, tmp_path, store):
    """The normal bridge path records stage ownership and gate multiplicity."""
    monkeypatch.setattr(issue_bridge, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(issue_bridge, "RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(issue_bridge, "fetch_issues", lambda repo, labels: [{
        "issue_number": 901,
        "title": "Characterize review path",
        "body": "",
        "prompt": "Characterize review path",
        "domain": "debugging",
        "difficulty": "easy",
    }])
    monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
    monkeypatch.setattr("repo_reader.clone_repo", lambda repo: tmp_path)
    monkeypatch.setattr("repo_reader.build_codebase_context", lambda path, text: "repo context")
    monkeypatch.setattr(issue_bridge, "_run_verify_gate", MagicMock(return_value=None))
    monkeypatch.setattr(issue_bridge, "_run_entire_sensor", MagicMock(return_value=None))
    monkeypatch.setattr(issue_bridge, "_run_adversarial_review", MagicMock(return_value={
        "verdict": "PASS", "score": 90, "findings": [],
    }))
    monkeypatch.setattr(issue_bridge, "verify_task_output", MagicMock(return_value={
        "score": 90, "verdict": "EXCELLENT", "reasoning": "ok", "gaps": [], "strengths": [],
    }))
    monkeypatch.setattr(issue_bridge, "_mark_github_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(issue_bridge, "_build_school_comment", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(issue_bridge, "notify_issue_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr("director.run_task", lambda **kwargs: {
        "status": "success",
        "agent": "coder",
        "domain": "debugging",
        "difficulty": "easy",
        "response": "done",
        "task_score": 90,
        "review": {
            "cto_verdict": "PASS",
            "coo_verdict": "PASS",
            "accepted": True,
            "combined_score": 90,
            "findings": [],
        },
    })

    results = issue_bridge.bridge_issues("owner/repo", store=store, crew_enabled=False)

    assert results[0]["status"] == "success"
    saved = json.loads((tmp_path / "last_run.json").read_text())[-1]
    metrics = saved["pipeline_metrics"]
    assert metrics["schema_version"] == 1
    assert set(("context", "student_generation", "verify", "entire", "review", "scoring", "persistence")) <= set(metrics["timings_ms"])
    assert metrics["calls"] == {
        "verify_gate": 1,
        "entire": 1,
        "adversarial_review": 1,
        "output_verification": 1,
    }
    assert metrics["verification"]["gate_invocations"] == 1
    assert metrics["quality"]["accepted"] is True
    assert metrics["quality"]["critical_findings"] == 0
    assert issue_bridge._run_verify_gate.call_count == 1
    assert issue_bridge._run_entire_sensor.call_count == 1
    assert issue_bridge._run_adversarial_review.call_count == 1
    assert issue_bridge.verify_task_output.call_count == 1
    shadow = results[0]["shadow_routing"]
    assert shadow["mode"] == "shadow"
    assert shadow["live_routing_unchanged"] is True
    assert saved["shadow_routing"] == shadow
    assert "response" not in json.dumps(metrics)
