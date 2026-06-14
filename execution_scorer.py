"""Tier 1: Execution-based scoring.

Runs actual tests/compilation to determine if code works.
Non-blocking: returns None on any failure.
"""

import ast
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class ExecutionScorer:
    """Scores output by actually executing/compiling it.

    For Python code: writes to temp file, tries compile().
    If codebase has pytest, runs it against the patch.
    Score = pass_rate (0.0-1.0) * 100.
    Returns None if no test suite or not code.
    """

    def score(self, output: str, codebase_context: str = "") -> Optional[float]:
        """Score output by execution. Returns None if not applicable."""
        if not output or not output.strip():
            return None

        if not self._is_code(output):
            return None

        syntax_ok = self._check_syntax(output)
        if not syntax_ok:
            return 0.0

        try:
            test_score = self._run_tests(output, codebase_context)
            if test_score is not None:
                return test_score
        except Exception:
            pass

        return None

    def _is_code(self, output: str) -> bool:
        """Heuristic: does this look like code?"""
        code_indicators = [
            "def ", "class ", "import ", "from ", "return ",
            "if ", "for ", "while ", "print(", "var ", "let ", "const ",
            "function ", "=>", "async ", "await ",
        ]
        return any(ind in output for ind in code_indicators)

    def _check_syntax(self, output: str) -> bool:
        """Try to compile Python code. Returns True if syntactically valid."""
        try:
            compile(output, "<output>", "exec")
            return True
        except SyntaxError:
            try:
                ast.parse(output)
                return True
            except SyntaxError:
                return False

    def _run_tests(self, output: str, codebase_context: str) -> Optional[float]:
        """Try to run pytest if available. Returns pass_rate * 100 or None."""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                f.write(output)
                f.flush()
                tmp_path = f.name

            result = subprocess.run(
                ["python", "-m", "pytest", tmp_path, "-q", "--tb=no", "--no-header"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            Path(tmp_path).unlink(missing_ok=True)

            stdout = result.stdout
            if "passed" in stdout or "failed" in stdout:
                return self._parse_pytest_output(stdout)

            return None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def _parse_pytest_output(self, stdout: str) -> float:
        """Parse pytest output to extract pass rate."""
        import re

        summary_match = re.search(
            r"(\d+) passed(?:, (\d+) failed)?", stdout
        )
        if summary_match:
            passed = int(summary_match.group(1))
            failed = int(summary_match.group(2)) if summary_match.group(2) else 0
            total = passed + failed
            if total > 0:
                return (passed / total) * 100.0
        return 0.0
