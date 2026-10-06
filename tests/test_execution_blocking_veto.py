"""Issue #139: execution failures must veto and be visible in the score.

Policy:
- not_executable / syntax_error / runtime_failure veto acceptance and cap
  the combined score at 40.
- A runtime failure that looks like missing context (ImportError,
  ModuleNotFoundError, EOFError) stays advisory: no veto, no cap.
- A timeout caps the score but does not veto (it may be a snippet blocked
  on input(); revisit once a real-input driver exists).
- An unresolved redaction placeholder used as a bare name
  (``[IP_REDACTED]``) is not executable; the same text in a string, or a
  name the code defines itself, is fine.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adversarial_reviewer import ReviewResult, Verdict
from bookbag import write_bookbag
from director import _is_missing_context_failure, _run_two_judge_review
from orca_executor import CodeExtractor, ExecutionResult

_REPO = "branben/sound-royale-ny"


class _PassReviewer:
    def __init__(self, call_model_fn=None):
        pass

    def review(self, **kwargs):
        return ReviewResult(verdict=Verdict.PASS, findings=[])


def _orca(exit_code=0, timed_out=False, stderr=""):
    class _Orca:
        def execute(self, code, **kwargs):
            reason = CodeExtractor.check_executable(code)
            if reason is not None:
                return ExecutionResult(
                    stdout="", stderr=reason, exit_code=1, timed_out=False,
                    duration_ms=0, not_executable=True, not_executable_reason=reason,
                )
            return ExecutionResult(
                stdout="", stderr=stderr, exit_code=exit_code,
                timed_out=timed_out, duration_ms=10, not_executable=False,
            )
    return _Orca


def _review(monkeypatch, tmp_path, output, bead, orca):
    write_bookbag(bead, student="coder", domain="python-testing",
                  difficulty="medium", task="implement a function",
                  output=output, repo=_REPO)
    monkeypatch.setattr("director._resolve_repo_path",
                        lambda repo, explicit_path=None: tmp_path)
    monkeypatch.setattr("director.run_verify_gate",
                        lambda repo_path, project_verify=None, **kw: {
                            "passed": True, "failures": [], "ran": 1})
    with (
        patch("director.AdversarialReviewer", _PassReviewer),
        patch("director.OrcaExecutionManager", orca),
        patch("director.call_model", side_effect=RuntimeError("no model")),
    ):
        return _run_two_judge_review(
            bead=bead, output=output,
            task={"domain": "python-testing", "difficulty": "medium"},
            repo=_REPO,
        )


GOOD = "```python\nprint('ok')\n```"


def test_runtime_failure_vetoes_and_caps_score(monkeypatch, tmp_path):
    r = _review(monkeypatch, tmp_path, GOOD, "t139-rt",
                _orca(exit_code=1, stderr="ZeroDivisionError: division by zero"))
    assert r["accepted"] is False
    assert r["combined_score"] <= 40.0


@pytest.mark.parametrize("stderr", [
    "ModuleNotFoundError: No module named 'school_core'",
    "ImportError: cannot import name 'helper' from 'utils'",
    "EOFError: EOF when reading a line",
])
def test_missing_context_failure_is_advisory(monkeypatch, tmp_path, stderr):
    r = _review(monkeypatch, tmp_path, GOOD, "t139-ctx",
                _orca(exit_code=1, stderr=f"Traceback (most recent call last):\n{stderr}"))
    assert r["accepted"] is True
    assert r["combined_score"] == 100.0


def test_timeout_caps_score_without_veto(monkeypatch, tmp_path):
    r = _review(monkeypatch, tmp_path, GOOD, "t139-to", _orca(timed_out=True))
    assert r["accepted"] is True
    assert r["combined_score"] <= 40.0


def test_clean_run_still_accepted(monkeypatch, tmp_path):
    r = _review(monkeypatch, tmp_path, GOOD, "t139-ok", _orca())
    assert r["accepted"] is True
    assert r["combined_score"] == 100.0


def test_missing_context_detector():
    assert _is_missing_context_failure("ModuleNotFoundError: No module named 'x'")
    assert _is_missing_context_failure("EOFError: EOF when reading a line")
    assert not _is_missing_context_failure("NameError: name 'x' is not defined")
    assert not _is_missing_context_failure("")


def test_redaction_placeholder_is_not_executable():
    code = "s = 'abc'\nprint(s[[IP_REDACTED]-1])\n"
    reason = CodeExtractor.check_executable(code)
    assert reason is not None and "placeholder" in reason.lower()


def test_redaction_text_in_string_is_fine():
    assert CodeExtractor.check_executable("print('[IP_REDACTED]')\n") is None


def test_self_defined_redacted_name_is_fine():
    code = "FIELD_REDACTED = '***'\nprint(FIELD_REDACTED)\n"
    assert CodeExtractor.check_executable(code) is None


def test_placeholder_submission_vetoed(monkeypatch, tmp_path):
    out = "```python\ns = 'abc'\nprint(s[[IP_REDACTED]-1])\n```"
    r = _review(monkeypatch, tmp_path, out, "t139-ph", _orca())
    assert r["accepted"] is False
