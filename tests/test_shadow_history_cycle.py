"""Bridge-cycle shadow history reuse regression coverage."""

from unittest.mock import MagicMock

import issue_bridge


def test_shadow_packet_normalizes_legacy_history_input():
    packet = issue_bridge._build_shadow_routing_packet(
        {"agent": "coder", "status": "success"},
        {"difficulty": "easy"},
        80.0,
        0,
        None,
        [],
    )

    assert packet["samples"] == 1
    assert packet["mode"] == "shadow"


def test_bridge_loads_shadow_history_once_per_cycle(monkeypatch, tmp_path, store):
    issues = [
        {
            "issue_number": 901,
            "title": "First issue",
            "body": "",
            "prompt": "first",
            "domain": "debugging",
            "difficulty": "easy",
        },
        {
            "issue_number": 902,
            "title": "Second issue",
            "body": "",
            "prompt": "second",
            "domain": "debugging",
            "difficulty": "easy",
        },
    ]
    monkeypatch.setattr(issue_bridge, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(issue_bridge, "RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(issue_bridge, "fetch_issues", lambda repo, labels: issues)
    monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
    monkeypatch.setattr("repo_reader.clone_repo", lambda repo: tmp_path)
    monkeypatch.setattr("repo_reader.build_codebase_context", lambda path, text: "")
    monkeypatch.setattr(issue_bridge, "_run_verify_gate", lambda *args: None)
    monkeypatch.setattr(issue_bridge, "_run_entire_sensor", lambda *args: None)
    monkeypatch.setattr(issue_bridge, "_run_adversarial_review", lambda *args, **kwargs: {
        "verdict": "PASS", "score": 90, "findings": [],
    })
    monkeypatch.setattr(issue_bridge, "verify_task_output", lambda *args, **kwargs: {
        "score": 90, "verdict": "EXCELLENT", "reasoning": "ok", "gaps": [], "strengths": [],
    })
    monkeypatch.setattr(issue_bridge, "_mark_github_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(issue_bridge, "notify_issue_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(store, "list_agents", lambda: ["coder"])
    monkeypatch.setattr("director.evaluate_and_update", lambda task, score, store: {
        "old_score": 80, "new_score": score, "gate_crossed": False,
    })
    monkeypatch.setattr("director.run_task", lambda **kwargs: {
        "status": "success",
        "agent": "coder",
        "domain": "debugging",
        "difficulty": "easy",
        "response": "done",
        "review": {"accepted": True},
    })

    history = [{"agent": "old", "score": 80, "status": "success"}]
    load_history = MagicMock(return_value=history)
    monkeypatch.setattr(issue_bridge, "load_shadow_history", load_history)

    results = issue_bridge.bridge_issues("owner/repo", store=store, crew_enabled=False)

    assert [result["status"] for result in results] == ["success", "success"]
    load_history.assert_called_once_with(tmp_path / "last_run.json")
    assert results[0]["shadow_routing"]["samples"] == 2
    assert results[1]["shadow_routing"]["samples"] == 2
