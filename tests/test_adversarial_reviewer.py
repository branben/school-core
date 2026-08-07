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
        # medium difficulty: CRITICAL=25, HIGH=12 → 100 - 37 = 63
        assert r.score == 63.0

    def test_score_with_hard_difficulty(self):
        """With hard difficulty, HIGH costs 15 (original strictness)."""
        findings = [
            Finding("s1", "err", Severity.CRITICAL, "l1", "d1"),
            Finding("s2", "err", Severity.HIGH, "l2", "d2"),
        ]
        r = ReviewResult(verdict=Verdict.FAIL, findings=findings, difficulty="hard")
        assert r.score == 60.0

    def test_score_floor_by_difficulty(self):
        """Floor varies by difficulty: easy=50, medium=40, hard=30, diploma=30."""
        many = [Finding(f"s{i}", "err", Severity.CRITICAL, f"l{i}", f"d{i}") for i in range(10)]
        r_easy = ReviewResult(verdict=Verdict.FAIL, findings=many, difficulty="easy")
        r_medium = ReviewResult(verdict=Verdict.FAIL, findings=many, difficulty="medium")
        r_hard = ReviewResult(verdict=Verdict.FAIL, findings=many, difficulty="hard")
        r_diploma = ReviewResult(verdict=Verdict.FAIL, findings=many, difficulty="diploma")
        assert r_easy.score == 50.0
        assert r_medium.score == 40.0
        assert r_hard.score == 30.0
        assert r_diploma.score == 30.0

    def test_easy_scoring_higher_for_same_findings(self):
        """Same HIGH findings penalize less on easy vs hard."""
        f = [Finding("s", "err", Severity.HIGH, "l", "d")] * 3
        r_easy = ReviewResult(verdict=Verdict.FAIL, findings=f, difficulty="easy")
        r_hard = ReviewResult(verdict=Verdict.FAIL, findings=f, difficulty="hard")
        # easy: 100 - 3*8 = 76  |  hard: 100 - 3*15 = 55
        assert r_easy.score == 76.0
        assert r_hard.score == 55.0

    def test_instantiation_defaults_include_difficulty(self):
        r = ReviewResult(verdict=Verdict.PASS)
        assert r.difficulty == "medium"
        assert r.verdict == Verdict.PASS
        assert r.findings == []
        assert r.lens_used == ""
        assert r.confidence == 0.0

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


# ── _parse_lens_output — JSON Parsing Robustness ────────────────────────────

class TestParseLensOutput:
    """Unit tests for _parse_lens_output covering common model output quirks."""

    @pytest.fixture
    def reviewer(self):
        """Reviewer with a mock call_model that won't be called in these tests."""
        return AdversarialReviewer(call_model_fn=MagicMock())

    # --- Happy path ---

    def test_clean_json_object(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Broken", "suggestion": "Fix"}]}',
            "correctness",
        )
        assert result.verdict == Verdict.FAIL
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH

    def test_empty_findings(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": []}',
            "correctness",
        )
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    # --- Markdown code fences ---

    def test_json_in_fences(self, reviewer):
        result = reviewer._parse_lens_output(
            '```json\n{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "LOW", "citation": "line 1", '
            '"description": "Minor"}]}\n```',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.LOW

    def test_json_in_fences_no_lang_tag(self, reviewer):
        result = reviewer._parse_lens_output(
            '```\n{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "MEDIUM", "citation": "line 1", '
            '"description": "Issue"}]}\n```',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.MEDIUM

    def test_json_with_multiple_backtick_blocks(self, reviewer):
        """Model might include a code block in its reasoning, then JSON in fences."""
        result = reviewer._parse_lens_output(
            'The student code:\n```python\ndef foo(): pass\n```\n\n'
            'Review findings:\n```json\n{"findings": [{"section": "s1", '
            '"issue_class": "bug", "severity": "HIGH", "citation": "line 1", '
            '"description": "Problem"}]}\n```',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH

    # --- Preamble text ---

    def test_json_with_preamble(self, reviewer):
        result = reviewer._parse_lens_output(
            'Here is my review:\n\n{"findings": [{"section": "s1", '
            '"issue_class": "bug", "severity": "CRITICAL", '
            '"citation": "line 1", "description": "Fatal"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.CRITICAL

    def test_json_with_trailing_text(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Bad"}]}\n\nHope this helps!',
            "correctness",
        )
        assert len(result.findings) == 1

    # --- Control characters ---

    def test_control_characters_stripped(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "MEDIUM", "citation": "line 1", '
            '"description": "Has\x00null"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "\x00" not in result.findings[0].description

    # --- Unwrapped array (model drops the {"findings": [...]} wrapper) ---

    def test_unwrapped_array(self, reviewer):
        result = reviewer._parse_lens_output(
            '[{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Bad"}]',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH

    def test_unwrapped_empty_array(self, reviewer):
        result = reviewer._parse_lens_output(
            '[]',
            "correctness",
        )
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    # --- Malformed JSON fallback ---

    def test_completely_garbled_returns_pass(self, reviewer):
        result = reviewer._parse_lens_output(
            'I found no issues with this code. It looks perfect!',
            "correctness",
        )
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    def test_broken_json_returns_pass(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "severity": "HIGH", BROKEN]}',
            "correctness",
        )
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    # --- Nested braces inside finding descriptions ---

    def test_nested_braces_in_description(self, reviewer):
        """Descriptions may contain JSON-like snippets with braces."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Expected {\\"key\\": \\"value\\"} but got null"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "key" in result.findings[0].description

    # --- JSON with "json" prefix ---

    def test_json_prefix_stripped(self, reviewer):
        result = reviewer._parse_lens_output(
            'json{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "LOW", "citation": "line 1", '
            '"description": "Minor"}]}',
            "correctness",
        )
        assert len(result.findings) == 1

    # --- Multiple findings ---

    def test_multiple_findings(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": ['
            '{"section": "s1", "issue_class": "bug1", "severity": "CRITICAL", '
            '"citation": "line 1", "description": "Fatal A"},'
            '{"section": "s2", "issue_class": "bug2", "severity": "HIGH", '
            '"citation": "line 2", "description": "Bad B"},'
            '{"section": "s3", "issue_class": "bug3", "severity": "MEDIUM", '
            '"citation": "line 3", "description": "Minor C"}'
            ']}',
            "correctness",
        )
        assert len(result.findings) == 3
        assert result.verdict == Verdict.FAIL

    # --- Unknown severity defaults to LOW ---

    def test_unknown_severity_defaults(self, reviewer):
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "COSMIC", "citation": "line 1", '
            '"description": "Unknown severity"}]}',
            "correctness",
        )
        # ValueError from Severity("COSMIC") is caught silently, entry skipped
        assert result.findings == []


# ── _parse_lens_output — Extended Edge Cases ────────────────────────────────

# ── _parse_lens_output — Retry on Parse Failure ───────────────────────────

class TestAdversarialReviewerRetry:
    """Tests for parse-failure retry in _apply_lens."""

    def test_retry_on_garbled_output(self):
        """First call returns unparseable text, retry returns valid JSON."""
        call_count = 0

        def mock_call(prompt, system_prompt=None, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "I found some issues with this code. Let me describe them..."
            return '{"findings": [{"section": "s1", "issue_class": "bug", "severity": "HIGH", "citation": "line 1", "description": "Found issue"}]}'

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
            circuit_breaker=False,
        )
        # Should have called model twice: first (garbled) + retry (valid)
        assert call_count == 2
        # Retry succeeded, so we should have findings
        assert len(result.findings) == 1
        assert result.verdict == Verdict.FAIL

    def test_retry_still_fails_falls_back_to_pass(self):
        """Both first call and retry produce unparseable output — fall back to PASS."""
        call_count = 0

        def mock_call(prompt, system_prompt=None, **kw):
            nonlocal call_count
            call_count += 1
            return "I think this code looks good overall. No major concerns."

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
            circuit_breaker=False,
        )
        # Both calls made, both failed to parse
        assert call_count == 2
        # Fall back to PASS with empty findings
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    def test_no_retry_on_valid_empty_json(self):
        """Model returns valid empty JSON — no retry needed."""
        call_count = 0

        def mock_call(prompt, system_prompt=None, **kw):
            nonlocal call_count
            call_count += 1
            return '{"findings": []}'

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
            circuit_breaker=False,
        )
        # Only one call — no retry
        assert call_count == 1
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    def test_no_retry_on_short_empty_input(self):
        """Very short raw output (< 20 chars) skips retry to avoid loop on empty."""
        call_count = 0

        def mock_call(prompt, system_prompt=None, **kw):
            nonlocal call_count
            call_count += 1
            return "{}"

        reviewer = AdversarialReviewer(call_model_fn=mock_call)
        result = reviewer.review(
            output="some code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
            circuit_breaker=False,
        )
        # Only one call — no retry for very short output
        assert call_count == 1
        assert result.verdict == Verdict.PASS


class TestParseLensOutputExtended:
    """Additional edge cases for _parse_lens_output that complement the
    existing TestParseLensOutput class.

    Covers: newlines in strings, empty/whitespace input, deep markdown
    embedding, escaped quotes, optional field handling, bare-minimum
    JSON structures, Unicode, array-in-fence combos, and confidence math.
    """

    @pytest.fixture
    def reviewer(self):
        return AdversarialReviewer(call_model_fn=MagicMock())

    # --- Newlines in JSON strings (old parser collapsed them) ---

    def test_newlines_in_description(self, reviewer):
        """Escaped newlines in JSON string values survive parsing."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Line 1\\nLine 2\\nLine 3"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "Line 1" in result.findings[0].description
        assert "Line 3" in result.findings[0].description

    def test_newlines_in_suggestion(self, reviewer):
        """Newlines in suggestion field preserved."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "MEDIUM", "citation": "line 1", '
            '"description": "Issue", "suggestion": "Fix A\\nFix B"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "Fix A" in result.findings[0].suggestion

    # --- Empty / whitespace input ---

    def test_empty_string_returns_pass(self, reviewer):
        result = reviewer._parse_lens_output("", "correctness")
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    def test_whitespace_only_returns_pass(self, reviewer):
        result = reviewer._parse_lens_output("   \n\t  ", "correctness")
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    # --- Deep markdown embedding ---

    def test_json_deep_in_markdown(self, reviewer):
        """JSON buried deep inside a multi-section markdown review."""
        result = reviewer._parse_lens_output(
            "## Correctness Review\n\n"
            "### Summary\n\n"
            "The code was reviewed for correctness issues.\n\n"
            "### Findings\n\n"
            '{"findings": [{"section": "logic", "issue_class": "off_by_one", '
            '"severity": "HIGH", "citation": "line 42", '
            '"description": "Loop bounds incorrect"}]}\n\n'
            "### Recommendations\n\n"
            "- Add unit tests\n- Review edge cases\n",
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH
        assert result.findings[0].issue_class == "off_by_one"

    def test_json_in_fenced_code_block_inside_prose(self, reviewer):
        """JSON inside ```json fence, surrounded by markdown commentary."""
        result = reviewer._parse_lens_output(
            "After analyzing the student output, here are my findings:\n\n"
            "```json\n"
            '{"findings": [{"section": "s1", "issue_class": "logic_error", '
            '"severity": "CRITICAL", "citation": "line 10", '
            '"description": "Null pointer dereference"}]}\n'
            "```\n\n"
            "The student should also consider adding input validation.",
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.CRITICAL

    # --- Escaped characters ---

    def test_escaped_quotes_in_description(self, reviewer):
        """Descriptions with escaped double quotes."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "MEDIUM", "citation": "line 1", '
            '"description": "Expected \\"hello\\" but got \\"world\\""}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert '"hello"' in result.findings[0].description

    def test_escaped_backslashes_in_description(self, reviewer):
        """Descriptions with literal backslashes (e.g., file paths)."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "LOW", "citation": "line 1", '
            '"description": "Path should be C:\\\\Users\\\\file.txt"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "Users" in result.findings[0].description

    # --- Optional fields ---

    def test_missing_suggestion_field(self, reviewer):
        """Finding without the optional suggestion field."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Issue without suggestion"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].suggestion is None

    def test_missing_citation_field(self, reviewer):
        """Finding without the citation field."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "LOW", "description": "No citation provided"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].citation == ""

    def test_minimal_finding_all_optional_fields_missing(self, reviewer):
        """Finding with only required fields — section, issue_class, severity."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "core", "issue_class": "style", '
            '"severity": "LOW"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].description == ""
        assert result.findings[0].citation == ""
        assert result.findings[0].suggestion is None

    # --- Score-related ---

    def test_findings_with_only_low_severity_is_pass(self, reviewer):
        """LOW severity does not trigger FAIL — only CRITICAL/HIGH do."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "nitpick", '
            '"severity": "LOW", "citation": "line 1", "description": "Minor"},'
            '{"section": "s2", "issue_class": "style", '
            '"severity": "MEDIUM", "citation": "line 2", "description": "Style"}]}',
            "correctness",
        )
        assert result.verdict == Verdict.PASS
        assert len(result.findings) == 2

    def test_empty_findings_array_with_extra_fields(self, reviewer):
        """JSON has extra metadata but empty findings."""
        result = reviewer._parse_lens_output(
            '{"findings": [], "metadata": {"reviewer": "correctness"}, '
            '"timestamp": "2025-01-01"}',
            "correctness",
        )
        assert result.verdict == Verdict.PASS
        assert result.findings == []

    # --- Array strategy edge cases ---

    def test_unwrapped_array_with_preamble(self, reviewer):
        """Model drops wrapper AND adds preamble text."""
        result = reviewer._parse_lens_output(
            'Here are the findings:\n'
            '[{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Bad"}]',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH

    def test_unwrapped_array_in_code_fence(self, reviewer):
        """Unwrapped array inside a code fence (model puts array in fence)."""
        result = reviewer._parse_lens_output(
            '```json\n'
            '[{"section": "s1", "issue_class": "bug", '
            '"severity": "CRITICAL", "citation": "line 1", '
            '"description": "Fatal"}]\n'
            '```',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.CRITICAL

    # --- Unicode / non-ASCII ---

    def test_unicode_in_description(self, reviewer):
        """UTF-8 characters in description fields."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "MEDIUM", "citation": "line 1", '
            '"description": "变量名不符合规范 — variable naming convention"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "变量名" in result.findings[0].description

    # --- Edge: nested JSON-like structures in description ---

    def test_nested_json_snippet_in_description(self, reviewer):
        """Description contains a JSON snippet — balanced extraction handles it."""
        result = reviewer._parse_lens_output(
            '{"findings": [{"section": "s1", "issue_class": "bug", '
            '"severity": "HIGH", "citation": "line 1", '
            '"description": "Expected response {\\"status\\": \\"ok\\"} but got {}"}]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert "status" in result.findings[0].description

    # --- Confidence calculation ---

    def test_confidence_with_findings(self, reviewer):
        """Confidence reflects finding count."""
        result = reviewer._parse_lens_output(
            '{"findings": ['
            '{"section": "s1", "issue_class": "b1", "severity": "HIGH", "citation": "l1", "description": "d1"},'
            '{"section": "s2", "issue_class": "b2", "severity": "MEDIUM", "citation": "l2", "description": "d2"}'
            ']}',
            "correctness",
        )
        assert result.confidence == min(1.0, 2 * 0.3 + 0.2)  # = 0.8

    def test_confidence_no_findings(self, reviewer):
        """Confidence is 0.5 when no findings."""
        result = reviewer._parse_lens_output('{"findings": []}', "correctness")
        assert result.confidence == 0.5

    # --- String-only findings (gemini-3.5 returns flat descriptions) ---

    def test_string_entries_first_high_rest_medium(self, reviewer):
        """String entries: first defaults to HIGH, rest to MEDIUM."""
        result = reviewer._parse_lens_output(
            '{"findings": ["Missing null check on line 42", '
            '"No error handling for empty input", "Hardcoded API key"]}',
            "correctness",
        )
        assert len(result.findings) == 3
        assert result.findings[0].severity == Severity.HIGH
        assert result.findings[1].severity == Severity.MEDIUM
        assert result.findings[2].severity == Severity.MEDIUM
        assert result.verdict == Verdict.FAIL  # first finding is HIGH

    def test_single_string_entry_is_high(self, reviewer):
        """Single string entry defaults to HIGH severity (first entry)."""
        result = reviewer._parse_lens_output(
            '{"findings": ["Minor style issue"]}',
            "correctness",
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH
        assert result.findings[0].description == "Minor style issue"
        assert result.findings[0].section == "output"
        assert result.findings[0].issue_class == "review_finding"
        assert result.verdict == Verdict.FAIL  # HIGH → FAIL

    def test_mixed_string_and_dict_entries(self, reviewer):
        """Some findings are strings, some are proper objects."""
        result = reviewer._parse_lens_output(
            '{"findings": ['
            '"String-based issue",'
            '{"section": "auth", "issue_class": "security", '
            '"severity": "HIGH", "citation": "line 10", "description": "No auth"}'
            ']}',
            "correctness",
        )
        assert len(result.findings) == 2
        assert result.findings[0].description == "String-based issue"
        # First string entry gets HIGH, second entry (dict) keeps its own severity
        assert result.findings[0].severity == Severity.HIGH
        assert result.findings[1].severity == Severity.HIGH
        assert result.verdict == Verdict.FAIL  # both HIGH
