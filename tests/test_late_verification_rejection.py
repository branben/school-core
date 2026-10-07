"""Tests for late verification rejection in issue_bridge.

When _select_verification() returns a real failure (passed=False, ran > 0,
not skipped), the canonical packet's accepted flag must be updated BEFORE
the PR gate reads it. Without this, a PR can be created despite a verify
failure because the PR gate checks review_evidence (director's review),
not the bridge's adversarial_review.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review_packet import ReviewPacket


class TestRejectVerification:
    """ReviewPacket.reject_verification() must flip accepted to False."""

    def test_reject_verification_flips_accepted(self):
        packet = ReviewPacket.create(
            accepted=True,
            cto={"verdict": "PASS", "score": 90},
            coo={"verdict": "PASS", "score": 85},
        )
        assert packet.accepted is True

        packet.reject_verification({"passed": False, "ran": 2, "failures": []})

        assert packet.accepted is False

    def test_reject_verification_marks_authoritative(self):
        packet = ReviewPacket.create(
            accepted=True,
            cto={"verdict": "PASS", "score": 90},
            coo={"verdict": "PASS", "score": 85},
        )
        assert packet.is_verification_authoritative is False

        packet.reject_verification({"passed": False, "ran": 2, "failures": []})

        assert packet.is_verification_authoritative is True

    def test_reject_verification_preserves_judges(self):
        """Reject must not rewrite the judges' votes."""
        packet = ReviewPacket.create(
            accepted=True,
            cto={"verdict": "PASS", "score": 90, "findings": [{"severity": "LOW"}]},
            coo={"verdict": "PASS", "score": 85, "findings": []},
        )

        packet.reject_verification({"passed": False, "ran": 2, "failures": []})

        data = packet.to_dict()
        assert data["judges"]["cto"]["verdict"] == "PASS"
        assert data["judges"]["cto"]["score"] == 90
        assert data["judges"]["coo"]["verdict"] == "PASS"
        assert data["judges"]["coo"]["score"] == 85

    def test_reject_verification_sets_verdict_rejected(self):
        packet = ReviewPacket.create(
            accepted=True,
            cto={"verdict": "PASS", "score": 90},
            coo={"verdict": "PASS", "score": 85},
        )

        packet.reject_verification({"passed": False, "ran": 2, "failures": []})

        assert packet.to_dict()["verdict"] == "REJECTED"


class TestLateVerificationCondition:
    """The condition that triggers reject_verification in bridge_issues."""

    def _should_reject(self, verify_result, verify_skipped, canonical_packet):
        """Mirror the condition in bridge_issues.py."""
        return bool(
            verify_result
            and not verify_result.get("passed")
            and not verify_skipped
            and (
                verify_result.get("ran", 0) > 0
                or verify_result.get("strict_escalated")
            )
            and canonical_packet is not None
            and canonical_packet.is_authoritative
        )

    def test_real_failure_triggers_reject(self):
        packet = ReviewPacket.create(accepted=True)
        verify = {"passed": False, "ran": 2, "failures": [{"cmd": "pytest", "exit": 1}]}
        assert self._should_reject(verify, False, packet) is True

    def test_skipped_does_not_trigger(self):
        packet = ReviewPacket.create(accepted=True)
        verify = {"passed": False, "skipped": True, "ran": 0}
        assert self._should_reject(verify, True, packet) is False

    def test_passed_does_not_trigger(self):
        packet = ReviewPacket.create(accepted=True)
        verify = {"passed": True, "ran": 2}
        assert self._should_reject(verify, False, packet) is False

    def test_no_ran_and_no_strict_does_not_trigger(self):
        packet = ReviewPacket.create(accepted=True)
        verify = {"passed": False, "ran": 0}
        assert self._should_reject(verify, False, packet) is False

    def test_strict_escalated_triggers_even_without_ran(self):
        packet = ReviewPacket.create(accepted=True)
        verify = {"passed": False, "ran": 0, "strict_escalated": True}
        assert self._should_reject(verify, False, packet) is True

    def test_no_canonical_packet_does_not_trigger(self):
        verify = {"passed": False, "ran": 2}
        assert self._should_reject(verify, False, None) is False

    def test_non_authoritative_packet_does_not_trigger(self):
        packet = ReviewPacket.create(accepted=True)
        # Make it non-authoritative by removing authority
        packet._data["authority"] = "not_director"
        verify = {"passed": False, "ran": 2}
        assert self._should_reject(verify, False, packet) is False

    def test_none_verify_result_does_not_trigger(self):
        packet = ReviewPacket.create(accepted=True)
        assert self._should_reject(None, False, packet) is False
