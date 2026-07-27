"""Regression test: two-judge review must persist verdicts to the per-repo namespace.

Guards the bug caught in PR #38 (reported by testdriverai): run_task() writes
the bookbag into the per-repo namespace via write_bookbag(..., repo=repo), but
_run_two_judge_review() persisted results via update_bookbag(bead, ...) with NO
repo — which defaulted to __global__. For a non-global repo the durable verdict
record was silently never written (update_bookbag found no global bookbag and
returned None). Verdicts were dropped on disk.

This test seeds a per-repo bookbag, runs the two-judge review with repo= set,
and asserts the verdict lands in the per-repo namespace (and NOT in global).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from director import _run_two_judge_review, REPO_GLOBAL  # noqa: E402
from bookbag import write_bookbag, read_bookbag, REPO_GLOBAL as BAG_GLOBAL  # noqa: E402
from adversarial_reviewer import ReviewResult, Verdict  # noqa: E402


class _FakeReviewer:
    """Returns an unanimous PASS so the review is accepted."""

    def __init__(self, call_model_fn=None):
        self.call_model_fn = call_model_fn

    def review(self, **kwargs):
        return ReviewResult(verdict=Verdict.PASS, findings=[])


def test_two_judge_review_persists_verdict_to_per_repo_namespace():
    bead = "bead-repo-namespace"
    repo = "branben/sound-royale-ny"

    # Seed a bookbag in the PER-REPO namespace (mirrors run_task's write_bookbag).
    write_bookbag(
        bead,
        student="coder",
        domain="documentation",
        difficulty="easy",
        task="write a doc",
        output="here is the doc",
        repo=repo,
    )

    with patch("director.AdversarialReviewer", _FakeReviewer):
        result = _run_two_judge_review(
            bead=bead,
            output="here is the doc",
            task={"domain": "documentation"},  # non-executable: skips Orca sandbox
            repo=repo,
        )

    assert result["accepted"] is True

    # Verdict MUST be persisted to the per-repo namespace.
    bag = read_bookbag(bead, repo=repo)
    assert bag is not None, "per-repo bookbag should exist after review"
    assert bag["cto_verdict"] == "PASS"
    assert bag["coo_verdict"] == "PASS"

    # And it must NOT leak into the global namespace.
    assert read_bookbag(bead, repo=BAG_GLOBAL) is None


def test_two_judge_review_defaults_to_global_namespace():
    """Without an explicit repo, verdicts go to the global namespace."""
    bead = "bead-global-namespace"

    write_bookbag(
        bead,
        student="coder",
        domain="documentation",
        difficulty="easy",
        task="write a doc",
        output="here is the doc",
        repo=REPO_GLOBAL,
    )

    with patch("director.AdversarialReviewer", _FakeReviewer):
        _run_two_judge_review(
            bead=bead,
            output="here is the doc",
            task={"domain": "documentation"},
        )

    bag = read_bookbag(bead, repo=REPO_GLOBAL)
    assert bag is not None
    assert bag["cto_verdict"] == "PASS"
