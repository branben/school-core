"""F1: one canonical review/evidence packet and authoritative verdict."""

import json
from unittest.mock import MagicMock, patch

import director
import issue_bridge
from adversarial_reviewer import ReviewResult, Verdict
from bookbag import write_bookbag
from review_packet import ReviewPacket


def _packet(*, accepted=True):
    return ReviewPacket.create(
        artifact={"bead": "bead-1", "repo": "owner/repo"},
        execution={"findings": []},
        verification={"passed": True, "ran": 1, "failures": []},
        entire=None,
        cto={"verdict": "PASS", "score": 90, "findings": []},
        coo={"verdict": "PASS", "score": 88, "findings": []},
        accepted=accepted,
    )


def test_review_packet_is_bounded_and_preserves_authority():
    packet = _packet()
    data = packet.to_dict()

    assert data["schema_version"] == 1
    assert data["authority"] == "director"
    assert data["accepted"] is True
    assert data["artifact"] == {"bead": "bead-1", "repo": "owner/repo"}
    assert data["judges"]["cto"]["verdict"] == "PASS"
    assert data["judges"]["coo"]["score"] == 88
    assert "prompt" not in json.dumps(data).lower()
    assert "response" not in json.dumps(data).lower()

    restored = ReviewPacket.from_dict(data)
    assert restored.is_authoritative
    assert restored.accepted is True
    assert restored.verification["ran"] == 1


def test_director_emits_packet_alongside_legacy_review_fields():
    class _FakeReviewer:
        def __init__(self, call_model_fn=None):
            self.call_model_fn = call_model_fn

        def review(self, **kwargs):
            return ReviewResult(verdict=Verdict.PASS, findings=[])

    bead = "packet-director"
    write_bookbag(
        bead,
        student="coder",
        domain="documentation",
        difficulty="easy",
        task="write docs",
        output="docs",
    )
    with patch("director.AdversarialReviewer", _FakeReviewer), \
         patch("director.call_model", side_effect=RuntimeError("no model in test")):
        result = director._run_two_judge_review(
            bead=bead,
            output="docs",
            task={"domain": "documentation", "difficulty": "easy"},
            repo="owner/repo",
        )

    packet = ReviewPacket.from_dict(result["review_packet"])
    assert packet is not None
    assert packet.is_authoritative
    assert packet.accepted is result["accepted"]
    assert packet.is_verification_authoritative is False
    assert result["review_packet"]["judges"]["cto"]["verdict"] == result["cto_verdict"]
    assert result["review_packet"]["judges"]["coo"]["verdict"] == result["coo_verdict"]


def test_empty_verification_is_not_authoritative():
    packet = ReviewPacket.create(verification="")
    empty_mapping_packet = ReviewPacket.create(verification={})

    assert packet.is_verification_authoritative is False
    assert empty_mapping_packet.is_verification_authoritative is False


def test_skipped_verification_is_authoritative_evidence():
    packet = ReviewPacket.create(
        verification={
            "passed": False,
            "skipped": True,
            "ran": 0,
            "failures": [{"cmd": "(nix)", "stderr": "Nix missing"}],
        },
    )

    assert packet.is_verification_authoritative is True


def test_bridge_reuses_packet_instead_of_duplicate_gate_or_review(
    monkeypatch, tmp_path, store,
):
    monkeypatch.setattr(issue_bridge, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(issue_bridge, "RETRY_FILE", tmp_path / "retry.json")
    monkeypatch.setattr(issue_bridge, "fetch_issues", lambda repo, labels: [{
        "issue_number": 902,
        "title": "Reuse packet",
        "body": "",
        "prompt": "Reuse packet",
        "domain": "code-implementation",
        "difficulty": "easy",
    }])
    monkeypatch.setattr("repo_reader.cleanup_stale_caches", lambda: None)
    monkeypatch.setattr("repo_reader.clone_repo", lambda repo: tmp_path)
    monkeypatch.setattr("repo_reader.build_codebase_context", lambda path, text: "")
    verify = MagicMock(return_value={"passed": True, "ran": 1, "failures": []})
    duplicate_review = MagicMock(return_value={"verdict": "FAIL", "score": 0, "findings": []})
    monkeypatch.setattr(issue_bridge, "_run_verify_gate", verify)
    monkeypatch.setattr(issue_bridge, "_run_adversarial_review", duplicate_review)
    monkeypatch.setattr(issue_bridge, "_run_entire_sensor", lambda path: None)
    monkeypatch.setattr(issue_bridge, "verify_task_output", lambda **kwargs: {
        "score": 90, "verdict": "EXCELLENT", "reasoning": "ok", "gaps": [], "strengths": [],
    })
    monkeypatch.setattr(issue_bridge, "_mark_github_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(issue_bridge, "_build_school_comment", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(issue_bridge, "notify_issue_alert", lambda *args, **kwargs: True)

    review = {
        "cto_verdict": "PASS", "coo_verdict": "PASS", "cto_score": 90,
        "coo_score": 88, "combined_score": 89, "findings": [], "accepted": True,
        "build_verification": json.dumps({"passed": True, "ran": 1, "failures": []}),
    }
    monkeypatch.setattr("director.run_task", lambda **kwargs: {
        "status": "success", "agent": "coder", "domain": "code-implementation",
        "difficulty": "easy", "response": "done", "task_score": 89,
        "review": review, "review_packet": _packet().to_dict(),
    })

    results = issue_bridge.bridge_issues("owner/repo", store=store, crew_enabled=False)

    assert results[0]["status"] == "success"
    assert results[0]["adversarial_review"]["canonical"] is True
    assert results[0]["adversarial_review"]["score"] == 89
    assert results[0]["review_packet"]["entire"] is None
    saved = json.loads((tmp_path / "last_run.json").read_text())[-1]
    assert saved["review_packet"]["authority"] == "director"
    verify.assert_not_called()
    duplicate_review.assert_not_called()
