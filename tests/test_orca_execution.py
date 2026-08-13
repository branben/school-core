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
import subprocess
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

    def test_detect_language_returns_none_when_no_repo(self):
        """detect_language returns None when repo_path is None."""
        assert CodeExtractor.detect_language(None) is None

    def test_detect_language_python_project(self, tmp_path):
        """A repo with pyproject.toml is detected as Python."""
        (tmp_path / "pyproject.toml").write_text("[build-system]\n")
        assert CodeExtractor.detect_language(tmp_path) == "python"

    def test_detect_language_rust_project(self, tmp_path):
        """A repo with Cargo.toml is detected as Rust."""
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert CodeExtractor.detect_language(tmp_path) == "rust"

    def test_detect_language_typescript_project(self, tmp_path):
        """A repo with package.json + tsconfig.json is detected as TypeScript."""
        (tmp_path / "package.json").write_text('{"name": "test", "devDependencies": {"typescript": "^5.0"}}')
        assert CodeExtractor.detect_language(tmp_path) == "typescript"

    def test_detect_language_javascript_project(self, tmp_path):
        """A repo with package.json but no TypeScript is detected as JavaScript."""
        (tmp_path / "package.json").write_text('{"name": "test", "dependencies": {"react": "^18.0"}}')
        assert CodeExtractor.detect_language(tmp_path) == "javascript"

    def test_detect_language_unknown_project(self, tmp_path):
        """A repo with no config files returns None."""
        (tmp_path / "README.md").write_text("# My Project")
        assert CodeExtractor.detect_language(tmp_path) is None

    def test_detect_language_takes_rust_over_package_json(self, tmp_path):
        """Cargo.toml takes priority over package.json (Rust projects can have both)."""
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        (tmp_path / "package.json").write_text('{"name": "test"}')
        assert CodeExtractor.detect_language(tmp_path) == "rust"

    def test_language_for_domain_code_impl_with_repo_path(self, tmp_path):
        """code-implementation uses detected language when repo_path is given."""
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert CodeExtractor.language_for_domain("code-implementation", repo_path=tmp_path) == "rust"

    def test_language_for_domain_python_testing_ignores_repo_path(self, tmp_path):
        """python-testing always returns 'python' regardless of repo_path."""
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert CodeExtractor.language_for_domain("python-testing", repo_path=tmp_path) == "python"

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


@pytest.mark.live
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
        # The kill mechanism is asserted above (timed_out). The wall-clock
        # bound only guards against a *completely* broken timeout (hang), so
        # it is generous: the self-hosted runner also serves scheduled
        # school-loop cycles, and Orca terminal create/send/close latency
        # under that contention can exceed 10s (observed 14.6s 2026-08-12
        # while a school-loop was mid-cycle).
        assert elapsed < 60, f"Timeout took too long: {elapsed:.1f}s"
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


@pytest.mark.live
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


@pytest.mark.live
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


@pytest.mark.live
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
            if args[:2] == ["repo", "list"]:
                # target not yet registered
                return {"repos": []}
            if args[:2] == ["repo", "add"]:
                return {"id": "repo-id-123"}
            if args[:2] == ["worktree", "create"]:
                return {"worktree": {"id": "uuid::/some/orca/workspace/study-coder-r1"}}
            return {"id": "repo-id-123"}

        monkeypatch.setattr(mgr, "_run_orca", mock_run_orca)
        mgr.create_worktree("study-coder-r1")

        wt = next(c for c in calls if c[:2] == ["worktree", "create"])
        idx = wt.index("--repo")
        assert wt[idx + 1] == str(mgr.REPO_PATH), "default --repo is REPO_PATH"


# ── TeacherWorktree.close() Admin Entry Cleanup (Regression for cto-N Sufox Spray) ──


class TestTeacherCloseAdminEntryCleanup:
    """Regression: the ``teacher-cto-N`` suffix-spray origin.

    When the worktree directory is removed externally but the orca-side
    registry + ``<repo>/.git/worktrees/<name>`` admin entry linger, the next
    ``orca worktree create --name <name>`` auto-suffixes to ``<name>-2``
    because git still considers ``<name>`` registered. ``TeacherWorktree.close()``
    runs ``git worktree prune`` to drop the stale admin entry.

    This is a pure-git test (does NOT require Orca running). It builds a real
    git repo at ``tmp_path``, mints a stale admin entry, calls ``close()``
    on a teacher whose ``mgr.REPO_PATH`` points at the tmp repo — so the
    prune step runs against real git — and asserts the entry is gone
    afterwards.
    """

    def test_close_prunes_stale_admin_entry(self, tmp_path):
        """The actual regression scenario.

        1. Boot TWO tmp git repos (main_repo + sentinel_repo), each with
           a worktree that we later remove externally to make the admin
           entry stale.
        2. Construct a ``TeacherWorktree`` with ``_mgr.REPO_PATH=main_repo``
           so ``close()`` exercises ``git worktree prune`` against real git
           for the main_repo only.
        3. Call ``close()``.
        4. Assert ``git -C main_repo worktree list --porcelain`` no longer
           contains the stale entry (the cto-N suffix spray is fixed).
        5. Assert ``git -C sentinel_repo worktree list --porcelain`` STILL
           contains its stale entry — proves the prune targeted the right
           repo (REPO_PATH was honored), not just pruned everywhere.
        """
        from unittest.mock import MagicMock
        from teacher import TeacherWorktree

        main_repo = tmp_path / "main"
        sentinel_repo = tmp_path / "sentinel"
        wt_dir_main = tmp_path / "leaked-cto"
        wt_dir_sentinel = tmp_path / "sentinel-leak"
        main_repo.mkdir()
        sentinel_repo.mkdir()
        wt_dir_main.mkdir()
        wt_dir_sentinel.mkdir()

        self._init_git_repo(main_repo)
        self._init_git_repo(sentinel_repo)

        # Add STALE-EQUIVALENT worktrees to BOTH repos. prune() will only
        # touch main_repo.
        subprocess.run(
            ["git", "worktree", "add", str(wt_dir_main), "HEAD"],
            cwd=str(main_repo), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "worktree", "add", str(wt_dir_sentinel), "HEAD"],
            cwd=str(sentinel_repo), check=True, capture_output=True,
        )

        # Make both admin entries stale (simulate orphaned dirs). prune()
        # will only target the main repo via REPO_PATH.
        import shutil
        shutil.rmtree(str(wt_dir_main))
        shutil.rmtree(str(wt_dir_sentinel))

        # Sanity: both repos consider their entries registered.
        for label, repo, expected in (
            ("main", main_repo, "leaked-cto"),
            ("sentinel", sentinel_repo, "sentinel-leak"),
        ):
            porcelain = subprocess.check_output(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                text=True,
            )
            assert expected in porcelain, (
                f"{label} setup failed; expected {expected!r} in porcelain.\n"
                f"porcelain:\n{porcelain}"
            )

        # Build a TeacherWorktree and stub _mgr so REPO_PATH points at
        # main_repo. Layers 1 + 2 are mocked; Layer 3 runs real git prune.
        teacher = TeacherWorktree("cto")
        teacher._mgr = MagicMock()
        teacher._mgr.REPO_PATH = main_repo  # THE PRUNE TARGET
        teacher._mgr.close_worktree = MagicMock(return_value=True)
        teacher._mgr._run_orca = MagicMock(return_value=None)
        teacher._mgr.close_terminal = MagicMock()
        teacher.worktree_path = str(wt_dir_main)
        teacher._review_terminal = "fake-handle"
        teacher._booted = True

        teacher.close()

        # Layer 1 invoked with the worktree path.
        teacher._mgr.close_worktree.assert_called_once_with(str(wt_dir_main))

        # Layer 2 invoked with the canonical-name selector (corrected flag).
        rm_calls = [
            c.args[0] for c in teacher._mgr._run_orca.call_args_list
            if isinstance(c.args, tuple) and len(c.args) > 0
            and isinstance(c.args[0], list)
            and "remove" in c.args[0]
        ]
        assert rm_calls, "Expected belt-and-suspenders orca worktree rm call"
        assert "--worktree" in rm_calls[0]
        assert "name:teacher-cto" in rm_calls[0]
        assert "--force" in rm_calls[0]

        # REGRESSION: Layer 3 (real git prune) cleared the main_repo's stale
        # entry.
        after_main = subprocess.check_output(
            ["git", "-C", str(main_repo), "worktree", "list", "--porcelain"],
            text=True,
        )
        assert "leaked-cto" not in after_main, (
            f"close() did not prune the stale admin entry in the target "
            f"repo — the cto-N suffix spray will recur on next --serve.\n"
            f"main_repo porcelain after close():\n{after_main}"
        )

        # TARGETED-PROOF: the sentinel repo's entry survived. This proves
        # the prune ran in REPO_PATH, not "git worktree prune" with no
        # scoping that could touch the wrong repo anywhere.
        after_sentinel = subprocess.check_output(
            ["git", "-C", str(sentinel_repo), "worktree", "list", "--porcelain"],
            text=True,
        )
        assert "sentinel-leak" in after_sentinel, (
            f"close() pruned the WRONG repo — the sentinel repo's stale "
            f"entry should still be present (it wasn't REPO_PATH).\n"
            f"sentinel_repo porcelain after close():\n{after_sentinel}"
        )

    def _init_git_repo(self, repo_path):
        """Helper: init a tmp_path repo with a single commit on main."""
        subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo_path), check=True, capture_output=True)
        (repo_path / "README.md").write_text("init\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True, capture_output=True)
