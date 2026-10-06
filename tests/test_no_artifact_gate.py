"""Deterministic `no_artifact` gate: a code-shaped submission with no code and
no diff must not PASS review.

WHY THIS EXISTS
---------------
Live defect (issue #121, domain=debugging). The student produced 15 lines of
prose describing steps it *intends* to take — zero code, zero diff — and the
two-judge review returned cto=PASS 79.0 / coo=PASS 79.0 / accepted=True, then
published a PR. Three independent choices conspired:

1. Every lens prompt is scoped to CODE quality (missing imports, off-by-one,
   partial/broken code). No bullet asks "did they produce an artifact at all?".
2. The verdict fails only on CRITICAL/HIGH findings
   (``verdict = FAIL if has_critical_or_high else PASS``). #121 drew 8 LOW
   findings and 0 CRITICAL/HIGH, so PASS was arithmetically forced.
3. Execution was skipped because ``debugging`` is not in director's
   ``executable_domains``, so no execution check ever ran on it.

The fix is a DETERMINISTIC gate (plain Python, not a prompt change — the LLM
lenses are exactly what failed): a code-shaped submission with no artifact
emits a CRITICAL ``no_artifact`` finding, which forces FAIL and flows into the
acceptance gate.

These tests are hermetic: no network, no model, no Orca.
"""

from __future__ import annotations

import pytest

from adversarial_reviewer import (
    CODE_SHAPED_DOMAINS,
    DOMAIN_LENS,
    PROSE_DOMAINS,
    AdversarialReviewer,
    LensType,
    Severity,
    Verdict,
    has_artifact,
    is_code_shaped_domain,
)

# The exact prose-only response that wrongly passed issue #121, inlined as a
# string constant so the test is hermetic (no filesystem dependency on
# data/trajectories/, which is an untracked runtime artifact).
_FIXTURE = (
    "I have analyzed the problem and determined the right approach to address "
    "the issue with the verify shell's network posture. The current behavior "
    "of the verify shell is not enforcing the claimed isolation, which is a "
    "critical security concern. Here are the steps I will take to address "
    "this issue:\n\n"
    "1. **Measure the Current Behavior**: First, I will verify the current "
    "network posture of the verify shell to ensure that it is indeed open "
    "and not enforcing any isolation as claimed.\n\n"
    "2. **Evaluate Nix Sandboxing Options**: I will investigate whether Nix "
    "offers any sandboxing options that can enforce isolation for the verify "
    "shell on darwin-arm64. This includes checking if `pure = true` can be "
    "used to enforce a Nix-level sandbox.\n\n"
    "3. **Implement Isolation if Possible**: If Nix sandboxing options are "
    "available and suitable, I will implement them to enforce the isolation "
    "claimed in the comments. This will involve modifying the `flake.nix` "
    "file to include the necessary sandboxing configurations.\n\n"
    "4. **Document the Actual Network Posture**: If isolation is not practical "
    "or not enforced, I will update the comments in `flake.nix` to accurately "
    "reflect the actual network posture of the verify shell. This will involve "
    "removing any false claims about isolation and documenting the open "
    "network posture.\n\n"
    "5. **Add Tests for Isolation**: If isolation is enforced, I will add "
    "tests to ensure that the isolation is maintained. This will involve "
    "creating commands or tests that fail when network access is available, "
    "ensuring that the isolation claim cannot silently regress.\n\n"
    "6. **Record the Decision**: Finally, I will record the decision made in "
    "the issue, including the option considered and rejected, to ensure "
    "transparency and accountability.\n\n"
    "Let's start by measuring the current network posture of the verify shell."
)


def _no_artifact_findings(result):
    return [f for f in result.findings if f.issue_class == "no_artifact"]


def _reviewer(raw: str) -> AdversarialReviewer:
    """Reviewer whose judge model always returns an empty-findings PASS.

    This is the worst case for the gate: the LLM lenses find NOTHING (exactly
    the #121 shape — no CRITICAL/HIGH from the judges), so only the
    deterministic gate can force the FAIL.
    """
    return AdversarialReviewer(call_model_fn=lambda *a, **kw: '{"findings": []}')


@pytest.fixture(scope="module")
def prose_only_response() -> str:
    response = _FIXTURE
    assert response.startswith("I have analyzed the problem"), (
        "fixture drift: the #121 prose response no longer matches"
    )
    return response


# ── has_artifact detector ─────────────────────────────────────────────────────

class TestHasArtifact:
    def test_prose_only_has_no_artifact(self, prose_only_response):
        assert has_artifact(prose_only_response) is False

    def test_fenced_python_block_is_an_artifact(self):
        assert has_artifact("Here:\n```python\nprint(1)\n```\n") is True

    def test_bare_fence_is_an_artifact(self):
        assert has_artifact("```\nprint(1)\n```\n") is True

    def test_diff_git_block_is_an_artifact(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        assert has_artifact(diff) is True

    def test_empty_text_has_no_artifact(self):
        assert has_artifact("") is False

    def test_unterminated_fence_with_content_is_an_artifact(self):
        # Real code-implementation trajectories emit a single ```python opener
        # with no closing fence. That is code, not prose.
        assert has_artifact("```python\nx = 1\nprint(x)\n") is True

    def test_stray_fence_with_no_content_is_not_an_artifact(self):
        assert has_artifact("just a stray ``` marker") is False


# ── Domain classification ─────────────────────────────────────────────────────

class TestCodeShapedDomains:
    @pytest.mark.parametrize("domain", [
        "code-implementation", "python-coding", "python-testing",
        "debugging", "git-operations",
    ])
    def test_required_domains_are_code_shaped(self, domain):
        assert is_code_shaped_domain(domain) is True
        assert domain in CODE_SHAPED_DOMAINS

    @pytest.mark.parametrize("domain", [
        "docs", "research", "planning", "analysis", "triage-category", "general",
    ])
    def test_prose_domains_are_not_code_shaped(self, domain):
        assert is_code_shaped_domain(domain) is False


# ── Negative case: the exact #121 evidence ────────────────────────────────────

class TestProseOnlyCodeSubmissionFails:
    def test_prose_only_debugging_submission_is_critical_and_fails(
        self, prose_only_response
    ):
        """The exact #121 response, for a code-shaped domain, must produce a
        CRITICAL no_artifact finding and a FAIL verdict — even when the LLM
        lenses return a clean PASS (the defect shape)."""
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "Fix verify shell network posture",
                  "body": "Make the verify shell enforce isolation",
                  "domain": "debugging"},
            lens_types=[LensType.CORRECTNESS],
        )
        findings = _no_artifact_findings(result)
        assert len(findings) == 1, "expected exactly one no_artifact finding"
        assert findings[0].severity == Severity.CRITICAL
        assert result.verdict == Verdict.FAIL

    @pytest.mark.parametrize("domain", [
        "code-implementation", "python-coding", "python-testing",
        "debugging", "git-operations",
    ])
    def test_every_code_shaped_domain_vetoes_prose(self, prose_only_response, domain):
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "T", "body": "B", "domain": domain},
            lens_types=[LensType.CORRECTNESS],
        )
        assert _no_artifact_findings(result), f"{domain} did not veto prose-only"
        assert result.verdict == Verdict.FAIL

    def test_finding_description_states_no_code_or_diff(self, prose_only_response):
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "T", "body": "B", "domain": "debugging"},
            lens_types=[LensType.CORRECTNESS],
        )
        desc = _no_artifact_findings(result)[0].description.lower()
        assert "no code or diff" in desc
        assert "cannot be reviewed as work" in desc


# ── Positive cases: real artifacts must NOT be flagged ────────────────────────

class TestArtifactsAreNotFlagged:
    def _review(self, output):
        return _reviewer("").review(
            output=output,
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )

    def test_fenced_python_block_not_flagged(self):
        result = self._review("Here is the fix:\n```python\ndef f():\n    return 1\n```\n")
        assert _no_artifact_findings(result) == []

    def test_bare_fence_not_flagged(self):
        result = self._review("```\nprint('hi')\n```\n")
        assert _no_artifact_findings(result) == []

    def test_diff_git_block_not_flagged(self):
        diff = (
            "Applied the change:\n\n"
            "diff --git a/a.py b/a.py\n"
            "index 111..222 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        result = self._review(diff)
        assert _no_artifact_findings(result) == []


# ── Guard: prose domains are legitimate, must not be flagged ──────────────────

class TestProseDomainsAreNotFlagged:
    @pytest.mark.parametrize("domain", ["docs", "research", "planning", "analysis"])
    def test_prose_only_non_code_domain_not_flagged(self, prose_only_response, domain):
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "T", "body": "B", "domain": domain},
            lens_types=[LensType.CORRECTNESS],
        )
        assert _no_artifact_findings(result) == [], (
            f"prose is legitimate for {domain}; the gate must stay silent"
        )


# ── Wiring: the finding reaches the acceptance gate ───────────────────────────

class TestAcceptanceGateWiring:
    """Prove the CRITICAL finding flows into director's `accepted` flag.

    `accepted` requires both judges PASS AND no CRITICAL in
    ``execution + build + cto + coo`` findings (director.py ~736-756). The
    gate lands the no_artifact finding in the judge findings, so it must flip
    `accepted` to False — the exact hop that issue_bridge.py:2266 gates on.
    """

    def test_prose_only_submission_is_not_accepted(self, prose_only_response, monkeypatch):
        import director
        from director import _run_two_judge_review

        # Both judges return a clean empty-findings PASS — the #121 defect
        # shape, where the LLM lenses found nothing CRITICAL/HIGH.
        monkeypatch.setattr(
            director, "call_model",
            lambda *a, **kw: '{"findings": []}',
        )
        result = _run_two_judge_review(
            bead="bead-no-artifact",
            output=prose_only_response,
            task={"domain": "debugging", "difficulty": "medium"},
            repo="branben/sound-royale-ny",
        )
        assert result["accepted"] is False
        # The gate forces FAIL at BOTH layers: the judge verdict itself flips
        # (CRITICAL = 25 penalty → score 75, verdict FAIL), and the acceptance
        # recompute sees the CRITICAL regardless.
        assert result["cto_verdict"] == "FAIL" and result["coo_verdict"] == "FAIL"
        finding = next(
            (f for f in result["findings"] if f["issue_class"] == "no_artifact"),
            None,
        )
        assert finding is not None, "no_artifact finding did not reach the packet"
        assert finding["severity"] == "CRITICAL"

    def test_fenced_code_submission_is_still_accepted(self, monkeypatch):
        import director
        from director import _run_two_judge_review

        monkeypatch.setattr(
            director, "call_model",
            lambda *a, **kw: '{"findings": []}',
        )
        result = _run_two_judge_review(
            bead="bead-with-artifact",
            output="```python\nprint('hello')\n```",
            task={"domain": "debugging", "difficulty": "medium"},
            repo="branben/sound-royale-ny",
        )
        assert result["accepted"] is True
        assert not any(
            f["issue_class"] == "no_artifact" for f in result["findings"]
        )


# ── code-review is gated (the original defect) ───────────────────────────────

class TestCodeReviewIsGated:
    """code-review maps to the full code lens set in DOMAIN_LENS, so it must
    be treated as code-shaped. A prose-only code-review submission must FAIL."""

    def test_code_review_prose_only_is_critical_and_fails(self, prose_only_response):
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "Review this code", "body": "Review the PR",
                  "domain": "code-review"},
            lens_types=[LensType.CORRECTNESS],
        )
        findings = _no_artifact_findings(result)
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL
        assert result.verdict == Verdict.FAIL


# ── python-coding regression guard ────────────────────────────────────────────

class TestPythonCodingPreserved:
    """python-coding is NOT a key of DOMAIN_LENS but must remain gated.
    Guards against the trap where deriving from DOMAIN_LENS alone would
    silently drop python-coding."""

    def test_python_coding_prose_only_is_critical_and_fails(self, prose_only_response):
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "Write code", "body": "Implement the feature",
                  "domain": "python-coding"},
            lens_types=[LensType.CORRECTNESS],
        )
        findings = _no_artifact_findings(result)
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL
        assert result.verdict == Verdict.FAIL


# ── Prose domains stay ungated ────────────────────────────────────────────────

class TestProseDomainsStayUngated:
    """planning, analysis, and triage-category are prose-legitimate domains.
    A prose-only submission for these must NOT produce a no_artifact finding."""

    @pytest.mark.parametrize("domain", ["planning", "analysis", "triage-category"])
    def test_prose_only_prose_domain_not_flagged(self, prose_only_response, domain):
        result = _reviewer("").review(
            output=prose_only_response,
            task={"title": "T", "body": "B", "domain": domain},
            lens_types=[LensType.CORRECTNESS],
        )
        assert _no_artifact_findings(result) == [], (
            f"prose is legitimate for {domain}; the gate must stay silent"
        )


# ── Anti-drift guard ──────────────────────────────────────────────────────────

class TestCodeShapedDomainsAntiDrift:
    """Assert CODE_SHAPED_DOMAINS is derived from DOMAIN_LENS, not hardcoded.
    This prevents the two lists from drifting apart again."""

    def test_code_shaped_domains_derived_from_domain_lens(self):
        expected = (frozenset(DOMAIN_LENS) | {'python-coding'}) - PROSE_DOMAINS
        assert CODE_SHAPED_DOMAINS == expected

    def test_code_review_in_code_shaped_domains(self):
        assert "code-review" in CODE_SHAPED_DOMAINS

    def test_python_coding_in_code_shaped_domains(self):
        assert "python-coding" in CODE_SHAPED_DOMAINS

    def test_prose_domains_not_in_code_shaped_domains(self):
        for domain in PROSE_DOMAINS:
            assert domain not in CODE_SHAPED_DOMAINS

