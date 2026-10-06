"""Hermetic tests proving layer-A snippet execution vetoes non-executable
submissions (SCH-22, child of SCH-21).

A syntax-error or never-called-def submission must yield accepted=false with
a CRITICAL finding, while a genuinely runnable snippet still yields
accepted=true.

These tests are fully hermetic: no Orca terminal, no network, no real
verify gate.  The fake Orca delegates to ``CodeExtractor.check_executable``
so the compile/AST check that the production OrcaExecutionManager.execute()
performs is exercised end-to-end through director.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import director
from director import _run_two_judge_review
from adversarial_reviewer import ReviewResult, Verdict
from bookbag import write_bookbag
from orca_executor import CodeExtractor, ExecutionResult


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeReviewer:
    """Returns unanimous PASS with score 100 so acceptance is decided
    solely by layer-A execution evidence (the thing we're testing)."""

    def __init__(self, call_model_fn=None):
        self.call_model_fn = call_model_fn

    def review(self, **kwargs):
        return ReviewResult(verdict=Verdict.PASS, findings=[])


class _FakeOrcaWithCheck:
    """Fake Orca that mirrors the real execute()'s check_executable pre-guard.

    When the code is not executable it returns an ExecutionResult with
    not_executable=True (just like the real OrcaExecutionManager.execute()).
    Otherwise it returns a clean pass, simulating successful execution.
    """

    def __init__(self):
        pass

    def execute(self, code, **kwargs):
        reason = CodeExtractor.check_executable(code)
        if reason is not None:
            return ExecutionResult(
                stdout="",
                stderr=reason,
                exit_code=1,
                timed_out=False,
                duration_ms=0,
                not_executable=True,
                not_executable_reason=reason,
            )
        return ExecutionResult(
            stdout="ok",
            stderr="",
            exit_code=0,
            timed_out=False,
            duration_ms=100,
            not_executable=False,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

_REPO = "branben/sound-royale-ny"


def _finding(result, issue_class):
    """Grab a single finding by issue_class from the result dict."""
    return next(
        (f for f in result["findings"] if f.get("issue_class") == issue_class),
        None,
    )


def _run_review(monkeypatch, tmp_path, output, bead):
    """Hermetically run _run_two_judge_review with fakes for both judges,
    Orca, and verify gate.  The Orca fake delegates to check_executable."""
    write_bookbag(
        bead,
        student="coder",
        domain="python-testing",
        difficulty="medium",
        task="implement a function",
        output=output,
        repo=_REPO,
    )
    monkeypatch.setattr(
        "director._resolve_repo_path",
        lambda repo, explicit_path=None: tmp_path,
    )
    monkeypatch.setattr(
        "director.run_verify_gate",
        lambda repo_path, project_verify=None, **kwargs: {
            "passed": True, "failures": [], "ran": 1,
        },
    )
    with (
        patch("director.AdversarialReviewer", _FakeReviewer),
        patch("director.OrcaExecutionManager", _FakeOrcaWithCheck),
        patch("director.call_model", side_effect=RuntimeError("no model in tests")),
    ):
        return _run_two_judge_review(
            bead=bead,
            output=output,
            task={"domain": "python-testing", "difficulty": "medium"},
            repo=_REPO,
        )


# ── Unit tests: CodeExtractor.check_executable ─────────────────────────────────


class TestCheckExecutable:
    """Directly test the static check that the real execute() calls."""

    def test_syntax_error_detected(self):
        code = "def broken(x)  \n    return x + 1\n"
        reason = CodeExtractor.check_executable(code)
        assert reason is not None
        assert "Syntax" in reason

    def test_syntax_error_with_em_dash(self):
        """Reproduces the exact failure from trajectory 20260812_230343."""
        code = 'print("hello" \u2014 world)\n'
        reason = CodeExtractor.check_executable(code)
        assert reason is not None
        assert "Syntax" in reason

    def test_never_called_def_detected(self):
        code = "def foo():\n    pass\n"
        reason = CodeExtractor.check_executable(code)
        assert reason is not None
        assert "not executable" in reason.lower()

    def test_only_class_def_detected(self):
        code = "class Foo:\n    pass\n"
        reason = CodeExtractor.check_executable(code)
        assert reason is not None

    def test_only_imports_detected(self):
        code = "import sys\nimport os\n"
        reason = CodeExtractor.check_executable(code)
        assert reason is not None

    def test_docstring_only_detected(self):
        code = '"""Just a docstring."""\n'
        reason = CodeExtractor.check_executable(code)
        assert reason is not None

    def test_empty_code_returns_none(self):
        assert CodeExtractor.check_executable("") is None
        assert CodeExtractor.check_executable("   ") is None

    # ── Genuine executables — must return None ──

    def test_runnable_print_statement(self):
        code = "print('hello')\n"
        assert CodeExtractor.check_executable(code) is None

    def test_runnable_assignment(self):
        code = "x = 1 + 1\nprint(x)\n"
        assert CodeExtractor.check_executable(code) is None

    def test_runnable_def_with_call(self):
        code = "def foo():\n    return 42\nfoo()\n"
        assert CodeExtractor.check_executable(code) is None

    def test_runnable_import_with_usage(self):
        code = "import sys\nprint(sys.version)\n"
        assert CodeExtractor.check_executable(code) is None

    def test_runnable_if_main_block(self):
        code = (
            "def main():\n    print('hi')\n\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        assert CodeExtractor.check_executable(code) is None

    def test_runnable_with_class_and_usage(self):
        code = (
            "class Counter:\n"
            "    def __init__(self):\n"
            "        self.n = 0\n"
            "    def inc(self):\n"
            "        self.n += 1\n"
            "c = Counter()\n"
            "c.inc()\n"
            "print(c.n)\n"
        )
        assert CodeExtractor.check_executable(code) is None


# ── Integration tests: director.py acceptance flow ───────────────────────────


class TestVetoNonExecutable:
    """Prove that non-executable submissions are rejected at the director level
    while runnable code is still accepted."""

    def test_syntax_error_submission_rejected(self, monkeypatch, tmp_path):
        """A syntax-error submission yields accepted=false, CRITICAL."""
        code = "def broken(x)  \n    return x + 1\n"
        output = f"```python\n{code}\n```"
        result = _run_review(monkeypatch, tmp_path, output, "bead-syntax")

        assert result["accepted"] is False
        finding = _finding(result, "not_executable")
        assert finding is not None
        assert finding["severity"] == "CRITICAL"
        assert "Syntax" in finding["description"]

    def test_em_dash_syntax_error_rejected(self, monkeypatch, tmp_path):
        """The exact trajectory evidence: em-dash (U+2014) syntax error."""
        code = 'print("hello" \u2014 world)\n'
        output = f"```python\n{code}\n```"
        result = _run_review(monkeypatch, tmp_path, output, "bead-emdash")

        assert result["accepted"] is False
        finding = _finding(result, "not_executable")
        assert finding is not None
        assert finding["severity"] == "CRITICAL"
        # Issue #139: execution evidence now enters the score. The judges
        # award 100.0, but a blocking execution failure caps it.
        assert result["combined_score"] <= 40.0

    def test_never_called_def_rejected(self, monkeypatch, tmp_path):
        """A never-called def yields accepted=false, CRITICAL."""
        code = "def foo():\n    pass\n"
        output = f"```python\n{code}\n```"
        result = _run_review(monkeypatch, tmp_path, output, "bead-nevercalled")

        assert result["accepted"] is False
        finding = _finding(result, "not_executable")
        assert finding is not None
        assert finding["severity"] == "CRITICAL"

    def test_runnable_code_accepted(self, monkeypatch, tmp_path):
        """A genuinely runnable snippet is still accepted=true."""
        code = "print('hello world')\n"
        output = f"```python\n{code}\n```"
        result = _run_review(monkeypatch, tmp_path, output, "bead-runnable")

        assert result["accepted"] is True
        finding = _finding(result, "not_executable")
        assert finding is None

    def test_runnable_def_with_call_accepted(self, monkeypatch, tmp_path):
        """A function that is actually called still passes."""
        code = (
            "def add(a, b):\n"
            "    return a + b\n"
            "print(add(1, 2))\n"
        )
        output = f"```python\n{code}\n```"
        result = _run_review(monkeypatch, tmp_path, output, "bead-runnable-def")

        assert result["accepted"] is True
        finding = _finding(result, "not_executable")
        assert finding is None

    def test_runnable_exit_nonzero_still_high_not_critical(self, monkeypatch, tmp_path):
        """A runtime error keeps severity HIGH but now vetoes by issue class.

        Issue #139: the veto used to key on CRITICAL only, which the
        execution path never emits for runtime_failure, so a detected
        failure could not block acceptance. Severity is unchanged; the
        class is what blocks. (Missing-context errors such as ImportError
        stay advisory; see tests/test_execution_blocking_veto.py.)
        """
        # Override the fake Orca to return a runtime failure (not not_executable)
        class _FakeOrcaRuntimeError:
            def __init__(self):
                pass

            def execute(self, code, **kwargs):
                return ExecutionResult(
                    stdout="",
                    stderr="NameError: name 'undefined' is not defined",
                    exit_code=1,
                    timed_out=False,
                    duration_ms=50,
                    not_executable=False,
                )

        write_bookbag(
            "bead-runtime",
            student="coder",
            domain="python-testing",
            difficulty="medium",
            task="implement a function",
            output="```python\nundefined_var\n```",
            repo=_REPO,
        )
        monkeypatch.setattr(
            "director._resolve_repo_path",
            lambda repo, explicit_path=None: tmp_path,
        )
        monkeypatch.setattr(
            "director.run_verify_gate",
            lambda repo_path, project_verify=None, **kwargs: {
                "passed": True, "failures": [], "ran": 1,
            },
        )
        with (
            patch("director.AdversarialReviewer", _FakeReviewer),
            patch("director.OrcaExecutionManager", _FakeOrcaRuntimeError),
            patch("director.call_model", side_effect=RuntimeError("no model in tests")),
        ):
            result = _run_two_judge_review(
                bead="bead-runtime",
                output="```python\nundefined_var\n```",
                task={"domain": "python-testing", "difficulty": "medium"},
                repo=_REPO,
            )

        assert result["accepted"] is False  # runtime_failure is a blocking class
        finding = _finding(result, "runtime_failure")
        assert finding is not None
        assert finding["severity"] == "HIGH"
