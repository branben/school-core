import json

from teacher_feedback import (
    build_teacher_evidence,
    persist_teacher_evidence,
    routing_signal,
)


def _review(**overrides):
    value = {
        "cto_verdict": "PASS",
        "coo_verdict": "PASS",
        "cto_score": 92.0,
        "coo_score": 86.0,
        "combined_score": 89.0,
        "accepted": True,
        "findings": [
            {"severity": "LOW", "issue_class": "style", "description": "Use a clearer name."},
            {"severity": "HIGH", "issue_class": "edge_case", "description": "Handle an empty input."},
        ],
    }
    value.update(overrides)
    return value


def test_teacher_evidence_is_bounded_and_contains_routing_signal():
    evidence = build_teacher_evidence(
        agent="coder",
        domain="python-testing",
        difficulty="medium",
        review=_review(),
    )

    assert evidence["agent"] == "coder"
    assert evidence["domain"] == "python-testing"
    assert evidence["accepted"] is True
    assert evidence["routing_feedback"] == {"success": True, "quality": 0.89}
    assert evidence["finding_counts"] == {"HIGH": 1, "LOW": 1}
    assert len(evidence["findings"]) == 2
    assert all(len(item["description"]) <= 240 for item in evidence["findings"])


def test_teacher_evidence_persists_on_existing_trajectory(tmp_path):
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps({"agent": "coder", "response": "ok"}))
    evidence = build_teacher_evidence("coder", "python-testing", "easy", _review())

    assert persist_teacher_evidence(path, evidence) is True
    saved = json.loads(path.read_text())
    assert saved["response"] == "ok"
    assert saved["teacher_evidence"] == evidence


def test_rejected_review_has_zero_or_bounded_quality():
    evidence = build_teacher_evidence(
        "coder",
        "code-review",
        "hard",
        _review(
            cto_verdict="FAIL",
            coo_verdict="PASS",
            combined_score=35.0,
            accepted=False,
        ),
    )

    assert routing_signal(evidence) == (False, 0.35)
    assert evidence["accepted"] is False


def test_teacher_evidence_redacts_provider_and_assignment_tokens():
    evidence = build_teacher_evidence(
        "coder",
        "security",
        "hard",
        _review(
            findings=[
                {
                    "severity": "HIGH",
                    "description": (
                        "github_pat_abcdefghijklmnopqrstuvwxyz "
                        "OMNIROUTE_API_KEY=sk-live-secret "
                        "AGENTMAIL_API_KEY=agent-secret"
                    ),
                }
            ]
        ),
    )

    description = evidence["findings"][0]["description"]
    assert "github_pat_abcdefghijklmnopqrstuvwxyz" not in description
    assert "OMNIROUTE_API_KEY=sk-live-secret" not in description
    assert "AGENTMAIL_API_KEY=agent-secret" not in description
    assert description.count("[REDACTED]") == 3
