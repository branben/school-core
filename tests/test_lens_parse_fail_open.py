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

from src.review.adversarial_reviewer import AdversarialReviewer, LensType, Verdict


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


class TestLensCrashIsNotAnApproval:
    """A lens that RAISED must not count as an approving vote.

    The tests above cover output the parser cannot read. This covers the other
    branch: the lens threw. `review()`'s per-lens `except Exception` returns
    ReviewResult(verdict=PASS, findings=[], confidence=0.0) with parse_failed
    left at its default False -- so director.py:792 (`accepted = cto PASS and
    coo PASS and ... and not parse_failed`) counts a CRASHED lens as a PASS vote
    for work nobody reviewed. The class docstring at
    adversarial_reviewer.py:148-153 states the contract ("INCONCLUSIVE, not an
    approval"); this branch broke it.
    """

    def test_a_raising_lens_is_marked_inconclusive(self):
        def boom(*a, **kw):
            raise RuntimeError("judge model exploded")

        r = AdversarialReviewer(call_model_fn=boom)
        result = r.review(
            output="```python\ndef f():\n    return 1\n```\n",
            task={"title": "t", "body": "b", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert result.parse_failed is True, (
            "a lens that raised returned an unlabeled PASS -- the acceptance "
            "gate counts it as an approving vote"
        )


    def test_a_crash_survives_a_second_lens_finding(self, monkeypatch):
        """A crashed primary lens must stay INCONCLUSIVE when a second lens finds work.

        A crashed lens returns PASS with no findings, which is the condition that
        triggers the circuit-breaker branch. That branch also builds a replacement
        ReviewResult at :401 -- but the replacement is DEAD CODE: `result` is never
        read after :392, and `final_result` (:468) is built fresh from
        `all_findings`, `",".join(l.value for l in lenses)` and the aggregate
        marker. So the marker's survival does not depend on that rebuild; it
        depends on the aggregate deriving from the per-lens results.

        This asserts the OBSERVABLE contract rather than a branch with no effect on
        the return value. An earlier revision of this test asserted
        `lens_used.endswith("(circuit_breaker)")` -- which can never hold, because
        `final_result` overwrites `lens_used` with the joined lens names.
        """
        monkeypatch.setenv("LENS_TRANSPORT_RETRIES", "0")  # one attempt, no backoff

        calls = []

        def boom_then_find(prompt, system_prompt=None, **kw):
            calls.append(system_prompt or "")
            if len(calls) == 1:
                raise RuntimeError("primary lens exploded")
            return (
                '{"findings": [{"severity": "HIGH", "description": "unbounded loop",'
                ' "citation": "foo.py:12", "issue_class": "bug"}], "verdict": "FAIL"}'
            )

        r = AdversarialReviewer(call_model_fn=boom_then_find)
        result = r.review(
            output="```python\ndef f():\n    return 1\n```\n",
            task={"title": "t", "body": "b", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS, LensType.COMPLETENESS],
            circuit_breaker=True,
        )
        assert result.findings, "the second lens's finding was dropped"
        assert result.parse_failed is True, (
            "a crashed lens was folded into a review that reported findings, so "
            "director.py:792 counted the crash as an approving vote"
        )

    def test_a_repeated_lens_does_not_hide_an_earlier_crash(self, monkeypatch):
        """A duplicated lens must not erase an earlier attempt's crash marker.

        `lens_trace` is keyed by lens VALUE, so a caller passing the same lens
        twice overwrites the first attempt: a crashed run is replaced by a clean
        one and the marker disappears. `select_lenses` returns its `override`
        verbatim, so this is reachable through the public `review(lens_types=...)`
        API. The aggregate therefore derives the marker from an ordered LIST of
        every per-lens result, not from the keyed trace.
        """
        monkeypatch.setenv("LENS_TRANSPORT_RETRIES", "0")

        calls = []

        def boom_then_find(prompt, system_prompt=None, **kw):
            calls.append(system_prompt or "")
            if len(calls) == 1:
                raise RuntimeError("first attempt at this lens exploded")
            return '{"findings": [], "verdict": "PASS"}'

        r = AdversarialReviewer(call_model_fn=boom_then_find)
        result = r.review(
            output="```python\ndef f():\n    return 1\n```\n",
            task={"title": "t", "body": "b", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS, LensType.CORRECTNESS],  # same lens twice
        )
        assert result.parse_failed is True, (
            "a repeated lens overwrote the earlier crash in the trace, so the "
            "aggregate reported a clean review of a lens that never ran"
        )
