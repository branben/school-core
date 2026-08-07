"""Tests for benchmark/runner.py + benchmark/report.py — HAL Benchmarks."""

import json
import tempfile
from pathlib import Path

import pytest

# ── _grade_case ────────────────────────────────────────────────────────────

from benchmark.runner import _grade_case, _check_patterns, CaseResult, SuiteResult


class TestCheckPatterns:
    def test_all_match(self):
        result = _check_patterns("Hello world, foo bar", ["Hello", "foo", r"w\w+ld"])
        assert result == {"Hello": True, "foo": True, r"w\w+ld": True}

    def test_partial_match(self):
        result = _check_patterns("Hello world", ["Hello", "MISSING"])
        assert result == {"Hello": True, "MISSING": False}

    def test_invalid_regex(self):
        result = _check_patterns("test", [r"[invalid"])
        assert isinstance(result[r"[invalid"], str)
        assert "regex_error" in result[r"[invalid"]

    def test_empty_patterns(self):
        result = _check_patterns("any", [])
        assert result == {}

    def test_case_insensitive(self):
        result = _check_patterns("HELLO", ["hello"])
        assert result == {"hello": True}


class TestGradeCase:

    def test_all_expects_pass(self):
        case = {"expects": ["def foo", "return"]}
        passed, checks = _grade_case("def foo():\n    return 42", case)
        assert passed is True
        assert checks["expects"] == {"def foo": True, "return": True}
        assert checks["_failed"] == []

    def test_expects_fail(self):
        case = {"expects": ["def foo", "MISSING"]}
        passed, checks = _grade_case("def foo(): pass", case)
        assert passed is False
        assert "expects[MISSING]" in checks["_failed"]

    def test_forbids_pass(self):
        case = {"forbids": ["I cannot", "I don't know"]}
        passed, checks = _grade_case("def foo(): pass", case)
        assert passed is True

    def test_forbids_fail(self):
        case = {"forbids": ["I cannot"]}
        passed, checks = _grade_case("I cannot do this task", case)
        assert passed is False
        assert "forbids[I cannot]" in checks["_failed"]

    def test_min_chars_pass(self):
        case = {"min_chars": 10}
        passed, checks = _grade_case("a" * 20, case)
        assert passed is True
        assert checks["min_chars"]["ok"] is True

    def test_min_chars_fail(self):
        case = {"min_chars": 100}
        passed, checks = _grade_case("short", case)
        assert passed is False
        assert "min_chars" in checks["_failed"]

    def test_code_block_pass(self):
        case = {"code_block": True}
        passed, checks = _grade_case("here is ```python\ncode\n``` now", case)
        assert passed is True

    def test_code_block_fail(self):
        case = {"code_block": True}
        passed, checks = _grade_case("no code fences here", case)
        assert passed is False
        assert "code_block" in checks["_failed"]

    def test_multiple_failures(self):
        case = {
            "expects": ["MISSING1", "MISSING2"],
            "forbids": ["badword"],
            "min_chars": 100,
            "code_block": True,
        }
        passed, checks = _grade_case("badword", case)
        assert passed is False
        assert len(checks["_failed"]) >= 3  # multiple fails

    def test_empty_case_passes(self):
        case = {}
        passed, checks = _grade_case("anything", case)
        assert passed is True
        assert checks["_failed"] == []


# ── CaseResult / SuiteResult ──────────────────────────────────────────────


class TestCaseResult:
    def test_creation(self):
        cr = CaseResult(
            case_id="test-1",
            domain="code-implementation",
            model="coder",
            passed=True,
            response="def foo(): pass",
            response_length=17,
            elapsed_ms=1500,
        )
        assert cr.case_id == "test-1"
        assert cr.passed is True
        assert cr.response_length == 17

    def test_with_error(self):
        cr = CaseResult(
            case_id="err-1",
            domain="_default",
            model="coder",
            passed=False,
            error="Connection refused",
        )
        assert cr.error == "Connection refused"
        assert cr.response == ""


class TestSuiteResult:
    def test_creation(self):
        sr = SuiteResult(
            suite="test_suite",
            models=["coder"],
            cases=[{"id": "c1"}],
            elapsed_total_ms=5000,
        )
        assert sr.suite == "test_suite"
        assert len(sr.cases) == 1

    def test_with_results(self):
        cr = CaseResult(
            case_id="c1", domain="test", model="coder",
            passed=True, elapsed_ms=1000,
        )
        sr = SuiteResult(
            suite="test", models=["coder"], cases=[{"id": "c1"}],
            results=[cr], elapsed_total_ms=2000,
        )
        assert len(sr.results) == 1
        assert sr.results[0].case_id == "c1"


# ── Suite YAML loading ────────────────────────────────────────────────────


class TestSuiteYaml:
    def test_suite_file_exists(self):
        suite_path = Path(__file__).resolve().parent.parent / "benchmark" / "cases" / "suite.yaml"
        assert suite_path.exists(), f"Suite YAML missing: {suite_path}"

    def test_suite_has_cases(self):
        import yaml
        suite_path = Path(__file__).resolve().parent.parent / "benchmark" / "cases" / "suite.yaml"
        with open(suite_path) as f:
            data = yaml.safe_load(f)
        assert "cases" in data
        assert len(data["cases"]) >= 6
        assert "default_models" in data

    def test_all_cases_have_required_fields(self):
        import yaml
        suite_path = Path(__file__).resolve().parent.parent / "benchmark" / "cases" / "suite.yaml"
        with open(suite_path) as f:
            data = yaml.safe_load(f)
        for case in data["cases"]:
            assert "id" in case, f"Case missing 'id': {case}"
            assert "domain" in case, f"Case {case.get('id')} missing 'domain'"
            assert "prompt" in case, f"Case {case['id']} missing 'prompt'"
            assert "expects" in case, f"Case {case['id']} missing 'expects'"

    def test_case_ids_are_unique(self):
        import yaml
        suite_path = Path(__file__).resolve().parent.parent / "benchmark" / "cases" / "suite.yaml"
        with open(suite_path) as f:
            data = yaml.safe_load(f)
        ids = [c["id"] for c in data["cases"]]
        assert len(ids) == len(set(ids)), f"Duplicate case IDs: {ids}"


# ── Report generator ──────────────────────────────────────────────────────


class TestReport:
    def test_report_from_mock_data(self):
        from benchmark.report import report

        mock_data = {
            "suite": "test_suite",
            "models": ["coder", "auto/best-free"],
            "case_count": 2,
            "elapsed_total_ms": 3000,
            "results": [
                {
                    "case_id": "extract-constant",
                    "domain": "code-implementation",
                    "model": "coder",
                    "passed": True,
                    "checks": {"_failed": []},
                    "response_length": 250,
                    "elapsed_ms": 1500,
                    "error": None,
                },
                {
                    "case_id": "extract-constant",
                    "domain": "code-implementation",
                    "model": "auto/best-free",
                    "passed": False,
                    "checks": {"_failed": ["expects[MAX_SPECTATORS]"]},
                    "response_length": 80,
                    "elapsed_ms": 2000,
                    "error": None,
                },
                {
                    "case_id": "write-test",
                    "domain": "python-testing",
                    "model": "coder",
                    "passed": True,
                    "checks": {"_failed": []},
                    "response_length": 400,
                    "elapsed_ms": 1800,
                    "error": None,
                },
                {
                    "case_id": "write-test",
                    "domain": "python-testing",
                    "model": "auto/best-free",
                    "passed": True,
                    "checks": {"_failed": []},
                    "response_length": 350,
                    "elapsed_ms": 1900,
                    "error": None,
                },
            ],
            "summary": {
                "coder": {"passed": 2, "total": 2, "pct": 100.0},
                "auto/best-free": {"passed": 1, "total": 2, "pct": 50.0},
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(mock_data, f)
            tmp_path = f.name

        try:
            output = report(Path(tmp_path))
            assert "coder" in output
            assert "auto/best-free" in output
            assert "PASS" in output
            assert "FAIL" in output
            assert "100%" in output or "50%" in output
        finally:
            Path(tmp_path).unlink()

    def test_report_markdown_mode(self):
        from benchmark.report import report

        mock_data = {
            "suite": "test_suite",
            "models": ["coder"],
            "case_count": 1,
            "elapsed_total_ms": 1000,
            "results": [
                {
                    "case_id": "test-1",
                    "domain": "test",
                    "model": "coder",
                    "passed": True,
                    "checks": {"_failed": []},
                    "response_length": 100,
                    "elapsed_ms": 500,
                    "error": None,
                },
            ],
            "summary": {"coder": {"passed": 1, "total": 1, "pct": 100.0}},
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(mock_data, f)
            tmp_path = f.name

        try:
            output = report(Path(tmp_path), markdown=True)
            assert "## Per-Model Summary" in output
            assert "## Case-by-Case Matrix" in output
            assert "| `coder` |" in output
        finally:
            Path(tmp_path).unlink()

    def test_report_domain_filter(self):
        from benchmark.report import report

        mock_data = {
            "suite": "test_suite",
            "models": ["coder"],
            "case_count": 2,
            "elapsed_total_ms": 1000,
            "results": [
                {
                    "case_id": "ci-1", "domain": "code-implementation",
                    "model": "coder", "passed": True,
                    "checks": {"_failed": []},
                    "response_length": 100, "elapsed_ms": 500,
                    "error": None,
                },
                {
                    "case_id": "pt-1", "domain": "python-testing",
                    "model": "coder", "passed": False,
                    "checks": {"_failed": ["expects[x]"]},
                    "response_length": 50, "elapsed_ms": 600,
                    "error": None,
                },
            ],
            "summary": {"coder": {"passed": 1, "total": 2, "pct": 50.0}},
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(mock_data, f)
            tmp_path = f.name

        try:
            output = report(Path(tmp_path), domain="code-implementation")
            assert "PASS" in output
            assert "FAIL" not in output  # only code-implementation has the passing case
        finally:
            Path(tmp_path).unlink()
