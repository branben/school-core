"""Test the Orca execution sandbox with real task prompts.

These tests validate the full Orca execution pipeline:
  1. Orca terminal creation in shared worktree
  2. Code extraction from LLM-style responses
  3. Code execution via Orca terminal
  4. Exit code, stdout, stderr capture
  5. Terminal cleanup

Unlike the conductor loop tasks (which test the LLM), these tests
directly test the OrcaExecutionManager with known inputs, so we
can validate the sandbox independently of model quality.

Usage:
    # Orca must be running (orca open)
    cd school-core && python -m pytest tests/test_orca_execution.py -v

Environment:
    These tests require Orca to be running. If Orca is not available,
    tests will fail with OrcaUnavailableError — this is intentional
    (hard fail behavior).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orca_executor import (
    OrcaExecutionManager,
    CodeExtractor,
    ExecutionResult,
    OrcaUnavailableError,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Live-Orca tests create a real OrcaExecutionManager and shell out to the Orca
# CLI / git worktree. They are skipped by default; opt in with ORCA_LIVE_TESTS=1.
skip_without_orca = pytest.mark.skipif(
    os.environ.get("ORCA_LIVE_TESTS") != "1",
    reason="set ORCA_LIVE_TESTS=1 to run live-Orca integration tests",
)


@pytest.fixture
def track_cleanup(manager):
    """Track beads created during a test and clean them up after.

    Usage:
        def test_foo(manager, track_cleanup):
            result = manager.execute(code=..., bead=track_cleanup("my-bead"))
            # Even if the next assertion fails, the bead gets cleaned up.
    """
    beads = []

    def _track(bead: str) -> str:
        beads.append(bead)
        return bead

    yield _track

    for b in beads:
        try:
            manager.cleanup_tempspace(b)
        except Exception:
            pass


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def manager():
    """Create an OrcaExecutionManager once per test module.

    This tests the full init flow:
      - Orca runtime check (orca status)
      - Shared worktree creation (orca worktree create)
    """
    m = OrcaExecutionManager()
    yield m


# ── Code Extraction Tests ─────────────────────────────────────────────────────


class TestCodeExtraction:
    """Verify CodeExtractor handles real LLM response formats."""

    def test_strips_python_fences(self):
        """A coder writing a utility function wraps it in ```python."""
        llm_response = '''Here's a function to parse CSV lines:

```python
def parse_csv_line(line: str, delimiter: str = ",") -> dict:
    \"\"\"Parse a comma-separated line into a dict.\"\"\"
    parts = line.strip().split(delimiter)
    return {
        "name": parts[0],
        "value": parts[1] if len(parts) > 1 else "",
        "count": int(parts[2]) if len(parts) > 2 else 0,
    }
```

This handles edge cases like missing fields.'''
        code = CodeExtractor.extract(llm_response, language="python")
        assert "def parse_csv_line" in code
        assert "split(delimiter)" in code
        assert "Hello" not in code  # no explanatory prose
        assert "Here's" not in code
        assert code.startswith("def parse_csv_line"), \
            "Should start with code, not prose"

    def test_strips_any_fences_when_no_language_match(self):
        """Some models omit the language tag — should still extract."""
        llm_response = '''```
def merge_sorted(a, b):
    result = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] < b[j]:
            result.append(a[i])
            i += 1
        else:
            result.append(b[j])
            j += 1
    result.extend(a[i:])
    result.extend(b[j:])
    return result
```'''
        code = CodeExtractor.extract(llm_response)
        assert "def merge_sorted" in code
        assert "result.append" in code

    def test_returns_raw_text_when_no_fences(self):
        """If no fences found, return the response as-is."""
        response = "print('hello world')"
        code = CodeExtractor.extract(response)
        assert code == "print('hello world')"

    def test_empty_response_returns_empty(self):
        """Empty or whitespace-only responses should return empty."""
        assert CodeExtractor.extract("") == ""
        assert CodeExtractor.extract("   ") == ""

    def test_language_for_domain_mapping(self):
        """Domain → language mapping covers all executable domains."""
        assert CodeExtractor.language_for_domain("python-coding") == "python"
        assert CodeExtractor.language_for_domain("python-testing") == "python"
        assert CodeExtractor.language_for_domain("code-implementation") == "python"
        assert CodeExtractor.language_for_domain("terminal") == "bash"
        assert CodeExtractor.language_for_domain("git-operations") == "bash"
        assert CodeExtractor.language_for_domain("code-review") is None
        assert CodeExtractor.language_for_domain("web-automation") is None

    def test_language_specific_fence_preferred(self):
        """When language is specified, only matching fences are extracted."""
        mixed = '''Here's the Python:
```python
def greet(name):
    return f"Hello, {name}!"
```
And the shell:
```bash
echo "hello"
```'''
        python_code = CodeExtractor.extract(mixed, language="python")
        assert "def greet" in python_code
        assert "echo" not in python_code

        bash_code = CodeExtractor.extract(mixed, language="bash")
        assert "echo" in bash_code
        assert "def greet" not in bash_code


# ── Orca Execution Tests (require running Orca) ──────────────────────────────


@skip_without_orca
class TestOrcaExecution:
    """Test the Orca sandbox with real code that exercises different paths.

    These tests require Orca to be running. They validate:
      - Terminal creation and cleanup
      - Code execution and output capture
      - Exit code detection
      - Timeout behavior
      - stdout/stderr parsing

    Skipped unless ORCA_LIVE_TESTS=1 (they spin up a real OrcaExecutionManager).
    """

    def test_execute_successful_code(self, manager, track_cleanup):
        """A real task: write a function that processes data with I/O.

        The function reads numbers from a string, filters evens, and
        returns the sum. It produces stdout output we can verify.
        """
        code = '''"""Process a string of comma-separated integers:
- Parse the numbers
- Filter to even numbers only
- Return the sum
"""
import sys

def process_numbers(text: str) -> int:
    numbers = [int(x.strip()) for x in text.split(",") if x.strip()]
    evens = [n for n in numbers if n % 2 == 0]
    return sum(evens)

# Test
result = process_numbers("1,2,3,4,5,6,7,8,9,10")
print(f"Sum of evens: {result}")
# Also test edge case: empty
empty_result = process_numbers("")
print(f"Empty input: {empty_result}")
# Test negative numbers
neg_result = process_numbers("-2,-1,0,1,2")
print(f"Mixed negatives: {neg_result}")
'''
        bead = track_cleanup("test-success")
        result = manager.execute(code=code, bead=bead)
        assert result.passed, f"Code should execute successfully: {result.stderr}"
        assert result.exit_code == 0
        assert "Sum of evens: 30" in result.stdout, \
            f"Expected 'Sum of evens: 30' in stdout, got: {result.stdout}"
        assert "Empty input: 0" in result.stdout
        assert "Mixed negatives: 0" in result.stdout  # -2+0+2 = 0
        assert result.duration_ms > 0
        assert not result.timed_out

    def test_execute_code_with_runtime_error(self, manager, track_cleanup):
        """A task with a bug: missing import, wrong function name.

        This validates that runtime errors are captured in stderr
        and the exit_code is non-zero — the CTO should see this
        as a CRITICAL finding.
        """
        code = '''"""Try to read a config file without the proper module."""

def load_config(path):
    # BUG: json is not imported!
    with open(path) as f:
        return json.load(f)

# This will fail because json isn't imported
result = load_config("/tmp/nonexistent.json")
print(f"Config: {result}")
'''
        bead = track_cleanup("test-runtime-error")
        result = manager.execute(code=code, bead=bead)
        assert not result.passed, "Buggy code should NOT pass execution"
        assert result.exit_code != 0, \
            f"Expected non-zero exit code, got {result.exit_code}"
        assert "NameError" in result.stderr or "json" in result.stderr, \
            f"Expected NameError in stderr, got: {result.stderr}"

    def test_execute_code_with_syntax_error(self, manager, track_cleanup):
        """A task with a syntax error: missing colon.

        This validates that Python's SyntaxError is captured
        before the code even runs.
        """
        code = '''"""Missing colon after function definition."""
def broken_function(x)  # <-- missing colon
    return x + 1

print(broken_function(5))
'''
        bead = track_cleanup("test-syntax-error")
        result = manager.execute(code=code, bead=bead)
        assert not result.passed, "Syntax error should NOT pass execution"
        assert result.exit_code != 0
        assert "SyntaxError" in result.stderr, \
            f"Expected SyntaxError in stderr, got: {result.stderr}"

    def test_execute_code_with_timeout(self, manager, track_cleanup):
        """A task with an infinite loop — should be killed by timeout.

        This validates the timeout mechanism works. We use a short
        timeout (3s) so the test doesn't take too long.
        """
        code = '''"""Simulate an infinite loop."""
import time
while True:
    time.sleep(1)
    print("still running...")
'''
        bead = track_cleanup("test-timeout")
        start = time.monotonic()
        result = manager.execute(code=code, bead=bead, timeout_ms=3000)
        elapsed = time.monotonic() - start

        assert result.timed_out, "Code should have timed out"
        assert elapsed < 10, f"Timeout took too long: {elapsed:.1f}s"
        # Don't check exit_code — timed-out tasks may not have one

    def test_execute_empty_code_doesnt_crash(self, manager, track_cleanup):
        """Empty code should not crash the executor."""
        bead = track_cleanup("test-empty")
        result = manager.execute(code="", bead=bead)
        # Empty code might exit with code 0, might produce no output
        # The important thing is it doesn't raise an exception
        assert result is not None

    def test_multiple_executions_use_separate_terminals(self, manager, track_cleanup):
        """Running two tasks should use different terminals.

        This validates that state doesn't leak between tasks.
        Previous variables should not be available in a new terminal.
        """
        code_a = '''x = 42
print(f"x = {x}")
'''
        code_b = '''# This should fail because 'x' is from a previous terminal
# if terminals were shared — but they shouldn't be.
try:
    print(x)
except NameError as e:
    print(f"Correctly isolated: {e}")
'''

        bead_a = track_cleanup("test-isolation-a")
        bead_b = track_cleanup("test-isolation-b")
        result_a = manager.execute(code=code_a, bead=bead_a)
        assert result_a.passed
        assert "x = 42" in result_a.stdout

        result_b = manager.execute(code=code_b, bead=bead_b)
        assert result_b.passed, \
            f"Second task should handle NameError gracefully: {result_b.stderr}"
        assert "Correctly isolated" in result_b.stdout, \
            f"Expected isolation message, got: {result_b.stdout}"
        assert not result_b.timed_out

    def test_stdout_with_multiline_output(self, manager, track_cleanup):
        """Real task: generate a Markdown-style report.

        Validates that stdout captures multiple lines correctly.
        """
        code = '''"""Generate a summary report of test results."""
data = {
    "tests_passed": 15,
    "tests_failed": 2,
    "tests_skipped": 1,
}

print("# Test Results Report")
print(f"Total: {data['tests_passed'] + data['tests_failed'] + data['tests_skipped']}")
print(f"Passed: {data['tests_passed']}")
print(f"Failed: {data['tests_failed']}")
print(f"Skipped: {data['tests_skipped']}")
print(f"Pass rate: {data['tests_passed'] / (data['tests_passed'] + data['tests_failed']) * 100:.0f}%")
'''
        bead = track_cleanup("test-multiline")
        result = manager.execute(code=code, bead=bead)
        assert result.passed
        assert "Test Results Report" in result.stdout
        assert "Pass rate: 88%" in result.stdout
        assert len(result.stdout.splitlines()) == 6, \
            f"Expected 6 lines of output (6 print statements), got {len(result.stdout.splitlines())}"


# ── ExecutionResult Tests ─────────────────────────────────────────────────────


class TestExecutionResult:
    """Verify ExecutionResult helper properties work correctly."""

    def test_passed_when_exit_code_zero(self):
        r = ExecutionResult(
            stdout="ok", stderr="",
            exit_code=0, timed_out=False, duration_ms=100,
        )
        assert r.passed

    def test_not_passed_when_exit_code_nonzero(self):
        r = ExecutionResult(
            stdout="", stderr="error",
            exit_code=1, timed_out=False, duration_ms=100,
        )
        assert not r.passed

    def test_not_passed_when_timed_out(self):
        r = ExecutionResult(
            stdout="", stderr="timed out",
            exit_code=None, timed_out=True, duration_ms=30000,
        )
        assert not r.passed

    def test_not_passed_when_error_present(self):
        r = ExecutionResult(
            stdout="", stderr="",
            exit_code=0, timed_out=False, duration_ms=100,
            error="orca crashed",
        )
        assert not r.passed


# ── Conductor Integration Test ────────────────────────────────────────────────


class TestConductorOrcaFlow:
    """End-to-end test: run a task through the conductor with Orca execution.

    This validates the full pipeline:
      conductor.run_conductor() → director.run_task() → call_model()
      → bookbag → Orca execution → CTO+COO review → score update

    Requires:
      - Orca running
      - OmniRoute API reachable (for call_model)
    """

    @pytest.mark.skipif(
        not os.environ.get("OMNIROUTE_API_KEY", "") or os.environ.get("ORCA_DISABLED"),
        reason="OMNIROUTE_API_KEY missing or Orca disabled — skipping integration test",
    )
    def test_coder_produces_executable_code(self):
        """Real task: write a utility function that processes data.

        The coder should produce Python code in ``` fences.
        Orca should execute it and verify it works.
        The review should see the execution evidence.
        """
        from leaf import run_leaf
        from scoring import ScoreStore

        store = ScoreStore()
        task = (
            "Write a function `find_common(lst1, lst2)` that returns "
            "the common elements between two lists without using set(). "
            "Include a test at the bottom that calls find_common with "
            "lst1=[1,2,3,4,5] and lst2=[3,4,5,6,7] and prints the result. "
            "Just the code, no explanation."
        )

        result = run_leaf(
            task_prompt=task,
            role="coder",
            domain="python-coding",
            difficulty="easy",
            store=store,
        )

        assert result["status"] == "success", \
            f"Task should succeed: {result.get('error')}"

        # Check that Orca execution findings are present
        review = result.get("review", {})
        findings = review.get("findings", [])
        orca_findings = [f for f in findings if f.get("section") == "execution"]

        assert len(orca_findings) > 0, \
            "Should have Orca execution findings in the review"

        # At minimum, Orca should have reported execution_passed
        # or runtime_failure — something execution-related
        has_execution_evidence = any(
            f.get("issue_class") in ("execution_passed", "runtime_failure", "no_code_found")
            for f in orca_findings
        )
        assert has_execution_evidence, \
            f"Expected execution evidence in findings, got: {orca_findings}"

        # The bookbag should exist
        assert result.get("bookbag"), "Bookbag should be written to disk"


# ── Worktree Disposal Tests ──────────────────────────────────────────────────


@skip_without_orca
class TestWorktreeDisposal:
    """Test close_worktree() against the native Orca CLI contract.

    The git/rm-rmtree fallback was removed: Orca owns worktree lifecycle and
    the CLI dispose (``orca worktree rm --worktree path:<p> --force``) is the
    single, verified disposal path. close_worktree() retries once on a
    transient failure and returns False (never raises) if both attempts fail.
    """

    @pytest.fixture
    def mgr(self):
        """Create an OrcaExecutionManager (requires Orca running).

        Skip the entire class if Orca is not available.
        """
        try:
            return OrcaExecutionManager()
        except OrcaUnavailableError:
            pytest.skip("Orca not running — skipping disposal tests")

    def test_close_worktree_idempotent_nonexistent(self, mgr):
        """close_worktree() on a non-existent path returns True."""
        result = mgr.close_worktree("/tmp/nonexistent-path-xyz123")
        assert result is True, "Should return True when path doesn't exist"

    def test_close_worktree_orca_succeeds_first_try(self, mgr, monkeypatch, tmp_path):
        """When the Orca CLI succeeds, the path is removed on first attempt."""
        call_count = [0]
        dummy_worktree = tmp_path / "dummy-rm-test-first"

        def mock_run_orca(args, timeout=15):
            call_count[0] += 1
            assert args[0:2] == ["worktree", "rm"]
            assert "--force" in args
            # Simulate successful removal: delete the path
            import shutil
            shutil.rmtree(str(dummy_worktree), ignore_errors=True)
            return {"ok": True}

        dummy_worktree.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)

        result = mgr.close_worktree(str(dummy_worktree))
        assert result is True
        assert call_count[0] == 1, "Should succeed on first attempt"
        assert not dummy_worktree.exists()

    def test_close_worktree_retry_then_succeed(self, mgr, monkeypatch, tmp_path):
        """When Orca fails once but succeeds on retry, removal succeeds."""
        call_count = [0]
        dummy_worktree = tmp_path / "dummy-rm-test-retry"
        dummy_worktree.mkdir(parents=True, exist_ok=True)

        def mock_run_orca(args, timeout=15):
            call_count[0] += 1
            if call_count[0] < 2:
                raise OrcaUnavailableError("Simulated Orca failure")
            import shutil
            shutil.rmtree(str(dummy_worktree), ignore_errors=True)
            return {"ok": True}

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)

        result = mgr.close_worktree(str(dummy_worktree))
        assert result is True
        assert call_count[0] == 2, f"Should retry once, got {call_count[0]}"
        assert not dummy_worktree.exists()

    def test_close_worktree_returns_false_when_both_fail(self, mgr, monkeypatch, tmp_path):
        """If both CLI attempts fail, close_worktree returns False (no raise)."""
        call_count = [0]
        dummy_worktree = tmp_path / "real-dir-both-fail"
        dummy_worktree.mkdir(parents=True, exist_ok=True)

        def mock_run_orca(args, timeout=15):
            call_count[0] += 1
            raise OrcaUnavailableError("Orca down")

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)

        # Use a path that exists so both attempts are attempted
        result = mgr.close_worktree(str(dummy_worktree))
        assert result is False, "Should return False when CLI cannot remove it"
        assert call_count[0] == 2, "Should attempt twice (initial + 1 retry)"
        assert dummy_worktree.exists(), "Mock never removed it"


@skip_without_orca
class TestCreateWorktreeRepoPath:
    """create_worktree() must scope --repo to the target clone for cross-repo dispatch."""

    @pytest.fixture
    def mgr(self):
        try:
            return OrcaExecutionManager()
        except OrcaUnavailableError:
            pytest.skip("Orca not running — skipping create_worktree tests")

    def test_create_worktree_uses_target_repo_path(self, mgr, monkeypatch, tmp_path):
        """Cross-repo: registers missing repo (repo list -> repo add) then scopes --repo."""
        target = tmp_path / "sound-royale-ny-clone"
        calls = []

        def mock_run_orca(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["worktree", "create"]:
                return {"worktree": {"id": f"uuid::{target}"}}
            if args[:2] == ["repo", "list"]:
                # target not yet registered
                return {"repos": []}
            if args[:2] == ["repo", "add"]:
                return {"id": "repo-id-123"}
            return {"id": "repo-id-123"}

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)
        returned = mgr.create_worktree("study-coder-r1", repo_path=target)

        repo_list = [c for c in calls if c[:2] == ["repo", "list"]]
        repo_add = [c for c in calls if c[:2] == ["repo", "add"]]
        wt = [c for c in calls if c[:2] == ["worktree", "create"]]
        assert len(repo_list) == 1, "must check repo list once"
        assert len(repo_add) == 1, "must add missing target repo"
        assert repo_add[0][3] == str(target), "repo add must use --path <target>"
        assert len(wt) == 1, "must create exactly one worktree"
        idx = wt[0].index("--repo")
        assert wt[0][idx + 1] == str(target), "repo_path must scope --repo to target"
        assert str(mgr.REPO_PATH) not in wt[0], "must NOT fall back to REPO_PATH"
        assert returned == str(target), "returned path must be the worktree id path"

    def test_create_worktree_skips_registration_when_already_listed(self, mgr, monkeypatch, tmp_path):
        """Cross-repo but already registered: repo list -> NO repo add (idempotent)."""
        target = tmp_path / "sound-royale-ny-clone"
        calls = []

        def mock_run_orca(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["worktree", "create"]:
                return {"worktree": {"id": f"uuid::{target}"}}
            if args[:2] == ["repo", "list"]:
                # target already known to Orca
                return {"repos": [{"path": str(target), "id": "repo-id-123"}]}
            return {"id": "repo-id-123"}

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)
        mgr.create_worktree("study-coder-r1", repo_path=target)

        assert not [c for c in calls if c[:2] == ["repo", "add"]], \
            "must NOT re-add an already-listed repo"
        wt = next(c for c in calls if c[:2] == ["worktree", "create"])
        assert wt[wt.index("--repo") + 1] == str(target)

    def test_create_worktree_raises_when_registration_fails(self, mgr, monkeypatch, tmp_path):
        """If repo registration fails, surface OrcaUnavailableError (not opaque repo_not_found)."""
        target = tmp_path / "sound-royale-ny-clone"
        calls = []

        def mock_run_orca(args, timeout=30):
            calls.append(list(args))
            if args[:2] == ["repo", "list"]:
                return {"repos": []}
            if args[:2] == ["repo", "add"]:
                # simulate repo add failing
                raise OrcaUnavailableError("repo add failed")
            return {"id": "x"}

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)
        with pytest.raises(OrcaUnavailableError):
            mgr.create_worktree("study-coder-r1", repo_path=target)

    def test_create_worktree_defaults_to_repo_path(self, mgr, monkeypatch):
        """When repo_path is None, --repo falls back to REPO_PATH (single-repo)."""
        calls = []

        def mock_run_orca(args, timeout=30):
            calls.append(list(args))
            return {"worktree": {"id": "uuid::/some/orca/workspace/study-coder-r1"}}

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)
        mgr.create_worktree("study-coder-r1")

        idx = calls[0].index("--repo")
        assert calls[0][idx + 1] == str(mgr.REPO_PATH), "default --repo is REPO_PATH"
