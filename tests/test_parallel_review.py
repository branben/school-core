"""F2: CTO and COO reviews run concurrently with deterministic merge order."""

import threading
from unittest.mock import patch

import director
from adversarial_reviewer import ReviewResult, Verdict
from bookbag import write_bookbag


def test_normal_review_can_skip_non_gating_narrative_enrichment():
    class _PassingReviewer:
        def __init__(self, call_model_fn=None):
            pass

        def review(self, **kwargs):
            return ReviewResult(verdict=Verdict.PASS, findings=[])

    bead = "no-narrative-review"
    write_bookbag(
        bead,
        student="coder",
        domain="documentation",
        difficulty="easy",
        task="write docs",
        output="docs",
    )
    with patch("director.AdversarialReviewer", _PassingReviewer), \
         patch("director.call_model", side_effect=RuntimeError("no model in test")), \
         patch("director._synthesize_judge_narratives") as narrative:
        result = director._run_two_judge_review(
            bead=bead,
            output="docs",
            task={"domain": "documentation", "difficulty": "easy"},
            synthesize_narratives=False,
        )

    assert result["accepted"] is True
    assert result["cto_narrative"] is None
    assert result["coo_narrative"] is None
    narrative.assert_not_called()


def test_cto_and_coo_reviews_overlap_and_merge_deterministically():
    barrier = threading.Barrier(2, timeout=2)
    roles_seen = []

    class _BarrierReviewer:
        def __init__(self, call_model_fn=None):
            self.call_model_fn = call_model_fn

        def review(self, **kwargs):
            lenses = kwargs["lens_types"]
            role = "cto" if len(lenses) == 2 else "coo"
            roles_seen.append(role)
            barrier.wait()
            return ReviewResult(verdict=Verdict.PASS, findings=[])

    bead = "parallel-review"
    write_bookbag(
        bead,
        student="coder",
        domain="documentation",
        difficulty="easy",
        task="write docs",
        output="docs",
    )
    with patch("director.AdversarialReviewer", _BarrierReviewer), \
         patch("director.call_model", side_effect=RuntimeError("no model in test")), \
         patch("director._synthesize_judge_narratives", return_value=(None, None)):
        result = director._run_two_judge_review(
            bead=bead,
            output="docs",
            task={"domain": "documentation", "difficulty": "easy"},
        )

    assert set(roles_seen) == {"cto", "coo"}
    assert result["cto_verdict"] == "PASS"
    assert result["coo_verdict"] == "PASS"
    assert result["accepted"] is True
