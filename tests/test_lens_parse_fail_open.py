"""Regression tests: an unparseable judge must not silently become an approving judge.

WHY THIS EXISTS
---------------
Live School Loop run 32319064467 (the first cycle that ever cleared all the
preflights) processed three real sound-royale-ny issues and logged::

    lens_parse_failed
    lens_retry_on_parse_failure
    lens_parse_failed
    [director] Two-judge review: CTO=FAIL (score=64), COO=PASS (score=100)

Both `lens_parse_failed` branches in ``_parse_lens_output``
(adversarial_reviewer.py:557 and :564) returned::

    ReviewResult(verdict=Verdict.PASS, findings=[], ...)

That is FAIL-OPEN on the acceptance gate. When a judge model emits output the
parser cannot read, the reviewer reports a clean PASS with zero findings —
indistinguishable from a genuine approval. `director._run_two_judge_review`
then computes ``accepted = cto PASS and coo PASS and ...``, so a broken judge
actively votes to accept work nobody reviewed.

COO scoring 100 on the same issue where the parser failed twice is exactly the
shape of that bug: a perfect score produced by an empty findings list.

The fix must make a parse failure DISTINGUISHABLE from an approval. It does not
have to fail the issue outright — but it must not be able to contribute a PASS
vote to the acceptance calculation.
"""

from unittest.mock import patch

import pytest

from adversarial_reviewer import AdversarialReviewer, LensType, Verdict


def _reviewer(raw_output: str) -> AdversarialReviewer:
    """A reviewer whose model always returns ``raw_output``."""
    return AdversarialReviewer(call_model_fn=lambda *a, **kw: raw_output)


class TestParseFailureIsNotAnApproval:
    UNPARSEABLE = (
        "Sure! Here is my review of the code. Overall it looks reasonable "
        "and I did not find anything alarming, though I would double check "
        "the error handling around the retry path before shipping."
    )

    def test_unparseable_output_is_not_reported_as_a_clean_pass(self):
        """A judge whose output could not be parsed must not look approving.

        REGRESSION: both lens_parse_failed branches returned
        Verdict.PASS with findings=[] — identical to a real approval, so it fed
        a PASS vote into the two-judge acceptance gate.
        """
        r = _reviewer(self.UNPARSEABLE)
        result = r._parse_lens_output(
            self.UNPARSEABLE, LensType.CORRECTNESS.value, difficulty="medium"
        )

        # The distinguishing signal: it must be marked inconclusive somehow.
        # Either a non-PASS verdict, or an explicit parse_failed flag.
        inconclusive = (
            result.verdict != Verdict.PASS
            or getattr(result, "parse_failed", False) is True
        )
        assert inconclusive, (
            "unparseable judge output produced a clean PASS with no marker — "
            "the acceptance gate cannot tell it apart from a real approval"
        )

    def test_parse_failure_is_flagged_on_the_result(self):
        """There must be a machine-readable marker callers can gate on."""
        r = _reviewer(self.UNPARSEABLE)
        result = r._parse_lens_output(
            self.UNPARSEABLE, LensType.CORRECTNESS.value, difficulty="medium"
        )
        assert hasattr(result, "parse_failed"), (
            "ReviewResult has no parse_failed field, so no caller can "
            "distinguish 'judge said fine' from 'judge output was garbage'"
        )
        assert result.parse_failed is True

    def test_a_genuine_pass_is_still_a_pass(self):
        """The fix must not turn real approvals into failures."""
        good = '{"findings": [], "verdict": "PASS", "confidence": 0.9}'
        r = _reviewer(good)
        result = r._parse_lens_output(
            good, LensType.CORRECTNESS.value, difficulty="medium"
        )
        assert result.verdict == Verdict.PASS
        assert getattr(result, "parse_failed", False) is False, (
            "a parseable clean review was mislabelled as a parse failure"
        )

    def test_a_genuine_finding_still_parses(self):
        """Normal findings must be unaffected."""
        good = (
            '{"findings": [{"severity": "HIGH", "description": "unbounded loop",'
            ' "citation": "foo.py:12", "issue_class": "bug"}], "verdict": "FAIL"}'
        )
        r = _reviewer(good)
        result = r._parse_lens_output(
            good, LensType.CORRECTNESS.value, difficulty="medium"
        )
        assert len(result.findings) == 1
        assert getattr(result, "parse_failed", False) is False
