"""B3 completion: the PR body must carry the FULL acceptance evidence chain.

Bead school-core-bri (B3) requires the PR body to include:
    1. verify gate result (commands run, exit code)   — present
    2. two-judge scores (CTO + COO + combined)        — present
    3. acceptance status                              — MISSING
    4. artifact path if crew path                     — MISSING
    5. "a human reading the PR should see WHY it passed without leaving GitHub"

Verified against the committed body builder (pr_creator.py:360-371): it emits
Review, Verify gate, and Pre-merge check, but never states whether the work was
ACCEPTED, and never surfaces the crew artifact path. `grep -nE
"artifact_path|accepted|crew_used" pr_creator.py` matches only comment prose.

Why each gap matters, concretely:

* ACCEPTANCE STATUS. "CTO PASS / COO PASS" is not the verdict. Live run
  32319064467 produced `CTO=PASS (79), COO=PASS (82) -> REJECTED` — rejected by
  a CRITICAL finding veto (director.py:618). A reader seeing two PASSes would
  conclude the opposite of what happened. And since ca400aa an unparseable judge
  also blocks acceptance while still reporting verdict=PASS. So the verdict is
  NOT derivable from the two judge verdicts; it has to be stated.

* ARTIFACT PATH. The crew path's whole output is a report.md whose
  branch/commit/base identity must match the status detail
  (crew_dispatch.py:860-910). When a crew PR eventually lands, the artifact path
  is the only way a human can check the handshake that produced it.
"""

from unittest.mock import patch

import pr_creator


def _base_kwargs(**over):
    kw = dict(
        issue={
            "issue_number": 77,
            "title": "add a thing",
            "domain": "code-implementation",
            "difficulty": "medium",
        },
        task_result={"response": "print('ok')", "agent": "coder"},
        repo="owner/repo",
        review_evidence={
            "cto_verdict": "PASS",
            "coo_verdict": "PASS",
            "combined_score": 80.5,
        },
        combined_score=80.5,
        dry_run=True,
    )
    kw.update(over)
    return kw


def _captured_body(**over):
    """Render the PR body directly.

    ``create_pr_for_issue`` short-circuits on ``dry_run`` (pr_creator.py:248)
    before the body is built, so the body was unreachable through the public
    entry point. ``build_pr_body`` is the extracted, directly-testable renderer.
    """
    kw = dict(
        issue={
            "issue_number": 77,
            "title": "add a thing",
            "domain": "code-implementation",
            "difficulty": "medium",
        },
        response_text="print('ok')",
        agent="coder",
        review_evidence={
            "cto_verdict": "PASS",
            "coo_verdict": "PASS",
            "combined_score": 80.5,
        },
        combined_score=80.5,
    )
    kw.update(over)
    return pr_creator.build_pr_body(**kw)


class TestPrBodyAcceptanceStatus:
    def test_body_states_accepted_explicitly(self):
        """The body must say ACCEPTED, not leave it inferred from two PASSes.

        REGRESSION: run 32319064467 logged CTO=PASS COO=PASS -> REJECTED (a
        CRITICAL finding vetoed it). Two PASSes do not mean accepted.
        """
        body = _captured_body(review_evidence={
            "cto_verdict": "PASS", "coo_verdict": "PASS",
            "combined_score": 80.5, "accepted": True,
        })
        assert "ccept" in body, (
            "PR body never states acceptance status; a reader cannot tell "
            "ACCEPTED from REJECTED when both judges say PASS"
        )

    def test_body_shows_rejected_when_not_accepted(self):
        body = _captured_body(review_evidence={
            "cto_verdict": "PASS", "coo_verdict": "PASS",
            "combined_score": 80.5, "accepted": False,
        })
        low = body.lower()
        assert "reject" in low or "not accepted" in low, (
            "a vetoed result renders identically to an accepted one"
        )


class TestPrBodyFlagsUnreviewedWork:
    """A crashed review must be visible to whoever decides to merge.

    _run_adversarial_review fails closed (review_failed=True) and omits its
    score, so the review component silently falls back to the execution score.
    The combined number then looks fully earned even though the check never ran.
    """

    def test_review_failure_is_surfaced(self):
        body = _captured_body(review_evidence={
            "cto_verdict": "PASS", "coo_verdict": "PASS",
            "combined_score": 72.0, "accepted": True,
            "review_failed": True, "error": "model unreachable",
        })
        assert "DID NOT RUN" in body, (
            "PR body hides that the adversarial review crashed — a reviewer "
            "would read the score as earned"
        )
        assert "model unreachable" in body, "the failure reason was dropped"
        assert "UNREVIEWED" in body

    def test_healthy_review_adds_no_warning(self):
        """A guard that fires when nothing is wrong gets ignored."""
        body = _captured_body()
        assert "DID NOT RUN" not in body
        assert "UNREVIEWED" not in body


class TestPrBodyArtifactPath:
    def test_crew_artifact_path_is_surfaced(self):
        """B3: 'artifact path if crew path'."""
        body = _captured_body(
            artifact_path="data/crew/task-123/report.md",
            crew_used=True,
        )
        assert "report.md" in body, (
            "crew artifact path is absent from the PR body, so the handshake "
            "that produced it cannot be checked from GitHub"
        )

    def test_direct_path_does_not_invent_an_artifact_line(self):
        body = _captured_body()
        assert "Artifact" not in body, (
            "direct-path PR advertises an artifact it does not have"
        )


class TestPrBodyRegressionGuards:
    """The evidence already present must not be lost by this change."""

    def test_existing_evidence_still_rendered(self):
        body = _captured_body()
        for token in ("CTO", "COO", "Verify gate", "Pre-merge check"):
            assert token in body, f"lost existing evidence line: {token}"
