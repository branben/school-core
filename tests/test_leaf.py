"""Unit tests for leaf.py — StudentLeaf lifecycle and run_leaf convenience function.

Tests cover:
  - Construction and auto-generated bead/worktree name
  - boot() with Orca worktree creation
  - write_brief() and write_output() to worktree filesystem
  - run_task() delegation to director
  - signal_ready() via BookbagSignal
  - dispose() and idempotent close
  - Context manager auto-boot and auto-dispose
  - run_leaf() convenience function (full lifecycle)
  - Error paths: Orca unavailable, not booted, task failure

All external dependencies (Orca, director, bookbag) are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leaf import StudentLeaf, run_leaf, LeafError, LeafNotBootedError
from orca_executor import OrcaUnavailableError
from bookbag import HANDOFF_TIMEOUT


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_mgr():
    """Mock OrcaExecutionManager."""
    with patch("leaf.OrcaExecutionManager") as mock:
        instance = MagicMock()
        instance.create_worktree.return_value = "/tmp/leaf-worktrees/study-coder-abc123"
        mock.return_value = instance
        yield instance


@pytest.fixture
def mock_bookbag_signal():
    """Mock BookbagSignal class.

    Yields the patched class mock, not the instance, so tests can
    assert on the class call (``mock_bookbag_signal.assert_called_with``)
    and on the instance (``mock_bookbag_signal.return_value.ready``).
    """
    with patch("leaf.BookbagSignal") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield mock


@pytest.fixture
def mock_director():
    """Mock director.run_task."""
    with patch("leaf.run_task") as mock:
        mock.return_value = {
            "status": "success",
            "domain": "python-coding",
            "difficulty": "easy",
            "agent": "coder",
            "response": "def hello(): pass",
            "review": {
                "cto_verdict": "PASS",
                "coo_verdict": "PASS",
                "cto_score": 85.0,
                "coo_score": 78.0,
                "findings": [],
                "accepted": True,
            },
            "old_score": 50.0,
            "new_score": 60.0,
            "task_score": 75.0,
            "bead": "coder-python-coding-abc123",
        }
        yield mock


@pytest.fixture
def mock_wait_for_verdicts():
    """Mock wait_for_verdicts for async handoff tests."""
    with patch("leaf.wait_for_verdicts") as mock:
        mock.return_value = ("PASS", "PASS")
        yield mock


@pytest.fixture
def mock_store():
    """Mock ScoreStore."""
    with patch("leaf.ScoreStore") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield instance


# ── Construction Tests ───────────────────────────────────────────────────────


class TestConstruction:
    """StudentLeaf construction and auto-generated identifiers."""

    def test_auto_generates_bead(self):
        """Bead should follow {role}-{domain}-{rand8} pattern."""
        leaf = StudentLeaf("coder", "python-coding")
        parts = leaf.bead.split("-")
        assert parts[0] == "coder"
        assert parts[1] == "python"
        assert parts[2] == "coding"
        assert len(parts[3]) == 8  # rand8 hex

    def test_worktree_name_uses_role_and_rand(self):
        """Worktree name should be study-{role}-{rand8}."""
        leaf = StudentLeaf("coder", "python-coding")
        parts = leaf.worktree_name.split("-")
        assert parts[0] == "study"
        assert parts[1] == "coder"
        assert len(parts[2]) == 8

    def test_different_leaves_have_different_beads(self):
        """Two consecutive leaves should have different beads."""
        leaf1 = StudentLeaf("coder", "python-coding")
        leaf2 = StudentLeaf("coder", "python-coding")
        assert leaf1.bead != leaf2.bead

    def test_default_state(self):
        """New leaf should not be booted."""
        leaf = StudentLeaf("coder", "python-coding")
        assert not leaf._booted
        assert leaf.worktree_path is None
        assert leaf._mgr is None

    def test_custom_difficulty(self):
        """Custom difficulty should be stored."""
        leaf = StudentLeaf("coder", "python-coding", difficulty="hard")
        assert leaf.difficulty == "hard"

    def test_default_handoff_timeout(self):
        """Default handoff timeout should match HANDOFF_TIMEOUT."""
        leaf = StudentLeaf("coder", "python-coding")
        assert leaf.handoff_timeout == HANDOFF_TIMEOUT

    def test_repr(self):
        """__repr__ should show bead, role, and boot status."""
        leaf = StudentLeaf("coder", "python-coding")
        assert "coder" in repr(leaf)
        assert leaf.bead in repr(leaf)
        assert "unbooted" in repr(leaf)

        leaf._booted = True
        assert "booted" in repr(leaf)


# ── Boot Tests ───────────────────────────────────────────────────────────────


class TestBoot:
    """boot() — worktree creation."""

    def test_boot_creates_worktree(self, mock_mgr):
        """boot() should create worktree and set state."""
        leaf = StudentLeaf("coder", "python-coding")
        path = leaf.boot()

        mock_mgr.create_worktree.assert_called_once()
        # Worktree name should include study- prefix and role
        call_args = mock_mgr.create_worktree.call_args[0][0]
        assert call_args.startswith("study-coder-")

        assert path == "/tmp/leaf-worktrees/study-coder-abc123"
        assert leaf.worktree_path == path
        assert leaf._booted
        assert leaf._mgr is not None

    def test_boot_fails_on_orca_error(self, mock_mgr):
        """boot() should raise LeafError when Orca is unavailable."""
        mock_mgr.create_worktree.side_effect = OrcaUnavailableError("Orca not running")

        leaf = StudentLeaf("coder", "python-coding")
        with pytest.raises(LeafError, match="Failed to boot leaf"):
            leaf.boot()

        assert not leaf._booted

    def test_boot_idempotent(self, mock_mgr):
        """Calling boot() twice should not create two worktrees."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()
        leaf.boot()  # Second call sets _mgr again but creates a new worktree

        assert mock_mgr.create_worktree.call_count == 2

    def test_different_role_worktree_names(self, mock_mgr):
        """Different roles should produce different worktree names."""
        leaf1 = StudentLeaf("coder", "python-coding")
        leaf2 = StudentLeaf("searcher", "code-search")

        leaf1.boot()
        leaf2.boot()

        name1 = mock_mgr.create_worktree.call_args_list[0][0][0]
        name2 = mock_mgr.create_worktree.call_args_list[1][0][0]

        assert "coder" in name1
        assert "searcher" in name2
        assert name1 != name2


# ── Brief and Output Tests ───────────────────────────────────────────────────


class TestBriefAndOutput:
    """write_brief() and write_output()."""

    def test_write_brief_requires_boot(self):
        """write_brief() should raise if not booted."""
        leaf = StudentLeaf("coder", "python-coding")
        with pytest.raises(LeafNotBootedError, match="not booted"):
            leaf.write_brief("task")

    def test_write_brief_creates_student_brief(self, mock_mgr):
        """write_brief() should create and write a StudentBrief."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        leaf.write_brief("Write a hello function")

        mock_mgr.write_student_brief.assert_called_once()
        args, kwargs = mock_mgr.write_student_brief.call_args
        path, brief = args
        assert brief.bead == leaf.bead
        assert brief.role == "coder"
        assert brief.domain == "python-coding"
        assert brief.task == "Write a hello function"
        assert brief.difficulty == "easy"

    def test_write_output_requires_boot(self):
        """write_output() should raise if not booted."""
        leaf = StudentLeaf("coder", "python-coding")
        with pytest.raises(LeafNotBootedError, match="not booted"):
            leaf.write_output({"response": "hello"})

    def test_write_output_writes_data(self, mock_mgr):
        """write_output() should write data to worktree."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        data = {"response": "hello", "score": 85}
        leaf.write_output(data)

        mock_mgr.write_student_output.assert_called_once()
        args, kwargs = mock_mgr.write_student_output.call_args
        path, bead, output_data = args
        assert bead == leaf.bead
        assert output_data == data


# ── run_task Tests ───────────────────────────────────────────────────────────


class TestRunTask:
    """run_task() delegation to director."""

    def test_run_task_requires_boot(self):
        """run_task() should raise if not booted."""
        leaf = StudentLeaf("coder", "python-coding")
        with pytest.raises(LeafNotBootedError, match="not booted"):
            leaf.run_task("task prompt")

    def test_run_task_delegates_to_director(self, mock_mgr, mock_director):
        """run_task() should call director.run_task with correct args."""
        leaf = StudentLeaf("coder", "python-coding", difficulty="hard")
        leaf.boot()

        leaf.run_task("Write code")

        mock_director.assert_called_once_with(
            prompt="Write code",
            domain="python-coding",
            difficulty="hard",
            force_agent="coder",
            store=leaf._store,
            skip_review=False,
            repo=leaf._repo_slug(),
            ce_enabled=False,
            complex_task=False,
            dod_gate=False,
            skip_readiness=False,
        )

    def test_run_task_returns_result(self, mock_mgr, mock_director):
        """run_task() should return the director result."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        result = leaf.run_task("Write code")

        assert result["status"] == "success"
        assert result["agent"] == "coder"
        assert result["review"]["cto_verdict"] == "PASS"

    def test_run_task_handles_error(self, mock_mgr, mock_director):
        """run_task() should propagate director errors."""
        mock_director.return_value = {
            "status": "error",
            "error": "Model call failed",
        }

        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        result = leaf.run_task("Write code")
        assert result["status"] == "error"
        assert "Model call failed" in result["error"]


# ── Signal Tests ─────────────────────────────────────────────────────────────


class TestSignal:
    """signal_ready() and wait_for_handoff()."""

    def test_signal_ready_creates_bookbag_signal(self, mock_mgr, mock_bookbag_signal):
        """signal_ready() should create a repo-scoped BookbagSignal and call ready()."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()
        leaf.bead = "test-bead-1234"
        leaf.repo_path = None  # default → __global__ namespace

        leaf.signal_ready()

        # Repo namespace must be threaded so repo-scoped consumers find the flag.
        mock_bookbag_signal.assert_called_once_with("test-bead-1234", repo="__global__")
        mock_bookbag_signal.return_value.ready.assert_called_once()

    def test_wait_for_handoff_calls_wait_for_verdicts(self, mock_mgr, mock_wait_for_verdicts):
        """wait_for_handoff() should delegate to wait_for_verdicts."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.bead = "test-bead-5678"

        cto, coo = leaf.wait_for_handoff(timeout=60)

        mock_wait_for_verdicts.assert_called_once_with(
            "test-bead-5678", repo="__global__", timeout=60
        )
        assert cto == "PASS"
        assert coo == "PASS"

    def test_wait_for_handoff_default_timeout(self, mock_mgr, mock_wait_for_verdicts):
        """wait_for_handoff() should use self.handoff_timeout when no timeout given."""
        leaf = StudentLeaf("coder", "python-coding", handoff_timeout=99)
        leaf.bead = "test-bead-9999"

        leaf.wait_for_handoff()

        mock_wait_for_verdicts.assert_called_once_with(
            "test-bead-9999", repo="__global__", timeout=99
        )

    def test_signal_ready_non_global_repo_scopes_signal(self, mock_mgr, mock_bookbag_signal):
        """signal_ready() must scope the BookbagSignal to a non-global repo."""
        leaf = StudentLeaf("coder", "python-coding", repo="branben/sound-royale-ny")
        leaf.boot()
        leaf.bead = "test-bead-7777"

        leaf.signal_ready()

        mock_bookbag_signal.assert_called_once_with(
            "test-bead-7777", repo="branben/sound-royale-ny"
        )
        mock_bookbag_signal.return_value.ready.assert_called_once()

    def test_wait_for_handoff_passes_repo_slug(self, mock_mgr, mock_wait_for_verdicts):
        """wait_for_handoff() must poll the leaf's repo namespace, not __global__."""
        leaf = StudentLeaf("coder", "python-coding", repo="branben/sound-royale-ny")
        leaf.bead = "test-bead-8888"

        leaf.wait_for_handoff(timeout=42)

        mock_wait_for_verdicts.assert_called_once_with(
            "test-bead-8888", repo="branben/sound-royale-ny", timeout=42
        )


# ── Dispose Tests ────────────────────────────────────────────────────────────


class TestDispose:
    """dispose() — worktree cleanup."""

    def test_dispose_removes_worktree(self, mock_mgr):
        """dispose() should call close_worktree and reset state."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        leaf.dispose()

        mock_mgr.close_worktree.assert_called_once_with("/tmp/leaf-worktrees/study-coder-abc123")
        assert leaf.worktree_path is None
        assert not leaf._booted
        assert leaf._mgr is None

    def test_dispose_idempotent(self, mock_mgr):
        """Calling dispose() twice should not raise."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()
        leaf.dispose()
        leaf.dispose()  # Should not raise

    def test_dispose_on_unbooted_leaf(self):
        """dispose() on an unbooted leaf should not raise."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.dispose()  # Should not raise

    def test_dispose_handles_orca_error(self, mock_mgr):
        """dispose() should not crash if close_worktree fails."""
        mock_mgr.close_worktree.side_effect = Exception("Orca error")

        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        leaf.dispose()  # Should not crash

        # State should still be reset
        assert leaf.worktree_path is None
        assert not leaf._booted


# ── Context Manager Tests ────────────────────────────────────────────────────


class TestContextManager:
    """Context manager — auto-boot and auto-dispose."""

    def test_context_manager_boots_and_disposes(self, mock_mgr, mock_director):
        """'with StudentLeaf()' should boot and dispose."""
        with StudentLeaf("coder", "python-coding") as leaf:
            assert leaf._booted
            assert leaf.worktree_path is not None

        # After exit: disposed
        assert not leaf._booted
        assert leaf.worktree_path is None
        mock_mgr.close_worktree.assert_called_once()

    def test_context_manager_disposes_on_exception(self, mock_mgr, mock_director):
        """Context manager should dispose even when an exception occurs."""
        class TestError(Exception):
            pass

        try:
            with StudentLeaf("coder", "python-coding") as leaf:
                raise TestError("inside context")
        except TestError:
            pass

        # dispose() should still be called
        mock_mgr.close_worktree.assert_called_once()

    def test_context_manager_does_not_reboot(self, mock_mgr, mock_director):
        """Context manager should not call boot() if already booted."""
        leaf = StudentLeaf("coder", "python-coding")
        leaf.boot()

        with leaf:
            # __enter__ sees _booted=True and doesn't call boot() again
            pass

        mock_mgr.create_worktree.assert_called_once()  # Only one call


# ── run_leaf Convenience Function Tests ────────────────────────────────────────


class TestRunLeaf:
    """run_leaf() — full lifecycle convenience function."""

    def test_run_leaf_success(self, mock_mgr, mock_director, mock_bookbag_signal):
        """run_leaf() should execute full lifecycle on success."""
        result = run_leaf(
            task_prompt="Write hello",
            role="coder",
            domain="python-coding",
        )

        # Worktree was created and disposed
        mock_mgr.create_worktree.assert_called_once()
        mock_mgr.close_worktree.assert_called_once()

        # Brief was written
        mock_mgr.write_student_brief.assert_called_once()

        # Task was dispatched
        mock_director.assert_called_once()

        # Output was written (since status == "success")
        mock_mgr.write_student_output.assert_called_once()

        # Signal was sent
        mock_bookbag_signal.return_value.ready.assert_called_once()

        # Result returned
        assert result["status"] == "success"
        assert result["agent"] == "coder"

    def test_run_leaf_disposes_on_error(self, mock_mgr, mock_director, mock_bookbag_signal):
        """run_leaf() should always dispose, even on task failure."""
        mock_director.return_value = {
            "status": "error",
            "error": "Model unavailable",
        }

        result = run_leaf(
            task_prompt="Write hello",
            role="coder",
            domain="python-coding",
        )

        # Worktree was still disposed
        mock_mgr.close_worktree.assert_called_once()

        # Brief was still written (before task ran)
        mock_mgr.write_student_brief.assert_called_once()

        # Output was NOT written (task failed)
        mock_mgr.write_student_output.assert_not_called()

        # Signal was NOT sent (task failed)
        mock_bookbag_signal.return_value.ready.assert_not_called()

        assert result["status"] == "error"

    def test_run_leaf_disposes_on_orca_failure(self, mock_mgr, mock_director, mock_bookbag_signal):
        """run_leaf() should raise LeafError and dispose on Orca failure."""
        mock_mgr.create_worktree.side_effect = OrcaUnavailableError("Orca not running")

        with pytest.raises(LeafError, match="Failed to boot leaf"):
            run_leaf(
                task_prompt="Write hello",
                role="coder",
                domain="python-coding",
            )

        # close_worktree should not be called since boot failed
        mock_mgr.close_worktree.assert_not_called()
        mock_director.assert_not_called()

    def test_run_leaf_output_data_structure(self, mock_mgr, mock_director, mock_bookbag_signal):
        """run_leaf() output should contain structured review and scores."""
        result = run_leaf(
            task_prompt="Write hello",
            role="coder",
            domain="python-coding",
        )

        # Verify the output data written to the worktree
        call_args = mock_mgr.write_student_output.call_args
        assert call_args is not None
        path, bead, output_data = call_args[0]

        assert "review" in output_data
        assert output_data["review"]["cto_verdict"] == "PASS"
        assert output_data["review"]["coo_verdict"] == "PASS"
        assert output_data["review"]["accepted"] is True

        assert "scores" in output_data
        assert output_data["scores"]["old"] == 50.0
        assert output_data["scores"]["new"] == 60.0

        assert output_data["role"] == "coder"
        assert output_data["domain"] == "python-coding"
        assert output_data["response"] == "def hello(): pass"


# ── CLI Tests ─────────────────────────────────────────────────────────────────


class TestCLI:
    """CLI entry point (main function)."""

    def test_main_leaf(self, monkeypatch, mock_mgr, mock_director, mock_bookbag_signal):
        """python leaf.py --role coder --domain python-coding --task ... should run."""
        import leaf as leaf_module

        monkeypatch.setattr("sys.argv", [
            "leaf.py",
            "--role", "coder",
            "--domain", "python-coding",
            "--task", "Write a function",
        ])

        # main() should not crash
        leaf_module.main()

        # Task was dispatched
        mock_director.assert_called_once()

    def test_main_missing_args(self, monkeypatch):
        """Missing --role should be rejected by argparse."""
        import leaf as leaf_module

        monkeypatch.setattr("sys.argv", ["leaf.py", "--domain", "python-coding"])

        with pytest.raises(SystemExit):
            leaf_module.main()
