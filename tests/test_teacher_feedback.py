import json

from src.tools.teacher_feedback import (
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
    path = tmp_path / "src.sensors.trajectory.json"
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


# ---------------------------------------------------------------------------
# Producer -> consumer: parse_failed must survive the whole path
#
# `teacher_feedback.py` reads `cto_parse_failed` / `coo_parse_failed` off the
# review dict. `_run_two_judge_review` is what builds that dict -- and it
# computed `parse_failed` only to gate `accepted`, never returning it. So the
# reader always found nothing and recorded `parse_failed: false`, even when a
# judge had crashed and the work was rejected for exactly that reason.
#
# A rejection whose stated cause is absent from the record is unauditable.
# These tests drive the REAL producer and assert the consumer sees it.
# ---------------------------------------------------------------------------


def _two_judge(cto_parse_failed=False, coo_parse_failed=False):
    """Run the real _run_two_judge_review with judge parse failures injected."""
    from unittest.mock import MagicMock, patch

    from src.core.director import _run_two_judge_review

    def _judge(verdict, score, parse_failed):
        result = MagicMock()
        result.verdict = MagicMock()
        result.verdict.value = verdict
        result.score = score
        result.confidence = 0.9
        result.findings = []
        result.parse_failed = parse_failed
        return result

    with (
        patch("src.core.director.CodeExtractor") as ce,
        patch("src.core.director.AdversarialReviewer") as rev,
        patch("src.core.director.OrcaExecutionManager") as orca,
    ):
        ce.language_for_domain.return_value = "python"
        ce.extract.return_value = "print('hello')"
        orca.return_value.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=0,
            stderr="",
            stdout="hello",
            duration_ms=10,
        )
        rev.return_value.review.side_effect = [
            _judge("PASS", 90, cto_parse_failed),
            _judge("PASS", 90, coo_parse_failed),
        ]
        return _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task={"domain": "python-coding", "response": "print('hello')", "bead": "test"},
            # Narrative synthesis calls the real model over HTTP; without this
            # the test blocks on a socket read instead of exercising the gate.
            synthesize_narratives=False,
        )


def test_a_crashed_judge_is_recorded_in_the_review_dict():
    """The producer must return the crash, not just use it to gate `accepted`."""
    review = _two_judge(cto_parse_failed=True)

    assert review["cto_parse_failed"] is True
    assert review["accepted"] is False


def test_the_feedback_record_carries_the_judge_crash():
    review = _two_judge(coo_parse_failed=True)

    evidence = build_teacher_evidence(
        agent="coder", domain="python-coding", difficulty="medium", review=review
    )

    assert evidence["parse_failed"] is True, (
        "a judge crashed and the work was rejected, but the evidence record says "
        "nothing failed to parse -- the rejection is unauditable"
    )


def test_a_clean_review_records_parse_failed_false():
    review = _two_judge()

    evidence = build_teacher_evidence(
        agent="coder", domain="python-coding", difficulty="medium", review=review
    )

    assert evidence["parse_failed"] is False
