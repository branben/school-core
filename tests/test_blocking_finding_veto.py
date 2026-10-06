"""Tests for BLOCKING_CLASSES veto logic in director.py.

Issue #139: The acceptance veto fired only on Severity.CRITICAL, but the
execution path never emits CRITICAL — its worst finding is HIGH. So a
detected syntax error could not block acceptance.

Fix: Replace the CRITICAL-only veto with an explicit BLOCKING_CLASSES set.
Non-functional classes (runtime_failure, not_executable, timeout,
verify_failed, verify_gate_error) block at whatever severity the path emits.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from director import _run_two_judge_review, Severity, BLOCKING_CLASSES


def _make_judge_result(verdict: str, score: float, findings=None):
    """Create a mock judge result."""
    r = MagicMock()
    r.verdict = MagicMock()
    r.verdict.value = verdict
    r.score = score
    r.confidence = 0.9
    r.findings = findings or []
    r.parse_failed = False
    return r


def _make_task(domain="python-coding"):
    return {
        "domain": domain,
        "response": "print('hello')",
        "bead": "test-bead",
    }


def _mock_judges(mock_cls, cto_verdict="PASS", cto_score=90, coo_verdict="PASS", coo_score=90):
    """Configure AdversarialReviewer mock to return given verdicts."""
    mock_reviewer = MagicMock()
    mock_reviewer.review.side_effect = [
        _make_judge_result(cto_verdict, cto_score),
        _make_judge_result(coo_verdict, coo_score),
    ]
    mock_cls.return_value = mock_reviewer


class TestBlockingClassesVeto:
    """Test that BLOCKING_CLASSES veto works correctly."""

    @patch("director.CodeExtractor")
    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_runtime_failure_blocks_acceptance(self, mock_orca_cls, mock_reviewer_cls, mock_ce_cls):
        """runtime_failure (HIGH severity) must block acceptance."""
        mock_ce_cls.language_for_domain.return_value = "python"
        mock_ce_cls.extract.return_value = "print('hello')"
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=1,
            stderr="NameError: name 'x' is not defined",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        assert result["accepted"] is False
        assert result["has_blocking_finding"] is True

    @patch("director.CodeExtractor")
    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_not_executable_blocks_acceptance(self, mock_orca_cls, mock_reviewer_cls, mock_ce_cls):
        """not_executable (CRITICAL severity) must block acceptance."""
        mock_ce_cls.language_for_domain.return_value = "python"
        mock_ce_cls.extract.return_value = "print('hello')"
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=True,
            not_executable_reason="SyntaxError: invalid syntax",
            timed_out=False,
            exit_code=1,
            stderr="SyntaxError: invalid syntax",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        assert result["accepted"] is False
        assert result["has_blocking_finding"] is True

    @patch("director.CodeExtractor")
    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_timeout_blocks_acceptance(self, mock_orca_cls, mock_reviewer_cls, mock_ce_cls):
        """timeout (HIGH severity) must block acceptance."""
        mock_ce_cls.language_for_domain.return_value = "python"
        mock_ce_cls.extract.return_value = "print('hello')"
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=True,
            exit_code=None,
            stderr="timed out after 30s",
            duration_ms=30000,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        assert result["accepted"] is False
        assert result["has_blocking_finding"] is True

    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_execution_passed_does_not_block(self, mock_orca_cls, mock_reviewer_cls):
        """execution_passed (LOW severity) must NOT block acceptance."""
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=0,
            stderr="",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        assert result["accepted"] is True
        assert result["has_blocking_finding"] is False

    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_no_code_found_does_not_block(self, mock_orca_cls, mock_reviewer_cls):
        """no_code_found (LOW severity) must NOT block acceptance."""
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=0,
            stderr="",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        assert result["accepted"] is True
        assert result["has_blocking_finding"] is False

    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_sandbox_error_does_not_block(self, mock_orca_cls, mock_reviewer_cls):
        """sandbox_error (LOW severity) must NOT block acceptance."""
        mock_orca = MagicMock()
        mock_orca.execute.side_effect = Exception("Orca unavailable")
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        assert result["accepted"] is True
        assert result["has_blocking_finding"] is False

    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_language_not_supported_does_not_block(self, mock_orca_cls, mock_reviewer_cls):
        """language_not_supported (LOW severity) must NOT block acceptance."""
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=0,
            stderr="",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(domain="code-implementation"),
        )

        assert result["accepted"] is True
        assert result["has_blocking_finding"] is False


class TestCombinedScore:
    """Test that combined_score includes execution findings."""

    @patch("director.CodeExtractor")
    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_combined_score_includes_execution_passed(self, mock_orca_cls, mock_reviewer_cls, mock_ce_cls):
        """combined_score = (cto + coo + execution) / 3 when execution passed."""
        mock_ce_cls.language_for_domain.return_value = "python"
        mock_ce_cls.extract.return_value = "print('hello')"
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=0,
            stderr="",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls, cto_score=90, coo_score=80)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        # (90 + 80 + 100) / 3 = 90.0
        assert result["combined_score"] == 90.0

    @patch("director.CodeExtractor")
    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_combined_score_includes_execution_failed(self, mock_orca_cls, mock_reviewer_cls, mock_ce_cls):
        """combined_score = (cto + coo + 0) / 3 when execution failed."""
        mock_ce_cls.language_for_domain.return_value = "python"
        mock_ce_cls.extract.return_value = "print('hello')"
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=1,
            stderr="NameError",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls, cto_score=90, coo_score=80)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(),
        )

        # (90 + 80 + 0) / 3 = 56.67
        assert result["combined_score"] == pytest.approx(56.67, abs=0.1)

    @patch("director.AdversarialReviewer")
    @patch("director.OrcaExecutionManager")
    def test_combined_score_execution_not_run(self, mock_orca_cls, mock_reviewer_cls):
        """combined_score = (cto + coo + 50) / 3 when execution not run."""
        mock_orca = MagicMock()
        mock_orca.execute.return_value = MagicMock(
            not_executable=False,
            timed_out=False,
            exit_code=0,
            stderr="",
            duration_ms=100,
        )
        mock_orca_cls.return_value = mock_orca
        _mock_judges(mock_reviewer_cls, cto_score=90, coo_score=80)

        result = _run_two_judge_review(
            bead="test",
            output="print('hello')",
            task=_make_task(domain="planning"),  # not executable
        )

        # (90 + 80 + 50) / 3 = 73.33
        assert result["combined_score"] == pytest.approx(73.33, abs=0.1)


class TestBlockingClassesSet:
    """Test that BLOCKING_CLASSES contains the right values."""

    def test_blocking_classes_values(self):
        """BLOCKING_CLASSES must contain all non-functional issue classes."""
        expected = {
            "runtime_failure",
            "not_executable",
            "timeout",
            "verify_failed",
            "verify_gate_error",
        }
        assert BLOCKING_CLASSES == expected

    def test_non_blocking_classes_not_in_set(self):
        """Non-blocking issue classes must NOT be in BLOCKING_CLASSES."""
        non_blocking = {
            "execution_passed",
            "no_code_found",
            "language_not_supported",
            "sandbox_error",
            "verification_passed",
            "verification_skipped",
        }
        for cls in non_blocking:
            assert cls not in BLOCKING_CLASSES, f"{cls} should not be blocking"
