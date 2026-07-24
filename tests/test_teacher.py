"""Unit tests for teacher.py — TeacherWorktree lifecycle.

Tests cover:
  - Construction and role validation
  - boot() with create and rediscovery paths
  - sleep() and wake() lifecycle
  - review_cycle() bookbag polling and review
  - prune_sessions() file cleanup
  - close() and context manager
  - Edge cases: invalid roles, empty bookbags, review failures

All external dependencies (Orca, LLM, filesystem) are mocked.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teacher import TeacherWorktree, TeacherError, TEACHER_LENSES, DEFAULT_SESSION_ID
from adversarial_reviewer import LensType, Verdict, ReviewResult
from orca_executor import OrcaUnavailableError
import sleep_state  # Direct module import for patching SESSIONS_DIR / CONSOLIDATION_DIR


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_mgr():
    """Mock OrcaExecutionManager."""
    with patch("teacher.OrcaExecutionManager") as mock:
        instance = MagicMock()
        instance.create_worktree.return_value = "/tmp/worktrees/teacher-cto"
        instance._run_orca.return_value = {"worktrees": []}
        mock.return_value = instance
        yield instance


@pytest.fixture
def mock_sleep_state():
    """Mock sleep_state.execute_sleep and execute_wake."""
    with patch("teacher.execute_sleep") as mock_sleep, \
         patch("teacher.execute_wake") as mock_wake:
        mock_sleep.return_value = {
            "state": MagicMock(tasks_completed=0),
            "consolidation": MagicMock(tasks_completed=0, domains_visited=[]),
            "log_entry": {},
        }
        mock_wake.return_value = {
            "state": MagicMock(agent="teacher-cto", task_queue=[]),
            "consolidation": None,
            "log_entry": {},
        }
        yield mock_sleep, mock_wake


@pytest.fixture
def mock_bookbags():
    """Mock bookbag operations (list_bookbags, read_bookbag, locked_update_bookbag).

    By default, returns no bookbags and update returns a successful result.
    Individual tests can override by accessing the mock directly.
    """
    with patch("teacher.list_bookbags") as mock_list, \
         patch("teacher.read_bookbag") as mock_read, \
         patch("teacher.locked_update_bookbag") as mock_update:

        mock_list.return_value = []
        mock_read.return_value = None
        # Default: update succeeds with a minimal result dict
        mock_update.return_value = {"bead": "test-bead"}
        yield mock_list, mock_read, mock_update


@pytest.fixture
def mock_reviewer(monkeypatch):
    """Mock AdversarialReviewer to return a predictable review result.

    Returns the mock result so tests can configure it (e.g., make it fail).
    """
    result = ReviewResult(
        verdict=Verdict.PASS,
        findings=[],
        lens_used="correctness",
        confidence=0.8,
    )

    class MockReviewer:
        def __init__(self, call_model_fn=None):
            self.call_model_fn = call_model_fn
            self._stats = {}
            self.review_result = result

        def review(self, **kwargs):
            return self.review_result

    monkeypatch.setattr("teacher.AdversarialReviewer", MockReviewer)
    return result


@pytest.fixture
def teacher_cto(mock_mgr, mock_sleep_state, mock_bookbags, mock_reviewer):
    """Create a CTO teacher with all dependencies mocked."""
    t = TeacherWorktree("cto", poll_interval=0.1)
    return t


@pytest.fixture
def teacher_coo(mock_mgr, mock_sleep_state, mock_bookbags, mock_reviewer):
    """Create a COO teacher with all dependencies mocked."""
    t = TeacherWorktree("coo", poll_interval=0.1)
    return t


@pytest.fixture
def booted_teacher_cto(teacher_cto):
    """A CTO teacher that has been booted (mocked Orca)."""
    teacher_cto.boot()
    return teacher_cto


# ── Construction Tests ────────────────────────────────────────────────────────


class TestConstruction:
    """TeacherWorktree construction and role validation."""

    def test_valid_cto(self):
        """CTO role should initialize with correctness + security lenses."""
        t = TeacherWorktree("cto")
        assert t.role == "cto"
        assert t.lenses == [LensType.CORRECTNESS, LensType.SECURITY]
        assert t.session_id == f"{DEFAULT_SESSION_ID}-cto"
        assert t.worktree_name == "teacher-cto"
        assert not t._booted

    def test_valid_coo(self):
        """COO role should initialize with completeness lens."""
        t = TeacherWorktree("coo")
        assert t.role == "coo"
        assert t.lenses == [LensType.COMPLETENESS]
        assert t.session_id == f"{DEFAULT_SESSION_ID}-coo"
        assert t.worktree_name == "teacher-coo"
        assert not t._booted

    def test_invalid_role_raises(self):
        """Invalid role should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown teacher role"):
            TeacherWorktree("principal")

    def test_custom_session_id(self):
        """Custom session_id should be used with role suffix."""
        t = TeacherWorktree("cto", session_id="my-session")
        assert t.session_id == "my-session-cto"

    def test_custom_poll_interval(self):
        """Custom poll interval should be stored."""
        t = TeacherWorktree("cto", poll_interval=2.5)
        assert t.poll_interval == 2.5

        t2 = TeacherWorktree("cto")
        assert t2.poll_interval == 5.0  # default


# ── Boot Tests ────────────────────────────────────────────────────────────────


class TestBoot:
    """Teacher boot() — worktree creation and rediscovery."""

    def test_boot_creates_worktree(self, mock_mgr):
        """boot() should create the teacher worktree via Orca."""
        t = TeacherWorktree("cto")
        path = t.boot()

        mock_mgr.create_worktree.assert_called_once_with("teacher-cto")
        assert path == "/tmp/worktrees/teacher-cto"
        assert t.worktree_path == path
        assert t._booted

    def test_boot_rediscovery_on_existing(self, mock_mgr):
        """When the worktree already exists in Orca, boot() should reuse it
        (rediscover-first) instead of creating a suffixed duplicate."""
        mock_mgr._run_orca.return_value = {
            "worktrees": [
                {"name": "", "path": "/tmp/worktrees/teacher-cto"},
                {"name": "", "path": "/tmp/worktrees/teacher-coo"},
            ]
        }

        t = TeacherWorktree("cto")
        path = t.boot()

        # Rediscover-first: create_worktree must NOT be called when the
        # worktree is already present in the Orca worktree list.
        mock_mgr.create_worktree.assert_not_called()
        mock_mgr._run_orca.assert_called_once_with(["worktree", "list"], timeout=15)
        assert path == "/tmp/worktrees/teacher-cto"
        assert t._booted

    def test_boot_creates_when_absent(self, mock_mgr):
        """When no matching worktree exists, boot() should create it."""
        mock_mgr._run_orca.return_value = {"worktrees": []}

        t = TeacherWorktree("cto")
        path = t.boot()

        mock_mgr.create_worktree.assert_called_once_with("teacher-cto")
        assert path == "/tmp/worktrees/teacher-cto"
        assert t._booted

    def test_boot_fails_when_no_worktree(self, mock_mgr):
        """boot() should raise if creation and rediscovery both fail."""
        mock_mgr._run_orca.return_value = {"worktrees": []}
        mock_mgr.create_worktree.side_effect = OrcaUnavailableError("not found")

        t = TeacherWorktree("cto")
        with pytest.raises(TeacherError, match="could not create or rediscover"):
            t.boot()

        assert not t._booted

    def test_boot_rediscovery_no_match(self, mock_mgr):
        """Rediscovery should skip worktrees with non-matching names."""
        mock_mgr._run_orca.return_value = {
            "worktrees": [
                {"name": "", "path": "/tmp/worktrees/something-else"},
            ]
        }
        mock_mgr.create_worktree.side_effect = OrcaUnavailableError("exists")

        t = TeacherWorktree("cto")
        with pytest.raises(TeacherError):
            t.boot()

    def test_boot_rediscovery_scans_path_basename(self, mock_mgr):
        """Rediscovery should check path basename (since Orca names are empty)."""
        mock_mgr._run_orca.return_value = {
            "worktrees": [
                {"name": "", "path": "/Users/test/orca/workspaces/school-core/teacher-cto"},
            ]
        }

        t = TeacherWorktree("cto")
        path = t.boot()
        assert path == "/Users/test/orca/workspaces/school-core/teacher-cto"
        assert t._booted


# ── Sleep/Wake Tests ─────────────────────────────────────────────────────────


class TestSleepWake:
    """Sleep and wake lifecycle."""

    def test_sleep_calls_execute_sleep(self, booted_teacher_cto, mock_sleep_state):
        """sleep() should call execute_sleep with correct args."""
        mock_sleep, mock_wake = mock_sleep_state
        teacher = booted_teacher_cto
        teacher._episodic_history = [{"bead": "test-1", "status": "success"}]

        teacher.sleep(duration_minutes=5.0)

        mock_sleep.assert_called_once()
        args, kwargs = mock_sleep.call_args
        assert kwargs["session_id"] == teacher.session_id
        assert kwargs["agent"] == "teacher-cto"
        assert kwargs["duration_minutes"] == 5.0
        assert len(kwargs["episodic_history"]) == 1

        # Episodic history should be cleared after sleep
        assert teacher._episodic_history == []

    def test_wake_calls_execute_wake(self, booted_teacher_cto, mock_sleep_state):
        """wake() should call execute_wake."""
        mock_sleep, mock_wake = mock_sleep_state
        teacher = booted_teacher_cto

        teacher.wake()

        mock_wake.assert_called_once_with(session_id=teacher.session_id)

    def test_wake_graceful_on_first_boot(self, booted_teacher_cto, mock_sleep_state):
        """wake() should not crash on first boot when no session exists."""
        mock_sleep, mock_wake = mock_sleep_state
        mock_wake.side_effect = Exception("Session not found")
        teacher = booted_teacher_cto

        result = teacher.wake()

        # Should return empty state instead of crashing
        assert result["state"] is None
        assert result["consolidation"] is None

    def test_sleep_with_no_history(self, booted_teacher_cto, mock_sleep_state):
        """sleep() should work with empty episodic history."""
        mock_sleep, mock_wake = mock_sleep_state
        teacher = booted_teacher_cto

        teacher.sleep(duration_minutes=0.0)
        mock_sleep.assert_called_once()

    def test_multiple_sleep_cycles(self, booted_teacher_cto, mock_sleep_state):
        """Multiple sleep cycles should each clear and repopulate history."""
        mock_sleep, mock_wake = mock_sleep_state
        teacher = booted_teacher_cto

        teacher._episodic_history = [{"bead": "a"}]
        teacher.sleep()
        assert teacher._episodic_history == []

        teacher._episodic_history = [{"bead": "b"}, {"bead": "c"}]
        teacher.sleep()
        assert teacher._episodic_history == []


# ── Review Cycle Tests ────────────────────────────────────────────────────────


class TestReviewCycle:
    """review_cycle() — bookbag polling and review."""

    def test_raises_if_not_booted(self, teacher_cto):
        """review_cycle() should raise if teacher isn't booted."""
        with pytest.raises(TeacherError, match="not booted"):
            teacher_cto.review_cycle()

    def test_no_bookbags_returns_zero(self, booted_teacher_cto, mock_bookbags):
        """No bookbags → return 0."""
        mock_list, mock_read, mock_update = mock_bookbags
        mock_list.return_value = []

        count = booted_teacher_cto.review_cycle()
        assert count == 0

    def test_skips_already_reviewed_bookbags(self, booted_teacher_cto, mock_bookbags):
        """Bookbag already reviewed by this teacher should be skipped."""
        mock_list, mock_read, mock_update = mock_bookbags
        mock_list.return_value = ["bead-1"]
        mock_read.return_value = {
            "bead": "bead-1",
            "cto_verdict": "PASS",  # Already reviewed
            "output": "some code",
            "domain": "python-coding",
        }

        count = booted_teacher_cto.review_cycle()
        assert count == 0  # Skipped — already has verdict

    def test_reviews_unreviewed_bookbag(self, booted_teacher_cto, mock_bookbags):
        """Unreviewed bookbag should be reviewed and updated."""
        mock_list, mock_read, mock_update = mock_bookbags
        bead = "bead-unreviewed"
        mock_list.return_value = [bead]
        mock_read.return_value = {
            "bead": bead,
            "cto_verdict": "",  # Not yet reviewed by CTO
            "output": "def hello(): pass",
            "domain": "python-coding",
            "difficulty": "easy",
            "task": "Write hello function",
        }
        mock_update.return_value = {
            "bead": bead,
            "cto_verdict": "PASS",
        }

        count = booted_teacher_cto.review_cycle()
        assert count == 1

        # Should update the bookbag with cto_verdict
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        # bead is the first positional arg
        assert args[0] == bead
        assert kwargs["lock_timeout"] == 10.0
        assert kwargs["cto_verdict"] == "PASS"

    def test_review_failure_records_error(self, booted_teacher_cto, mock_bookbags, mock_reviewer):
        """Review failure should log error and record in episodic history.

        Patches the existing reviewer instance to fail, then verifies the
        exception handler records it in episodic history and returns 1
        (processed — even though the review failed).
        """
        mock_list, mock_read, mock_update = mock_bookbags
        bead = "bead-fail"
        mock_list.return_value = [bead]
        mock_read.return_value = {
            "bead": bead,
            "cto_verdict": "",
            "output": "broken code",
            "domain": "python-coding",
        }
        # Make the update succeed so we reach the review call
        mock_update.return_value = {"bead": bead}

        # Make the existing reviewer instance fail
        def failing_review(**kwargs):
            raise RuntimeError("Model call failed")

        booted_teacher_cto._reviewer.review = failing_review

        count = booted_teacher_cto.review_cycle()
        assert count == 1  # Still returns 1 — we processed (and failed) the bookbag

        # Episodic history should record the failure
        assert len(booted_teacher_cto._episodic_history) == 1
        assert booted_teacher_cto._episodic_history[0]["status"] == "error"
        assert "Model call failed" in booted_teacher_cto._episodic_history[0]["error"]

    def test_empty_output_still_reviewed(self, booted_teacher_cto, mock_bookbags):
        """Empty student output should still trigger a review."""
        mock_list, mock_read, mock_update = mock_bookbags
        bead = "bead-empty"
        mock_list.return_value = [bead]
        mock_read.return_value = {
            "bead": bead,
            "cto_verdict": "",
            "output": "",
            "domain": "general",
        }

        count = booted_teacher_cto.review_cycle()
        assert count == 1
        mock_update.assert_called_once()
        assert mock_update.call_args[1]["cto_verdict"] == "PASS"

    def test_cto_vs_coo_verdict_field(self, booted_teacher_cto, teacher_coo, mock_bookbags):
        """CTO should write to cto_verdict, COO to coo_verdict."""
        mock_list_cto, mock_read_cto, mock_update_cto = mock_bookbags
        bead = "bead-verdict-test"

        # CTO reviews
        mock_list_cto.return_value = [bead]
        mock_read_cto.return_value = {
            "bead": bead, "cto_verdict": "", "coo_verdict": "", "output": "x",
        }
        booted_teacher_cto.review_cycle()
        assert mock_update_cto.call_args[1]["cto_verdict"] == "PASS"
        assert "coo_verdict" not in mock_update_cto.call_args[1]

        # COO reviews (reset mock)
        mock_update_cto.reset_mock()
        mock_read_cto.return_value = {
            "bead": bead, "cto_verdict": "PASS", "coo_verdict": "", "output": "x",
        }
        teacher_coo._booted = True
        teacher_coo.review_cycle()
        assert mock_update_cto.call_args[1]["coo_verdict"] == "PASS"

    def test_review_skips_when_lock_fails(self, booted_teacher_cto, mock_bookbags):
        """If locked_update_bookbag returns None (lock timeout), return 0."""
        mock_list, mock_read, mock_update = mock_bookbags
        bead = "bead-locked"
        mock_list.return_value = [bead]
        mock_read.return_value = {
            "bead": bead, "cto_verdict": "", "output": "x",
        }
        mock_update.return_value = None  # Lock timeout

        count = booted_teacher_cto.review_cycle()
        assert count == 0  # Could not update — considered skipped


# ── Prune Sessions Tests ──────────────────────────────────────────────────────


class TestPruneSessions:
    """prune_sessions() — session file cleanup."""

    def test_no_sessions_to_prune(self, booted_teacher_cto, tmp_path):
        """With fewer sessions than max_cycles, nothing should be pruned."""
        with patch.object(sleep_state, "SESSIONS_DIR", tmp_path), \
             patch.object(sleep_state, "CONSOLIDATION_DIR", tmp_path):

            count = booted_teacher_cto.prune_sessions(max_cycles=10)
            assert count == 0

    def test_prunes_excess_session_files(self, booted_teacher_cto, tmp_path):
        """Extra session files beyond max_cycles should be removed."""
        with patch.object(sleep_state, "SESSIONS_DIR", tmp_path), \
             patch.object(sleep_state, "CONSOLIDATION_DIR", tmp_path):

            # Create 5 session files with different mtimes
            for i in range(5):
                file = tmp_path / f"teacher-default-cto_{i}.json"
                file.write_text('{"test": true}')
                time.sleep(0.02)  # Ensure different mtimes

            count = booted_teacher_cto.prune_sessions(max_cycles=3)
            assert count == 2  # 5 - 3 = 2 pruned

            remaining = list(tmp_path.glob("teacher-default-cto*.json"))
            assert len(remaining) == 3

    def test_prunes_excess_consolidation_files(self, booted_teacher_cto, tmp_path):
        """Extra consolidation files beyond max_cycles should be removed."""
        with patch.object(sleep_state, "CONSOLIDATION_DIR", tmp_path), \
             patch.object(sleep_state, "SESSIONS_DIR", tmp_path):

            for i in range(4):
                file = tmp_path / f"teacher-default-cto_{i}.yaml"
                file.write_text("key: value")
                time.sleep(0.02)

            count = booted_teacher_cto.prune_sessions(max_cycles=2)
            assert count == 2  # 4 - 2 = 2 pruned

            remaining = list(tmp_path.glob("teacher-default-cto*.yaml"))
            assert len(remaining) == 2

    def test_keeps_newest_files(self, booted_teacher_cto, tmp_path):
        """After pruning, the remaining files should be the newest ones."""
        with patch.object(sleep_state, "SESSIONS_DIR", tmp_path), \
             patch.object(sleep_state, "CONSOLIDATION_DIR", tmp_path):

            for i in range(5):
                file = tmp_path / f"session_{i}.json"
                file.write_text('{"test": true}')
                time.sleep(0.02)

            booted_teacher_cto.session_id = "session"
            count = booted_teacher_cto.prune_sessions(max_cycles=2)
            assert count == 3

            remaining = sorted(p.name for p in tmp_path.glob("session*.json"))
            assert remaining == ["session_3.json", "session_4.json"]

    def test_handles_deleted_files_gracefully(self, booted_teacher_cto, tmp_path):
        """prune_sessions should not crash if a file is deleted between listing and unlinking."""
        with patch.object(sleep_state, "SESSIONS_DIR", tmp_path), \
             patch.object(sleep_state, "CONSOLIDATION_DIR", tmp_path):

            file = tmp_path / "teacher-default-cto.json"
            file.write_text('{"test": true}')

            original_unlink = Path.unlink

            def flaky_unlink(self, *a, **kw):
                raise OSError("File gone")

            with patch.object(Path, "unlink", flaky_unlink):
                count = booted_teacher_cto.prune_sessions(max_cycles=0)
                assert count == 0  # Failed to unlink

    def test_no_files_of_wrong_type(self, booted_teacher_cto, tmp_path):
        """Files that don't match the session prefix should be ignored."""
        with patch.object(sleep_state, "SESSIONS_DIR", tmp_path), \
             patch.object(sleep_state, "CONSOLIDATION_DIR", tmp_path):

            Path(tmp_path / "other-agent.json").write_text("{}")
            Path(tmp_path / "other-agent.yaml").write_text("key: val")

            count = booted_teacher_cto.prune_sessions(max_cycles=1)
            assert count == 0
            assert Path(tmp_path / "other-agent.json").exists()
            assert Path(tmp_path / "other-agent.yaml").exists()


# ── Close and Context Manager Tests ───────────────────────────────────────────


class TestClose:
    """close() and context manager."""

    def test_close_cleans_up(self, booted_teacher_cto, mock_mgr):
        """close() should call close_worktree and reset state."""
        teacher = booted_teacher_cto
        assert teacher._mgr is not None  # boot() sets _mgr

        teacher.close()

        mock_mgr.close_worktree.assert_called_once_with("/tmp/worktrees/teacher-cto")
        assert teacher.worktree_path is None
        assert not teacher._booted

    def test_close_idempotent(self, booted_teacher_cto):
        """Calling close() twice should not raise."""
        teacher = booted_teacher_cto
        teacher.close()
        teacher.close()  # Should not raise

    def test_context_manager(self, mock_mgr, mock_sleep_state, mock_bookbags, mock_reviewer):
        """Using 'with TeacherWorktree()' should boot and close."""
        with TeacherWorktree("cto") as t:
            assert t._booted
            assert t.worktree_path == mock_mgr.create_worktree.return_value

        assert not t._booted
        assert t.worktree_path is None

    def test_context_manager_on_exception(self, mock_mgr, mock_sleep_state, mock_bookbags, mock_reviewer):
        """Context manager should close even when an exception occurs."""
        class TestError(Exception):
            pass

        try:
            with TeacherWorktree("cto"):
                raise TestError("inside context")
        except TestError:
            pass

        # The teacher instance is gone after the context, so we verify
        # via the mock: close_worktree was called
        mock_mgr.close_worktree.assert_called_once()


# ── Integration-Style Tests ──────────────────────────────────────────────────


class TestRunLoop:
    """run_loop() edge cases (not the infinite loop itself, just its entry points)."""

    def test_run_loop_requires_boot(self, teacher_cto):
        """run_loop() should print error and return if not booted."""
        teacher_cto.run_loop()
        # Should not crash — just prints error message

    def test_run_loop_polls_bookbags_repeatedly(self, booted_teacher_cto):
        """run_loop() should call review_cycle repeatedly until no bookbags found."""
        teacher = booted_teacher_cto
        call_count = 0

        def limited_review():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise StopIteration("Test done")  # Break the infinite loop
            return 0

        teacher.review_cycle = limited_review

        with pytest.raises(StopIteration):
            teacher.run_loop()

        assert call_count >= 1


# ── CLI Tests ─────────────────────────────────────────────────────────────────


class TestCLI:
    """CLI entry point (main function)."""

    def test_main_cto_once(self, monkeypatch):
        """python teacher.py --role cto --once should boot and review once."""
        import teacher as teacher_module

        boot_called = False
        review_called = False

        def mock_boot(self):
            nonlocal boot_called
            boot_called = True
            self._booted = True

        def mock_review_cycle(self):
            nonlocal review_called
            review_called = True
            return 1

        monkeypatch.setattr(TeacherWorktree, "boot", mock_boot)
        monkeypatch.setattr(TeacherWorktree, "review_cycle", mock_review_cycle)
        monkeypatch.setattr("sys.argv", ["teacher.py", "--role", "cto", "--once"])

        teacher_module.main()

        assert boot_called
        assert review_called

    def test_main_invalid_role(self, monkeypatch):
        """Invalid --role should be rejected by argparse."""
        import teacher as teacher_module

        monkeypatch.setattr("sys.argv", ["teacher.py", "--role", "invalid"])

        with pytest.raises(SystemExit):
            teacher_module.main()


# ── TEACHER_LENSES Configuration Tests ────────────────────────────────────────


class TestTeacherLenses:
    """TEACHER_LENSES configuration correctness."""

    def test_cto_lenses_are_correct(self):
        """CTO should have correctness + security lenses."""
        assert LensType.CORRECTNESS in TEACHER_LENSES["cto"]
        assert LensType.SECURITY in TEACHER_LENSES["cto"]
        assert len(TEACHER_LENSES["cto"]) == 2

    def test_coo_lenses_are_correct(self):
        """COO should have completeness + build lenses."""
        assert LensType.COMPLETENESS in TEACHER_LENSES["coo"]
        assert len(TEACHER_LENSES["coo"]) == 1

    def test_only_known_roles(self):
        """Only 'cto' and 'coo' should be valid roles."""
        assert set(TEACHER_LENSES.keys()) == {"cto", "coo"}
