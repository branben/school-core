"""Tests for the Verification-co-evolution loop (school-core P2.2).

The loop must:
  - detect capability gains above a margin,
  - flag acceptance-check coverage gaps when a gained dimension is uncovered,
  - propose regenerate/harden actions, and
  - never silently colonize the production check set (default = proposed, human-gated).

Run: python -m pytest tests/test_verification_coevolution.py -v
"""

from unittest.mock import MagicMock

from adversarial_reviewer import (
    AdversarialReviewer,
    CoevolutionReport,
    Finding,
    LensType,
    ReviewResult,
    Severity,
    VerificationCoevolution,
    review_with_coevolution,
)


def _result(findings, lens_types, domain="code-implementation"):
    """Build a ReviewResult with a per-lens trace so analyze() gets real axes."""
    r = ReviewResult(
        verdict="PASS" if not any(f.severity in (Severity.CRITICAL, Severity.HIGH) for f in findings) else "FAIL",
        findings=findings,
        lens_used=",".join(l.value for l in lens_types),
        confidence=0.5,
        difficulty="medium",
    )
    trace = {}
    for lt in lens_types:
        trace[lt.value] = ReviewResult(verdict="PASS", findings=[f for f in findings if False], difficulty="medium")
    # Put all findings under the first lens for the trace rollup.
    first = lens_types[0].value
    trace[first] = ReviewResult(verdict=r.verdict, findings=findings, difficulty="medium")
    r._lens_trace = trace
    return r


class TestCoevolutionReport:
    def test_defaults(self):
        rep = CoevolutionReport()
        assert rep.triggered is False
        assert rep.capability_delta == {}
        assert rep.to_dict()["triggered"] is False

    def test_roundtrip(self):
        rep = CoevolutionReport(
            triggered=True,
            capability_delta={"security": 0.2},
            coverage_gaps=[{"dimension": "security", "gain": 0.2, "covered": False}],
            proposals=[{"dimension": "security", "gain": 0.2}],
            actions_applied=["added check covering 'security'"],
            reason="x",
        )
        d = rep.to_dict()
        assert d["triggered"] is True
        assert d["capability_delta"]["security"] == 0.2
        assert d["actions_applied"]


class TestVerificationCoevolutionBasics:
    def test_no_gain_on_first_sample(self):
        co = VerificationCoevolution()
        res = _result([], [LensType.CORRECTNESS])
        rep = co.analyze(res, {"domain": "code-implementation"}, [])
        assert rep.triggered is False
        assert rep.reason  # explains why nothing changed

    def test_no_gain_below_margin(self):
        co = VerificationCoevolution(capability_margin=0.10)
        # Two identical high-capability passes: no gain.
        co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "code-implementation"}, [])
        rep = co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "code-implementation"}, [])
        assert rep.triggered is False

    def test_gain_triggers_but_covered_no_gap(self):
        co = VerificationCoevolution(capability_margin=0.10)
        # First pass has a LOW finding => reduced capability; then a clean pass => gain.
        co.analyze(_result([Finding("s", "err", Severity.LOW, "l", "d")], [LensType.CORRECTNESS]),
                   {"domain": "code-implementation"}, [])
        # Sudden jump to near-perfect on correctness, and a check already covers it.
        checks = [{"covers": ["correctness"], "status": "active"}]
        rep = co.analyze(
            _result([], [LensType.CORRECTNESS]),
            {"domain": "code-implementation"},
            checks,
        )
        assert rep.triggered is True
        assert rep.capability_delta  # capability rose
        assert rep.coverage_gaps == []  # covered -> no gap, monitor only
        assert "Monitor" in rep.reason

    def test_gain_with_uncovered_dimension_flags_gap(self):
        co = VerificationCoevolution(capability_margin=0.10)
        co.analyze(_result([Finding("s", "err", Severity.LOW, "l", "d")], [LensType.CORRECTNESS]),
                   {"domain": "code-implementation"}, [])
        # Capability jumps; no check covers correctness.
        rep = co.analyze(
            _result([], [LensType.CORRECTNESS]),
            {"domain": "code-implementation"},
            [],  # empty acceptance checks
        )
        assert rep.triggered is True
        assert rep.coverage_gaps
        gap = rep.coverage_gaps[0]
        assert gap["dimension"] == "correctness"
        assert gap["covered"] is False
        assert "reward hacking" in rep.reason


class TestCoevolutionCoverageEdgeCases:
    def test_covers_as_single_string(self):
        co = VerificationCoevolution(capability_margin=0.10)
        co.analyze(_result([], [LensType.SECURITY]), {"domain": "x"}, [])
        rep = co.analyze(
            _result([], [LensType.SECURITY]),
            {"domain": "x"},
            [{"covers": "security", "status": "active"}],  # string, not list
        )
        assert rep.coverage_gaps == []

    def test_high_severity_finding_suppresses_capability(self):
        co = VerificationCoevolution(capability_margin=0.10)
        # First pass clean; second pass has a HIGH finding => capability drops, not gains.
        co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        rep = co.analyze(
            _result([Finding("s", "err", Severity.HIGH, "l", "d")], [LensType.CORRECTNESS]),
            {"domain": "x"},
            [],
        )
        # No gain because capability went DOWN, not up.
        assert not rep.capability_delta


class TestApplyProposals:
    def test_default_placeholder_is_proposed_not_active(self):
        co = VerificationCoevolution(capability_margin=0.10)
        co.analyze(_result([Finding("s", "err", Severity.LOW, "l", "d")], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        rep = co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        checks: list[dict] = []
        added = co.apply_proposals(rep, checks)
        assert len(added) == 1
        assert added[0]["status"] == "proposed"
        assert added[0]["covers"] == ["correctness"]
        assert "added check covering 'correctness'" in rep.actions_applied

    def test_strategy_customizes_check(self):
        co = VerificationCoevolution(capability_margin=0.10)
        co.analyze(_result([Finding("s", "err", Severity.LOW, "l", "d")], [LensType.SECURITY]), {"domain": "x"}, [])
        rep = co.analyze(_result([], [LensType.SECURITY]), {"domain": "x"}, [])
        strategy = lambda prop: {  # noqa: E731
            "covers": prop["spec"]["covers"],
            "status": "active",
            "kind": "acceptance_check",
            "body": f"def test_{prop['dimension']}(): assert True",
        }
        checks: list[dict] = []
        added = co.apply_proposals(rep, checks, strategy=strategy)
        assert added[0]["status"] == "active"
        assert "def test_security" in added[0]["body"]

    def test_strategy_failure_falls_back_to_placeholder(self):
        co = VerificationCoevolution(capability_margin=0.10)
        co.analyze(_result([Finding("s", "err", Severity.LOW, "l", "d")], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        rep = co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        def boom(prop):
            raise RuntimeError("llm down")
        checks: list[dict] = []
        added = co.apply_proposals(rep, checks, strategy=boom)
        assert added[0]["status"] == "proposed"  # safe fallback


class TestReviewWithCoevolutionHook:
    def test_attaches_report(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        res = review_with_coevolution(
            reviewer,
            output="def f(): return 1",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert res.coevolution is not None
        assert isinstance(res.coevolution, CoevolutionReport)
        assert res._lens_trace  # trace captured for coverage analysis

    def test_report_surfaces_in_to_dict(self):
        mock_fn = MagicMock(return_value='{"findings": []}')
        reviewer = AdversarialReviewer(call_model_fn=mock_fn)
        res = review_with_coevolution(
            reviewer,
            output="code",
            task={"title": "T", "body": "B", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        d = res.to_dict()
        assert "coevolution" in d
        assert d["coevolution"]["triggered"] is False


class TestCapabilityWindow:
    def test_windowed_baseline(self):
        co = VerificationCoevolution(capability_margin=0.10, window=3)
        # Seed a low baseline (LOW finding) of 3 samples at 0.9 capability.
        low = [Finding("s", "err", Severity.LOW, "l", "d")]
        for _ in range(3):
            co.analyze(_result(low, [LensType.CORRECTNESS]), {"domain": "x"}, [])
        # Now jump to perfect (1.0): a real 0.10 gain => should trigger.
        rep = co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        assert rep.capability_delta  # genuine jump is detected

    def test_window_does_not_trigger_on_noise(self):
        co = VerificationCoevolution(capability_margin=0.10, window=3)
        # Four identical perfect passes: no gain, no trigger.
        for _ in range(4):
            rep = co.analyze(_result([], [LensType.CORRECTNESS]), {"domain": "x"}, [])
        assert not rep.capability_delta
