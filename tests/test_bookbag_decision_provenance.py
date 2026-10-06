"""RED: a bookbag must record WHY it was accepted or rejected, not just WHETHER.

director.py:647-654 computes `accepted` from six conditions, four of which are
never persisted:

    accepted = (cto_verdict == "PASS" and coo_verdict == "PASS"
                and cto_result.score >= 50 and coo_result.score >= 50
                and not has_critical and not parse_failed)

`update_bookbag` persists cto_verdict, coo_verdict, findings and accepted — but
not the two scores, not has_critical, not parse_failed. So on a real corpus
(286 bags, 87 PASS/PASS, 63 with accepted=false) there is NO way to reconstruct
why a record was rejected. The field is unauditable, and unauditable derived
fields get read as bugs — which is exactly what happened: it was reported as
"63 records where the field lies" before anyone checked the computation.

These tests fail until the deciding inputs are persisted.
"""

import json
import os
import tempfile
import unittest
import unittest.mock as mock

import bookbag
import director
from adversarial_reviewer import ReviewResult, Verdict


class _Judge:
    """Stand-in for a ReviewResult with a controllable score/parse state."""

    def __init__(self, verdict=Verdict.PASS, score=100, findings=None, parse_failed=False):
        self.verdict = verdict
        self.score = score
        self.findings = findings or []
        self.parse_failed = parse_failed
        self.confidence = 1.0


class _PassingDirector(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.mkdtemp()
        self._prev = os.environ.get("HOME")
        os.environ["HOME"] = self._home
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._prev

    def _run(self, cto, coo):
        """Drive _run_two_judge_review with fixed judge results, read the bag.

        The seam is director.AdversarialReviewer + director.call_model — the same
        pair tests/test_parallel_review.py drives. CTO and COO run concurrently,
        so alternate the two fixed results rather than assuming an order.
        """
        state = {"n": 0}

        class _Reviewer:
            def __init__(self, *a, **k):
                pass

            def review(self, **kwargs):
                picked = cto if state["n"] % 2 == 0 else coo
                state["n"] += 1
                return picked

        bookbag.write_bookbag("probe", student="coder", task="t", output="o")
        with mock.patch.object(director, "AdversarialReviewer", _Reviewer), \
             mock.patch.object(director, "call_model",
                               side_effect=RuntimeError("no model in test")), \
             mock.patch.object(director, "_synthesize_judge_narratives",
                               return_value=(None, None)):
            director._run_two_judge_review(
                bead="probe", output="o",
                task={"domain": "x", "difficulty": "easy"},
                synthesize_narratives=False,
            )
        with open(bookbag.bead_path("probe")) as fh:
            return json.load(fh)

    def test_rejected_for_low_score_records_both_scores(self):
        """A rejection a human cannot explain is an unauditable record."""
        bag = self._run(_Judge(score=40), _Judge(score=90))
        self.assertFalse(bag["accepted"], "precondition: PASS/PASS but score < 50")
        self.assertEqual(bag["cto_verdict"], "PASS")
        self.assertEqual(bag["coo_verdict"], "PASS")
        # These keys do not exist yet -> RED.
        self.assertIn("cto_score", bag)
        self.assertIn("coo_score", bag)
        self.assertEqual(bag["cto_score"], 40)
        self.assertEqual(bag["coo_score"], 90)

    def test_parse_failure_is_recorded_not_just_implied(self):
        """A broken judge returns PASS; only parse_failed distinguishes it."""
        bag = self._run(_Judge(parse_failed=True), _Judge())
        self.assertFalse(bag["accepted"], "precondition: a failed parse must not accept")
        self.assertIn("parse_failed", bag)
        self.assertTrue(bag["parse_failed"])

    def test_clean_pass_records_its_scores(self):
        bag = self._run(_Judge(score=100), _Judge(score=100))
        self.assertTrue(bag["accepted"], "precondition: clean pass must accept")
        self.assertIn("cto_score", bag)
        self.assertIn("coo_score", bag)
        self.assertIn("parse_failed", bag)
        self.assertFalse(bag["parse_failed"])


if __name__ == "__main__":
    unittest.main()
