from director import _attach_teacher_evidence, _resolve_capability_metadata
from issue_bridge import _observability_fields


def test_director_resolves_persona_profile_skills_and_tools():
    capability = _resolve_capability_metadata(
        role="coder",
        domain="python-testing",
        difficulty="medium",
        score=30,
    )

    assert capability is not None
    assert capability["school_role"] == "Teacher"
    assert capability["task_role"] == "coder"
    assert capability["profile"] == "student-coder"
    assert "python" in capability["allowed_tools"]
    assert capability["domain"] == "python-testing"


def test_run_record_fields_preserve_bounded_learning_evidence():
    evidence = {
        "schema_version": 1,
        "accepted": True,
        "routing_feedback": {"success": True, "quality": 0.89},
    }
    capability = {
        "task_role": "coder",
        "profile": "student-coder",
        "allowed_tools": ("python", "testing"),
    }

    fields = _observability_fields(
        {"capability": capability, "teacher_evidence": evidence, "response": "omitted"}
    )

    assert fields == {"capability": capability, "teacher_evidence": evidence}
    assert "response" not in fields


def test_dod_rejection_overrides_raw_teacher_acceptance(monkeypatch, tmp_path):
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text("{}")
    seen = []
    monkeypatch.setattr(
        "director._record_acrouter_outcome",
        lambda agent, success, quality: seen.append((agent, success, quality)),
    )
    result = {
        "agent": "coder",
        "domain": "python-testing",
        "difficulty": "medium",
        "trajectory": str(trajectory),
        "accepted": False,
        "review": {
            "accepted": True,
            "cto_verdict": "PASS",
            "coo_verdict": "PASS",
            "combined_score": 91.0,
        },
    }

    _attach_teacher_evidence(result)

    assert result["teacher_evidence"]["accepted"] is False
    assert seen == [("coder", False, 0.91)]


def test_teacher_evidence_redacts_tokens_and_home_paths():
    from teacher_feedback import build_teacher_evidence

    evidence = build_teacher_evidence(
        "coder",
        "python-testing",
        "easy",
        {
            "accepted": False,
            "cto_verdict": "FAIL",
            "coo_verdict": "FAIL",
            "combined_score": 12,
            "findings": [{
                "severity": "HIGH",
                "description": "Bearer abcdefghijklmnopqrstuvwxyz123456 at /Users/private-user/x",
            }],
        },
    )
    description = evidence["findings"][0]["description"]
    assert "abcdefghijklmnopqrstuvwxyz123456" not in description
    assert "/Users/private-user" not in description
    assert "[REDACTED]" in description
    assert "~" in description


def test_bridge_success_run_record_contains_capability_and_teacher_evidence(
    monkeypatch, tmp_path
):
    import json
    import issue_bridge
    from unittest.mock import MagicMock

    monkeypatch.setattr(issue_bridge, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(issue_bridge, "RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(issue_bridge, "fetch_issues", lambda repo, labels: [{
        "issue_number": 77,
        "title": "Add parser tests",
        "body": "Please add tests.",
        "prompt": "Add parser tests",
        "domain": "python-testing",
        "difficulty": "easy",
    }])
    monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
    monkeypatch.setattr("repo_reader.clone_repo", lambda repo: tmp_path)
    monkeypatch.setattr("repo_reader.build_codebase_context", lambda path, text: "")
    monkeypatch.setattr("issue_bridge._run_verify_gate", lambda *args: None)
    monkeypatch.setattr("issue_bridge._run_entire_sensor", lambda *args: None)
    monkeypatch.setattr("issue_bridge._run_adversarial_review", lambda **kwargs: {
        "verdict": "PASS", "score": 90, "findings": []
    })
    monkeypatch.setattr("issue_bridge.verify_task_output", lambda **kwargs: {
        "score": 92, "verdict": "EXCELLENT", "reasoning": "ok", "gaps": [], "strengths": []
    })
    monkeypatch.setattr("issue_bridge._mark_github_issue", lambda *args, **kwargs: None)

    capability = {"task_role": "coder", "profile": "student-coder", "allowed_tools": ["python"]}
    evidence = {"schema_version": 1, "accepted": True, "routing_feedback": {"success": True, "quality": 0.9}}
    task_result = {
        "status": "success",
        "agent": "coder",
        "domain": "python-testing",
        "difficulty": "easy",
        "response": "Added tests",
        "trajectory": None,
        "task_score": 90,
        "capability": capability,
        "teacher_evidence": evidence,
        "review": {"accepted": True, "cto_verdict": "PASS", "coo_verdict": "PASS"},
    }
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text("{}")
    task_result["trajectory"] = str(trajectory)
    task_result.pop("teacher_evidence")
    monkeypatch.setattr("director.run_task", lambda **kwargs: dict(task_result))
    monkeypatch.setattr("director._record_acrouter_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr("director.get_log", lambda: MagicMock())

    store = MagicMock()
    store.get_score.return_value = 0.0
    store.update_score.return_value = 0.0
    store.gate_for_score.return_value = "Student"
    result = issue_bridge.bridge_issues(
        "owner/repo", store=store, crew_enabled=False
    )

    assert result[0]["capability"] == capability
    assert result[0]["teacher_evidence"]["accepted"] is True
    assert result[0]["teacher_evidence"]["persisted_to_trajectory"] is True
    saved = json.loads((tmp_path / "last_run.json").read_text())[-1]
    assert saved["capability"] == capability
    assert saved["teacher_evidence"]["accepted"] is True
    assert saved["teacher_evidence"]["persisted_to_trajectory"] is True
    assert "response" not in saved
    assert "prompt" not in saved
    assert "review" not in saved
    assert "issue" in saved and saved["issue"] == 77
