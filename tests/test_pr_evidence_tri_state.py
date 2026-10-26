"""The evidence report must distinguish "has not reported" from "failed".

WHY THIS FILE EXISTS
====================

``scripts/pr_evidence.py`` renders one line per check. Every line used to be a
bool, so a CI run that was still ``in_progress`` rendered as ``FAIL``. That is
the same defect class as ``teacher_feedback.py``'s inert ``parse_failed`` gate:
a field that can only ever hold one value reports a fact that is not true.

A PR whose tests have not started yet is not broken. A PR whose tests failed is
broken. Collapsing both into ``False`` makes the report lie in the direction
that gets ignored -- a reviewer who sees ``FAIL`` on every fresh push learns to
skip the report, and then misses the real failure.

The tri-state is:

    True   the command ran and returned success
    False  the command ran and did not
    None   the command has not reported (queued / in progress)

``None`` is deliberately NOT success: ``main()`` exits 1 on it, so an
unfinished run can never gate a merge open. Fail-closed on the exit code,
honest in the text.

``scripts/`` is not a package, so the module is loaded by path. It must be
registered in ``sys.modules`` before ``exec_module`` or ``@dataclass`` fails
under ``from __future__ import annotations`` (dataclasses resolves the string
annotation through ``sys.modules[cls.__module__]``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pr_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pr_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pr_evidence"] = module
    spec.loader.exec_module(module)
    return module


pr_evidence = _load_module()


def _runs(*runs: dict) -> str:
    """Shape the JSON ``gh run list --json ...`` returns."""
    return json.dumps(list(runs))


class TestCheckRender:
    """Three states must render as three different words."""

    def test_pending_renders_pending(self):
        assert pr_evidence.Check("ci", None, "queued").render().endswith(
            "— PENDING — queued"
        )

    def test_pass_renders_pass(self):
        assert "— PASS —" in pr_evidence.Check("ci", True, "success").render()

    def test_fail_renders_fail(self):
        assert "— FAIL —" in pr_evidence.Check("ci", False, "failure").render()

    def test_no_checkboxes_anywhere(self):
        """A ticked box is an assertion; a command's exit code is evidence."""
        for ok in (True, False, None):
            rendered = pr_evidence.Check("ci", ok, "d").render()
            assert "[x]" not in rendered and "[ ]" not in rendered


class TestReportOk:
    """The overall verdict is tri-state too, and pending is not success."""

    def test_all_pass_is_true(self):
        report = pr_evidence.Report(outer=[pr_evidence.Check("a", True, "ok")])
        assert report.ok() is True

    def test_empty_report_is_true(self):
        assert pr_evidence.Report().ok() is True

    def test_pending_is_none_not_false(self):
        """RED before the fix: ``all()`` treats None as falsy."""
        report = pr_evidence.Report(outer=[pr_evidence.Check("ci", None, "queued")])
        assert report.ok() is None, (
            "a pending check collapsed to False, so an unfinished CI run is "
            "reported as a failure -- indistinguishable from a real one"
        )

    def test_failure_beats_pending(self):
        """A real failure must never hide behind a pending sibling."""
        report = pr_evidence.Report(
            outer=[
                pr_evidence.Check("a", False, "failed"),
                pr_evidence.Check("b", None, "queued"),
            ]
        )
        assert report.ok() is False

    def test_pending_in_any_section_is_pending(self):
        for section in ("outer", "inner", "guards"):
            report = pr_evidence.Report(**{section: [pr_evidence.Check("x", None, "q")]})
            assert report.ok() is None, f"{section} pending was not reported as pending"


class TestOuterLoopConclusions:
    """``gh`` reports queued runs with ``conclusion: null`` + a status."""

    def _conclusions(self, monkeypatch, **run):
        monkeypatch.setattr(pr_evidence, "_gh", lambda *a: _runs(run))
        return pr_evidence.outer_loop("abc1234")

    def test_in_progress_is_pending_not_failure(self, monkeypatch):
        """RED before the fix: ``ok = conclusion == "success"`` -> False."""
        checks = self._conclusions(
            monkeypatch,
            workflowName="pytest (py3.11)",
            conclusion=None,
            status="in_progress",
            url="u",
        )
        assert checks[0].ok is None, (
            "an in-progress CI run is reported as a failure; a PR whose tests "
            "have not reported yet reads as broken"
        )

    @pytest.mark.parametrize("status", ["queued", "pending", "waiting", "requested"])
    def test_every_pending_status_is_pending(self, monkeypatch, status):
        checks = self._conclusions(
            monkeypatch,
            workflowName="pytest (py3.12)",
            conclusion=None,
            status=status,
            url="u",
        )
        assert checks[0].ok is None, f"status {status!r} was not treated as pending"

    def test_success_is_true(self, monkeypatch):
        checks = self._conclusions(
            monkeypatch, workflowName="review-gate", conclusion="success", status="completed"
        )
        assert checks[0].ok is True

    def test_failure_is_false(self, monkeypatch):
        checks = self._conclusions(
            monkeypatch, workflowName="review-gate", conclusion="failure", status="completed"
        )
        assert checks[0].ok is False

    def test_cancelled_is_false_not_pending(self, monkeypatch):
        """A cancelled run did report -- and it did not pass."""
        checks = self._conclusions(
            monkeypatch, workflowName="pytest (py3.9)", conclusion="cancelled", status="completed"
        )
        assert checks[0].ok is False

    def test_phantom_check_still_renders_its_reason(self, monkeypatch):
        checks = self._conclusions(
            monkeypatch, workflowName="CircleCI", conclusion="failure", status="completed"
        )
        assert checks[0].ok is False
        assert "can never pass" in checks[0].detail


class TestMainExitCode:
    """The exit code fails closed on pending: a report is not a permission."""

    def _run_main(self, monkeypatch, report):
        monkeypatch.setattr(
            pr_evidence, "build_report", lambda pr: (report, "abc1234", "b")
        )
        monkeypatch.setattr(sys, "argv", ["pr_evidence.py", "161"])
        return pr_evidence.main()

    def test_exit_zero_when_everything_passed(self, monkeypatch):
        report = pr_evidence.Report(outer=[pr_evidence.Check("a", True, "ok")])
        assert self._run_main(monkeypatch, report) == 0

    def test_exit_nonzero_when_pending(self, monkeypatch):
        """PENDING is not PASS. An unfinished run must not read as green."""
        report = pr_evidence.Report(outer=[pr_evidence.Check("a", None, "queued")])
        assert self._run_main(monkeypatch, report) == 1

    def test_exit_nonzero_when_failed(self, monkeypatch):
        report = pr_evidence.Report(outer=[pr_evidence.Check("a", False, "failed")])
        assert self._run_main(monkeypatch, report) == 1
