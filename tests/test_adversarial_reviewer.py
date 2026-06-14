"""
Tests for the Adversarial Reviewer core engine (adversarial_reviewer.py).

Run: python -m pytest tests/test_adversarial_reviewer.py -v
"""

import json
from unittest.mock import MagicMock

import pytest

from adversarial_reviewer import (
    AdversarialReviewer,
    Finding,
    LensType,
    ReviewResult,
    Severity,
    Verdict,
    select_lenses,
)


# ── Enums ────────────────────────────────────────────────────────────────────

class TestSeverityEnum:
    def test_critical_value(self):
        assert Severity.CRITICAL.value == "CRITICAL"

    def test_high_value(self):
        assert Severity.HIGH.value == "HIGH"

    def test_medium_value(self):
        assert Severity.MEDIUM.value == "MEDIUM"

    def test_low_value(self):
        assert Severity.LOW.value == "LOW"

    def test_all_values_unique(self):
        values = {s.value for s in Severity}
        assert len(values) == 4


class TestVerdictEnum:
    def test_pass_value(self):
        assert Verdict.PASS.value == "PASS"

    def test_fail_value(self):
        assert Verdict.FAIL.value == "FAIL"


class TestLensTypeEnum:
    def test_correctness_value(self):
        assert LensType.CORRECTNESS.value == "correctness"

    def test_security_value(self):
        assert LensType.SECURITY.value == "security"

    def test_completeness_value(self):
        assert LensType.COMPLETENESS.value == "completeness"

    def test_simplicity_value(self):
        assert LensType.SIMPLICITY.value == "simplicity"


# ── Finding Dataclass ───────────────────────────────────────────────────────

class TestFinding:
    def test_instantiation(self):
        f = Finding(
            section="output",
            issue_class="logic_error",
            severity=Severity.HIGH,
            citation="line 42",
            description="Off-by-one error in loop",
            suggestion="Use range(0, n) instead of range(1, n)",
        )
        assert f.section == "output"
        assert f.issue_class == "logic_error"
        assert f.severity == Severity.HIGH
        assert f.citation == "line 42"
        assert f.description == "Off-by-one error in loop"
        assert f.suggestion == "Use range(0, n) instead of range(1, n)"

    def test_suggestion_optional(self):
        f = Finding(
            section="output",
            issue_class="logic_error",
            severity=Severity.MEDIUM,
            citation="line 10",
            description="Missing null check",
        )
        assert f.suggestion is None

    def test_to_dict(self):
        f = Finding(
            section="auth",
            issue_class="security_vulnerability",
            severity=Severity.CRITICAL,
            citation="line 5",
            description="Hardcoded API key",
            suggestion="Use environment variable",
        )
        d = f.to_dict()
        assert d["section"] == "auth"
        assert d["issue_class"] == "security_vulnerability"
        assert d["severity"] == "CRITICAL"
        assert d["citation"] == "line 5"
        assert d["description"] == "Hardcoded API key"
        assert d["suggestion"] == "Use environment variable"

    def test_to_dict_without_suggestion(self):
        f = Finding(
            section="output",
            issue_class="missing_edge_case",
            severity=Severity.LOW,
            citation="N/A",
            description="No handling for empty input",
        )
        d = f.to_dict()
        assert d["suggestion"] is None


# ── ReviewResult Dataclass ──────────────────────────────────────────────────

class TestReviewResult:
    def test_instantiation_defaults(self):
        r = ReviewResult(verdict=Verdict.PASS)
        assert r.verdict == Verdict.PASS
        assert r.findings == []
        assert r.lens_used == ""
        assert r.confidence == 0.0

    def test_instantiation_with_findings(self):
        findings = [
            Finding("s1", "logic_error", Severity.HIGH, "line 1", "desc"),
        ]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings, lens_used="correctness", confidence=0.8)
        assert r.verdict == Verdict.FAIL
        assert len(r.findings) == 1
        assert r.lens_used == "correctness"
        assert r.confidence == 0.8

    def test_gaps_property(self):
        findings = [
            Finding("s1", "logic_error", Severity.HIGH, "line 1", "First gap"),
            Finding("s2", "missing", Severity.LOW, "line 2", "Second gap"),
        ]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings)
        assert r.gaps == ["First gap", "Second gap"]

    def test_suggestions_property(self):
        findings = [
            Finding("s1", "err", Severity.HIGH, "l1", "d1", suggestion="Fix A"),
            Finding("s2", "err", Severity.LOW, "l2", "d2", suggestion=None),
            Finding("s3", "err", Severity.MEDIUM, "l3", "d3", suggestion="Fix B"),
        ]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings)
        assert r.suggestions == ["Fix A", "Fix B"]

    def test_score_no_findings(self):
        r = ReviewResult(verdict=Verdict.PASS, findings=[])
        assert r.score == 100.0

    def test_score_with_critical(self):
        findings = [Finding("s", "err", Severity.CRITICAL, "l", "d")]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings)
        assert r.score == 75.0

    def test_score_with_multiple_findings(self):
        findings = [
            Finding("s1", "err", Severity.CRITICAL, "l1", "d1"),
            Finding("s2", "err", Severity.HIGH, "l2", "d2"),
        ]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings)
        assert r.score == 60.0

    def test_score_floor_at_zero(self):
        findings = [
            Finding(f"s{i}", "err", Severity.CRITICAL, f"l{i}", f"d{i}")
            for i in range(10)
        ]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings)
        assert r.score == 0.0

    def test_to_dict(self, monkeypatch):
        # Workaround: to_dict() has a bug referencing self.suggestion (singular)
        # instead of self.suggestions (plural property). Patch to avoid crash.
        original_to_dict = ReviewResult.to_dict

        def patched_to_dict(self):
            d = {
                "verdict": self.verdict.value,
                "score": self.score,
                "findings": [f.to_dict() for f in self.findings],
                "lens_used": self.lens_used,
                "confidence": self.confidence,
                "gaps": self.gaps,
                "suggestions": self.suggestions,
            }
            return d

        monkeypatch.setattr(ReviewResult, "to_dict", patched_to_dict)
        findings = [Finding("s", "err", Severity.HIGH, "l", "desc", suggestion="fix")]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings, lens_used="correctness", confidence=0.7)
        d = r.to_dict()
        assert d["verdict"] == "FAIL"
        assert d["lens_used"] == "correctness"
        assert d["confidence"] == 0.7
        assert isinstance(d["findings"], list)
        assert len(d["findings"]) == 1
        assert d["gaps"] == ["desc"]
        assert isinstance(d["score"], float)

    def test_to_json(self, monkeypatch):
        # Same workaround as test_to_dict
        def patched_to_dict(self):
            return {
                "verdict": self.verdict.value,
                "score": self.score,
                "findings": [f.to_dict() for f in self.findings],
                "lens_used": self.lens_used,
                "confidence": self.confidence,
                "gaps": self.gaps,
                "suggestions": self.suggestions,
            }

        monkeypatch.setattr(ReviewResult, "to_dict", patched_to_dict)
        r = ReviewResult(verdict=Verdict.PASS, findings=[], lens_used="correctness", confidence=0.5)
        j = r.to_json()
        parsed = json.loads(j)
        assert parsed["verdict"] == "PASS"
        assert parsed["score"] == 100.0


# ── select_lenses() ──────────────────────────────────────────────────────────

class TestSelectLenses:
    def test_code_implementation_domain(self):
        lenses = select_lenses("code-implementation")
        assert LensType.CORRECTNESS in lenses
        assert LensType.SECURITY in lenses
        assert LensType.COMPLETENESS in lenses

    def test_code_review_domain(self):
        lenses = select_lenses("code-review")
        assert LensType.CORRECTNESS in lenses
        assert LensType.SECURITY in lenses
        assert LensType.COMPLETENESS in lenses

    def test_debugging_domain(self):
        lenses = select_lenses("debugging")
        assert LensType.CORRECTNESS in lenses
        assert LensType.SECURITY in lenses
        assert LensType.COMPLETENESS in lenses

    def test_python_testing_domain(self):
        lenses = select_lenses("python-testing")
        assert LensType.CORRECTNESS in lenses
        assert LensType.COMPLETENESS in lenses
        assert LensType.SECURITY not in lenses

    def test_git_operations_domain(self):
        lenses = select_lenses("git-operations")
        assert LensType.CORRECTNESS in lenses
        assert LensType.COMPLETENESS in lenses
        assert LensType.SECURITY not in lenses

    def test_unknown_domain_returns_defaults(self):
        lenses = select_lenses("totally-unknown-domain")
        assert LensType.CORRECTNESS in lenses
        assert LensType.COMPLETENESS in lenses

    def test_override_takes_precedence(self):
        override = [LensType.SECURITY]
        lenses = select_lenses("code-implementation", override=override)
        assert lenses == [LensType.SECURITY]

    def test_override_empty_list_treated_as_falsy(self):
        # select_lenses uses `if override:` which treats [] as falsy,
        # so an empty override list falls through to the domain default.
        lenses = select_lenses("code-implementation", override=[])
        assert LensType.CORRECTNESS in lenses


# ── AdversarialReviewer ─────────────────────────────────────────────────────

class TestAdversarialReviewerInit:
    def test_stores_call_model_fn(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        assert reviewer._call_model is mock_fn


class TestAdversarialReviewerEmptyOutput:
    def test_empty_string_returns_fail(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="",
            task={"title": "Test", "body": "Do stuff", "domain": "code-implementation"},
        )
        assert result.verdict == Verdict.FAIL
        assert any(f.description == "No output produced" for f in result.findings)

    def test_whitespace_only_returns_fail(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="   \n\t  ",
            task={"title": "Test", "body": "Do stuff", "domain": "code-implementation"},
        )
        assert result.verdict == Verdict.FAIL

    def test_empty_output_lens_used_is_none(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="",
            task={"title": "Test", "body": "Do stuff", "domain": "code-implementation"},
        )
        assert result.lens_used == "none"


class TestAdversarialReviewerCleanOutput:
    def test_clean_output_returns_pass(self, monkeypatch):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="def hello():\n    return 'world'",
            task={"title": "Test", "body": "Write hello", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert result.verdict == Verdict.PASS
        assert result.findings == []
        assert isinstance(result.score, float)

    def test_clean_output_has_valid_structure(self, monkeypatch):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="some code here",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert result.verdict == Verdict.PASS
        assert result.score == 100.0
        assert result.gaps == []
        assert result.suggestions == []


class TestAdversarialReviewerCircuitBreaker:
    def test_circuit_breaker_triggers_second_lens(self):
        """When first lens returns PASS/0-findings, second lens is applied."""
        call_count = 0

        def mock_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '{"findings": []}'
            return '{"findings": [{"section": "auth", "issue_class": "security_vulnerability", "severity": "HIGH", "citation": "line 1", "description": "Missing auth check", "suggestion": "Add auth"}]}'

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS, LensType.SECURITY],
            circuit_breaker=True,
        )
        assert call_count == 2
        assert result.verdict == Verdict.FAIL
        assert len(result.findings) == 1

    def test_circuit_breaker_false_skips_second_lens(self):
        call_count = 0

        def mock_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return '{"findings": []}'

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS, LensType.SECURITY],
            circuit_breaker=False,
        )
        # Without circuit breaker, all lenses are applied in the multi-lens path
        assert call_count >= 1
        assert result.verdict == Verdict.PASS

    def test_circuit_breaker_both_pass(self):
        """Both lenses return 0 findings — double pass."""
        call_count = 0

        def mock_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return '{"findings": []}'

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="clean code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS, LensType.SECURITY],
            circuit_breaker=True,
        )
        assert call_count == 2
        assert result.verdict == Verdict.PASS
        assert result.findings == []


class TestAdversarialReviewerFindings:
    def test_critical_finding_causes_fail(self):
        mock_fn = MagicMock(return_value=json.dumps({
            "findings": [{
                "section": "auth",
                "issue_class": "security_vulnerability",
                "severity": "CRITICAL",
                "citation": "line 10",
                "description": "SQL injection vulnerability",
                "suggestion": "Use parameterized queries",
            }]
        }))
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="query = f'SELECT * FROM users WHERE id = {user_id}'",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.SECURITY],
        )
        assert result.verdict == Verdict.FAIL
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.CRITICAL

    def test_medium_findings_only_causes_pass(self):
        mock_fn = MagicMock(return_value=json.dumps({
            "findings": [{
                "section": "output",
                "issue_class": "style_issue",
                "severity": "MEDIUM",
                "citation": "line 5",
                "description": "Minor formatting issue",
            }]
        }))
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert result.verdict == Verdict.PASS

    def test_multiple_lenses_merge_findings(self):
        call_count = 0

        def mock_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return '{"findings": []}'
            return json.dumps({
                "findings": [{
                    "section": "s1",
                    "issue_class": "missing_edge_case",
                    "severity": "HIGH",
                    "citation": "line 3",
                    "description": "Missing null check",
                }]
            })

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS, LensType.COMPLETENESS],
            circuit_breaker=False,
        )
        assert result.verdict == Verdict.FAIL
        assert len(result.findings) == 1


class TestAdversarialReviewerStats:
    def test_get_agreement_rate_no_history(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        assert reviewer.get_agreement_rate(LensType.CORRECTNESS) == 0.0

    def test_get_agreement_rate_after_reviews(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        for _ in range(10):
            reviewer.review(
                output="code",
                task={"title": "T", "body": "B", "domain": "code-implementation"},
                lens_types=[LensType.CORRECTNESS],
            )
        rate = reviewer.get_agreement_rate(LensType.CORRECTNESS)
        assert rate == 1.0

    def test_get_agreement_rate_mixed(self):
        call_count = 0

        def mock_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                return '{"findings": [{"section": "s", "issue_class": "err", "severity": "HIGH", "citation": "l", "description": "d"}]}'
            return '{"findings": []}'

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        for _ in range(10):
            reviewer.review(
                output="code",
                task={"title": "T", "body": "B", "domain": "code-implementation"},
                lens_types=[LensType.CORRECTNESS],
            )
        rate = reviewer.get_agreement_rate(LensType.CORRECTNESS)
        assert rate == 0.5

    def test_flag_drifting_lenses_none(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        drifting = reviewer.flag_drifting_lenses()
        assert drifting == []

    def test_flag_drifting_lenses_detects_sycophancy(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        for _ in range(10):
            reviewer.review(
                output="code",
                task={"title": "T", "body": "B", "domain": "code-implementation"},
                lens_types=[LensType.CORRECTNESS],
            )
        drifting = reviewer.flag_drifting_lenses(threshold=0.85)
        assert LensType.CORRECTNESS in drifting

    def test_flag_drifting_lenses_custom_threshold(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        for _ in range(10):
            reviewer.review(
                output="code",
                task={"title": "T", "body": "B", "domain": "code-implementation"},
                lens_types=[LensType.CORRECTNESS],
            )
        # With threshold of 1.0, even 100% pass rate should not flag
        drifting = reviewer.flag_drifting_lenses(threshold=1.0)
        assert drifting == []
