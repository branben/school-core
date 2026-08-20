"""A crashed adversarial review must not contribute a passing score.

WHY THIS EXISTS
---------------
This is the SECOND instance of the fail-open pattern fixed in ca400aa. That one
was in ``adversarial_reviewer._parse_lens_output`` (an unparseable judge returned
``verdict=PASS, findings=[]``). This one is one layer out, in the bridge:

    issue_bridge._run_adversarial_review, except-block::

        except Exception as e:
            sys.stderr.write("[issue_bridge] Adversarial review failed, falling back: ...")
            return {
                "verdict": "PASS",
                "score": 50.0,
                ...
            }

So when the review CRASHES — model unreachable, timeout, import error, malformed
task dict — the bridge substitutes a **passing verdict with a mid-range score**.

That score is not inert. issue_bridge computes::

    review_score  = adversarial_review.get("score", execution_score)
    combined_score = execution * 0.5 + review * 0.3 + heuristic * 0.2

A crashed review therefore contributes **50.0 at 30% weight** — a silent 15-point
donation toward acceptance from a check that never ran. And ``verdict: "PASS"``
is byte-identical to a real approval, so nothing downstream can tell the
difference.

I found this while fixing ca400aa and recorded it as still-open rather than
fixing it then. Two independent fail-open error paths in one review pipeline is
not a coincidence; it is a habit the codebase had.

THE FIX mirrors ca400aa exactly, deliberately:
  * mark the result as inconclusive with a machine-readable flag
    (``review_failed: True``), so "the reviewer crashed" is distinguishable from
    "the reviewer approved";
  * do NOT flip the verdict to FAIL — the work may be fine and only the checker
    broken, and manufacturing a false rejection destroys signal just as surely
    as manufacturing a false approval;
  * do NOT emit a fabricated score. ``score`` is omitted so the caller's
    ``.get("score", execution_score)`` default takes over, meaning a crashed
    review contributes the EXECUTION score rather than an invented 50.0.

The last point matters: returning 0.0 would be a false rejection dressed as
caution, and returning 50.0 is the bug. Absent is the honest answer.
"""

from unittest.mock import patch

import issue_bridge


ISSUE = {
    "issue_number": 91,
    "title": "add a thing",
    "body": "please add it",
    "domain": "code-implementation",
    "difficulty": "medium",
}
TASK_RESULT = {"response": "print('ok')", "agent": "coder"}


def _review_that_crashes():
    """Force _run_adversarial_review down its except path."""
    return patch(
        "adversarial_reviewer.AdversarialReviewer.review",
        side_effect=RuntimeError("model unreachable"),
    )


class TestCrashedReviewIsNotAnApproval:
    def test_crashed_review_is_flagged_inconclusive(self):
        """REGRESSION: the except block returned a clean PASS.

        A crashed review must be distinguishable from an approval by machine,
        not just by a line on stderr that nothing parses.
        """
        with _review_that_crashes():
            review = issue_bridge._run_adversarial_review(TASK_RESULT, ISSUE, "")

        assert review.get("review_failed") is True, (
            "a crashed review is indistinguishable from a real PASS — nothing "
            "downstream can gate on it"
        )

    def test_crashed_review_does_not_fabricate_a_score(self):
        """The invented 50.0 fed combined_score at 30% weight.

        issue_bridge does `review_score = review.get("score", execution_score)`,
        so OMITTING score makes a crashed review contribute the execution score
        instead of a made-up midpoint.
        """
        with _review_that_crashes():
            review = issue_bridge._run_adversarial_review(TASK_RESULT, ISSUE, "")

        assert review.get("score") != 50.0, (
            "crashed review still donates a fabricated 50.0 into combined_score"
        )
        assert "score" not in review or review["score"] is None, (
            f"crashed review must not assert a score at all; got {review.get('score')!r}"
        )

    def test_crashed_review_preserves_the_error(self):
        """The reason must survive for diagnosis."""
        with _review_that_crashes():
            review = issue_bridge._run_adversarial_review(TASK_RESULT, ISSUE, "")
        assert "model unreachable" in str(review.get("error", ""))

    def test_crashed_review_is_not_flipped_to_fail(self):
        """Guard: do NOT manufacture a false rejection.

        The artifact may be fine and only the checker broken. ca400aa made the
        same call for the same reason — inconclusive, not condemned.
        """
        with _review_that_crashes():
            review = issue_bridge._run_adversarial_review(TASK_RESULT, ISSUE, "")
        assert review.get("verdict") != "FAIL", (
            "a crashed reviewer must not fail the work it never reviewed"
        )


class TestSuccessfulReviewUnaffected:
    """Guards: the working path must not change shape."""

    def test_successful_review_is_not_flagged(self):
        fake = type("R", (), {"to_dict": lambda self: {
            "verdict": "PASS", "score": 88.0, "findings": [],
        }})()
        with patch("adversarial_reviewer.AdversarialReviewer.review", return_value=fake):
            review = issue_bridge._run_adversarial_review(TASK_RESULT, ISSUE, "")
        assert review["verdict"] == "PASS"
        assert review["score"] == 88.0
        assert not review.get("review_failed"), (
            "a successful review was mislabelled as failed"
        )

    def test_successful_fail_verdict_survives(self):
        fake = type("R", (), {"to_dict": lambda self: {
            "verdict": "FAIL", "score": 20.0, "findings": [{"severity": "HIGH"}],
        }})()
        with patch("adversarial_reviewer.AdversarialReviewer.review", return_value=fake):
            review = issue_bridge._run_adversarial_review(TASK_RESULT, ISSUE, "")
        assert review["verdict"] == "FAIL"
        assert review["score"] == 20.0
