"""Hermetic vertical pilot for the canonical issue-to-learning path."""

import json

import director
import issue_bridge
import repo_reader


def test_issue_bridge_persists_joined_pilot_evidence(
    tmp_path, monkeypatch, store
):
    """A representative issue reaches route, assurance, and learning records."""
    issue = {
        "issue_number": 901,
        "title": "Pilot route evidence",
        "body": "Create the smoke-proof artifact.",
        "domain": "code-implementation",
        "difficulty": "easy",
        "prompt": "Create the smoke-proof artifact.",
        "category": "feature",
        "state": "ready-for-agent",
        "bd_id": "school-core-dms",
        "plan_id": "docs/plans/pilot.md",
        "plan_unit": "U1",
        "wayfinder_id": "school-core-wayfinder-v1",
    }
    clone = tmp_path / "clone"
    clone.mkdir()

    monkeypatch.setattr(issue_bridge, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(issue_bridge, "RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(issue_bridge, "_mark_github_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(issue_bridge, "notify_issue_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(issue_bridge, "fetch_issues", lambda *args, **kwargs: [issue])
    monkeypatch.setattr(repo_reader, "cleanup_stale_caches", lambda: None)
    monkeypatch.setattr(repo_reader, "clone_repo", lambda *args, **kwargs: clone)
    monkeypatch.setattr(repo_reader, "build_codebase_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        director,
        "run_task",
        lambda **kwargs: {
            "status": "success",
            "agent": "coder",
            "domain": issue["domain"],
            "difficulty": issue["difficulty"],
            "prompt": issue["prompt"],
            "response": "SMOKE: ready",
            "bead": "bookbag-pilot",
            "task_score": 90,
            "review": {},
        },
    )
    monkeypatch.setattr(
        issue_bridge,
        "_run_verify_gate",
        lambda *args, **kwargs: {"passed": True, "ran": 1, "failures": []},
    )
    monkeypatch.setattr(issue_bridge, "_run_entire_sensor", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        issue_bridge,
        "_run_adversarial_review",
        lambda *args, **kwargs: {
            "verdict": "PASS", "score": 90, "findings": []
        },
    )
    monkeypatch.setattr(
        issue_bridge,
        "verify_task_output",
        lambda *args, **kwargs: {
            "score": 90, "verdict": "GOOD", "gaps": [], "strengths": []
        },
    )
    monkeypatch.setattr(
        director,
        "evaluate_and_update",
        lambda result, score, store=None: {"old_score": 50, "new_score": 54, "gate_crossed": False},
    )

    results = issue_bridge.bridge_issues(
        "branben/school-core",
        store=store,
        crew_enabled=False,
        cycle_session_id="loop-pilot-1",
    )

    assert results[0]["status"] == "success"
    assert results[0]["evidence_join"]["control"] == {
        "route_id": "route-bookbag-pilot",
        "bd_id": "school-core-dms",
        "plan_id": "docs/plans/pilot.md",
        "plan_unit": "U1",
        "wayfinder_id": "school-core-wayfinder-v1",
        "knowledge_anchor": None,
        "primary_workflow": None,
        "chosen_skill": None,
    }
    assert results[0]["evidence_join"]["runtime"]["cycle_session_id"] == "loop-pilot-1"

    run = json.loads((tmp_path / "last_run.json").read_text())[-1]
    assert run["evidence_join"] == results[0]["evidence_join"]
    assert run["lifecycle"] == "completed"

    learning = json.loads((tmp_path / "compound_learning.json").read_text())[-1]
    assert learning["trigger"] == "bead_completed"
    assert learning["evidence"]["control"]["bd_id"] == "school-core-dms"
