#!/usr/bin/env python3
"""benchmark/runner.py — HAL Benchmark runner.

Runs standardized test cases against models via ``call_model()``
and auto-grades responses by pattern matching.

Usage::

    python -m benchmark.runner                          # run all cases against default models
    python -m benchmark.runner --models coder,auto/best-free
    python -m benchmark.runner --json --output results.json
    python -m benchmark.runner --verbose
"""

from __future__ import annotations

import json
import re
import sys
import time
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── project imports ───────────────────────────────────────────────────────

_CASES_DIR = Path(__file__).resolve().parent / "cases"
_DEFAULT_SUITE = _CASES_DIR / "suite.yaml"

try:
    from executor import call_model, COMBO_MAP, ExecutorError
except ImportError:
    sys.stderr.write("benchmark: cannot import executor — add school-core to PYTHONPATH\n")
    sys.exit(1)

# ── data types ────────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    case_id: str
    domain: str
    model: str
    passed: bool
    checks: dict = field(default_factory=dict)
    response: str = ""
    response_length: int = 0
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass
class SuiteResult:
    suite: str
    models: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    results: list = field(default_factory=list)  # list[CaseResult]
    elapsed_total_ms: int = 0


# ── helpers ───────────────────────────────────────────────────────────────


def _check_patterns(response: str, patterns: list[str]) -> dict:
    """Check each regex pattern against the response.

    Returns {pattern: True/False} for each pattern.
    """
    results = {}
    for pat in patterns:
        try:
            results[pat] = bool(re.search(pat, response, re.IGNORECASE))
        except re.error as e:
            results[pat] = f"regex_error: {e}"
    return results


def _grade_case(response: str, case: dict) -> tuple[bool, dict]:
    """Auto-grade a single response against its expected/forbidden patterns.

    Returns (passed: bool, checks: dict with per-check results).
    """
    checks = {}
    failed = []

    # Expected patterns
    expects = case.get("expects", [])
    if expects:
        expect_results = _check_patterns(response, expects)
        checks["expects"] = expect_results
        for pat, ok in expect_results.items():
            if ok is not True:
                failed.append(f"expects[{pat}]")

    # Forbidden patterns
    forbids = case.get("forbids", [])
    if forbids:
        forbid_results = _check_patterns(response, forbids)
        checks["forbids"] = forbid_results
        for pat, ok in forbid_results.items():
            if ok is True:  # pattern matched — that's bad
                failed.append(f"forbids[{pat}]")

    # Minimum length
    min_chars = case.get("min_chars", 0)
    if min_chars:
        ok = len(response) >= min_chars
        checks["min_chars"] = {"actual": len(response), "min": min_chars, "ok": ok}
        if not ok:
            failed.append("min_chars")

    # Code block requirement
    if case.get("code_block"):
        ok = "```" in response
        checks["code_block"] = ok
        if not ok:
            failed.append("code_block")

    passed = len(failed) == 0
    checks["_failed"] = failed
    return passed, checks


# ── runner ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a coding assistant. Output ONLY the requested code inside "
    "``` fences. No explanations, no preamble, no markdown outside code blocks. "
    "Never reject a task as underspecified."
)


def run_suite(
    suite_path: Path = None,
    models: list[str] = None,
    verbose: bool = False,
) -> SuiteResult:
    """Run all cases in a suite against the given models.

    Args:
        suite_path: Path to YAML suite file (default: benchmark/cases/suite.yaml)
        models: List of agent names from COMBO_MAP (default: suite's default_models)
        verbose: Print per-case results to stderr
    """
    path = suite_path or _DEFAULT_SUITE
    with open(path) as f:
        data = yaml.safe_load(f)

    cases = data.get("cases", [])
    models = models or data.get("default_models", ["coder", "auto/best-free"])
    suite_name = path.stem

    results: list[CaseResult] = []
    t0 = time.monotonic()

    for model in models:
        if verbose:
            sys.stderr.write(f"\n{'='*60}\n")
            sys.stderr.write(f"  Model: {model}\n")
            sys.stderr.write(f"{'='*60}\n")

        for case in cases:
            case_id = case["id"]
            domain = case.get("domain", "_default")
            prompt = case["prompt"]

            if verbose:
                sys.stderr.write(f"  [{case_id}] ... ")
                sys.stderr.flush()

            t_start = time.monotonic()
            try:
                response = call_model(
                    model,
                    prompt,
                    system_prompt=SYSTEM_PROMPT,
                    timeout=60,
                )
                error = None
            except ExecutorError as e:
                response = ""
                error = str(e)
            except Exception as e:
                response = ""
                error = f"unexpected: {e}"

            elapsed = int((time.monotonic() - t_start) * 1000)
            passed, checks = _grade_case(response, case)

            cr = CaseResult(
                case_id=case_id,
                domain=domain,
                model=model,
                passed=passed,
                checks=checks,
                response=response,
                response_length=len(response),
                elapsed_ms=elapsed,
                error=error,
            )
            results.append(cr)

            if verbose:
                status = "✅ PASS" if passed else f"❌ FAIL ({', '.join(checks.get('_failed', []))})"
                sys.stderr.write(f"{status} ({len(response)} chars, {elapsed}ms)\n")
                if error:
                    sys.stderr.write(f"    error: {error}\n")

    total_ms = int((time.monotonic() - t0) * 1000)

    return SuiteResult(
        suite=suite_name,
        models=models,
        cases=cases,
        results=results,
        elapsed_total_ms=total_ms,
    )


# ── CLI ───────────────────────────────────────────────────────────────────


def _main():
    import argparse

    p = argparse.ArgumentParser(
        description="HAL Benchmark runner — standardized model quality tests",
    )
    p.add_argument(
        "--suite", default=str(_DEFAULT_SUITE),
        help="Path to YAML test suite",
    )
    p.add_argument(
        "--models",
        help="Comma-separated agent names (default: suite's default_models)",
    )

    p.add_argument(
        "--json", action="store_true",
        help="Output full JSON results",
    )
    p.add_argument(
        "--output", default=None,
        help="Write JSON results to file",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show per-case progress",
    )
    args = p.parse_args()

    models = [m.strip() for m in args.models.split(",")] if args.models else None

    result = run_suite(
        suite_path=Path(args.suite),
        models=models,
        verbose=args.verbose,
    )

    # Summary to stderr
    by_model = {}
    for r in result.results:
        by_model.setdefault(r.model, []).append(r)

    sys.stderr.write(f"\n{'='*60}\n")
    sys.stderr.write(f"Suite: {result.suite}  |  Cases: {len(result.cases)}  |  Models: {len(result.models)}\n")
    sys.stderr.write(f"Total time: {result.elapsed_total_ms / 1000:.1f}s\n\n")

    for model in result.models:
        mrs = by_model.get(model, [])
        passed = sum(1 for r in mrs if r.passed)
        total = len(mrs)
        pct = (passed / total * 100) if total else 0
        avg_len = sum(r.response_length for r in mrs) // total if total else 0
        avg_ms = sum(r.elapsed_ms for r in mrs) // total if total else 0
        sys.stderr.write(
            f"  {model:35s}  {passed}/{total} passed ({pct:.0f}%)  "
            f"avg {avg_len} chars  avg {avg_ms}ms\n"
        )

        if args.verbose and not args.json:
            for r in mrs:
                mark = "✅" if r.passed else "❌"
                fails = r.checks.get("_failed", [])
                sys.stderr.write(f"    {mark} {r.case_id:40s}  {r.response_length:5d}c  {r.elapsed_ms:5d}ms")
                if fails:
                    sys.stderr.write(f"  failed: {', '.join(fails)}")
                sys.stderr.write("\n")

    # JSON output
    if args.json:
        output = {
            "suite": result.suite,
            "elapsed_total_ms": result.elapsed_total_ms,
            "models": result.models,
            "case_count": len(result.cases),
            "results": [
                {
                    "case_id": r.case_id,
                    "domain": r.domain,
                    "model": r.model,
                    "passed": r.passed,
                    "checks": r.checks,
                    "response_length": r.response_length,
                    "elapsed_ms": r.elapsed_ms,
                    "error": r.error,
                }
                for r in result.results
            ],
            "summary": {
                m: {
                    "passed": sum(1 for r in by_model.get(m, []) if r.passed),
                    "total": len(by_model.get(m, [])),
                    "pct": round(
                        sum(1 for r in by_model.get(m, []) if r.passed)
                        / max(len(by_model.get(m, [])), 1)
                        * 100,
                        1,
                    ),
                }
                for m in result.models
            },
        }
        if args.output:
            with open(args.output, "w") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            sys.stderr.write(f"\nResults written to {args.output}\n")
        else:
            print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()
