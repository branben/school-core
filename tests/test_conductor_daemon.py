"""test_conductor_daemon.py — Path A daemon-mode tests for conductor.py.

The Path-A refactor replaces 3 cron Orca automations with 2 persistent
Python daemons in 2 fixed terminals.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from conductor import (
    SERVE_STATE_PATH,
    load_serve_state,
    save_serve_state,
    _send_to_terminal,
    _find_unreviewed_beads_for,
    _cleanup_legacy_automations,
    _gc_terminals,
    _launch_serve,
    _teardown_serve,
    principal_dispatch_loop,
    teacher_both_loop,
)


def _args(principal_daemon=False, teacher_both_daemon=False, daemon_interval=1,
          max_ticks=0, once=False, repo="__global__", difficulty="easy",
          doubt_enabled=False, serve_state_path=None):
    return SimpleNamespace(
        principal_daemon=principal_daemon, teacher_both_daemon=teacher_both_daemon,
        daemon_interval=daemon_interval, max_ticks=max_ticks, once=once,
        repo=repo, difficulty=difficulty, doubt_enabled=doubt_enabled,
        serve_state_path=serve_state_path,
    )


class TestServeStateRoundtrip:
    def test_save_then_load_returns_equal_payload(self, tmp_path):
        target = tmp_path / "serve-state.json"
        payload = {
            "principal_terminal_handle": "h-principal-xyz",
            "teacher_both_terminal_handle": "h-teacher-both-abc",
            "created_at": "2026-07-31T00:00:00+00:00",
        }
        save_serve_state(payload, target)
        assert load_serve_state(target) == payload

    def test_save_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "serve-state.json"
        save_serve_state({"x": 1}, target)
        assert target.exists()

    def test_load_returns_empty_dict_when_missing(self, tmp_path):
        assert load_serve_state(tmp_path / "nope.json") == {}

    def test_load_returns_empty_dict_on_corrupt_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ this is not json")
        assert load_serve_state(p) == {}

    def test_save_is_atomic_no_tmp_leftover(self, tmp_path):
        target = tmp_path / "serve-state.json"
        save_serve_state({"k": "v"}, target)
        siblings = list(tmp_path.iterdir())
        assert target in siblings
        assert all(not p.name.endswith(".tmp") for p in siblings)


@pytest.fixture
def mock_mgr():
    m = MagicMock()
    m._run_orca.return_value = {"ok": True}
    return m


class TestSendToTerminal:
    def test_send_to_terminal_passes_correct_orca_args(self, mock_mgr):
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _send_to_terminal("h-handle-xyz", "echo hi", enter=True)
        mock_mgr._run_orca.assert_called_once()
        args = mock_mgr._run_orca.call_args[0][0]
        assert args[0:2] == ["terminal", "send"]
        assert "--terminal" in args
        assert "h-handle-xyz" in args
        assert "--text" in args
        assert "echo hi" in args
        assert "--enter" in args

    def test_send_to_terminal_omits_enter_when_disabled(self, mock_mgr):
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _send_to_terminal("h-handle-xyz", "echo hi", enter=False)
        args = mock_mgr._run_orca.call_args[0][0]
        assert "--enter" not in args

    def test_send_to_terminal_is_best_effort_on_exception(self, mock_mgr):
        mock_mgr._run_orca.side_effect = RuntimeError("orca down")
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _send_to_terminal("h-handle-xyz", "echo hi")

    def test_send_to_terminal_is_best_effort_when_orca_unavailable(self):
        with patch("conductor.OrcaExecutionManager") as MockMgr:
            instance = MockMgr.return_value
            instance._run_orca.side_effect = OSError("no orca")
            _send_to_terminal("h-handle-xyz", "echo hi")


class TestLegacyCleanup:
    def test_removes_principal_automation(self, mock_mgr):
        fake_automations = [
            {"id": "auto-1", "name": "agent-school-principal"},
            {"id": "auto-2", "name": "Spec-Gap Harness Loop"},
        ]
        with patch("conductor.orca_automations_list", return_value=fake_automations), \
             patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            removed = _cleanup_legacy_automations(mock_mgr)
        assert removed == 1
        mock_mgr._run_orca.assert_called_once_with(
            ["automations", "remove", "--id", "auto-1"], timeout=15
        )

    def test_removes_both_teacher_automations(self, mock_mgr):
        fake_automations = [
            {"id": "ctoid", "name": "agent-school-teacher-cto"},
            {"id": "cooid", "name": "agent-school-teacher-coo"},
            {"id": "other", "name": "Spec-Gap Harness Loop"},
        ]
        with patch("conductor.orca_automations_list", return_value=fake_automations), \
             patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            removed = _cleanup_legacy_automations(mock_mgr)
        assert removed == 2

    def test_no_op_when_no_legacy_automations(self, mock_mgr):
        with patch("conductor.orca_automations_list", return_value=[]), \
             patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            removed = _cleanup_legacy_automations(mock_mgr)
        assert removed == 0
        mock_mgr._run_orca.assert_not_called()

    def test_continues_on_per_automation_failure(self, mock_mgr):
        fake = [
            {"id": "ok-primary", "name": "agent-school-principal"},
            {"id": "wedged", "name": "agent-school-teacher-cto"},
            {"id": "ok-coo", "name": "agent-school-teacher-coo"},
        ]
        n = {"n": 0}
        def maybe_fail(*args, **kwargs):
            n["n"] += 1
            if n["n"] == 2:
                raise RuntimeError("wedged")
            return {"ok": True}
        mock_mgr._run_orca.side_effect = maybe_fail
        with patch("conductor.orca_automations_list", return_value=fake), \
             patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            removed = _cleanup_legacy_automations(mock_mgr)
        assert removed == 2


class TestFindUnreviewedBeads:
    def test_returns_bead_when_both_verdicts_missing(self, monkeypatch):
        monkeypatch.setattr("bookbag.list_bookbags", lambda repo=None: ["b1"])
        monkeypatch.setattr("bookbag.read_bookbag", lambda b, r=None: {"cto_verdict": "", "coo_verdict": ""})
        assert "b1" in _find_unreviewed_beads_for("__global__")

    def test_excludes_bead_when_both_verdicts_present(self, monkeypatch):
        monkeypatch.setattr("bookbag.list_bookbags", lambda repo=None: ["b1"])
        monkeypatch.setattr("bookbag.read_bookbag", lambda b, r=None: {"cto_verdict": "PASS", "coo_verdict": "PASS"})
        assert "b1" not in _find_unreviewed_beads_for("__global__")

    def test_includes_bead_when_only_coo_missing(self, monkeypatch):
        monkeypatch.setattr("bookbag.list_bookbags", lambda repo=None: ["b1"])
        monkeypatch.setattr("bookbag.read_bookbag", lambda b, r=None: {"cto_verdict": "PASS", "coo_verdict": ""})
        assert "b1" in _find_unreviewed_beads_for("__global__")

    def test_includes_bead_when_only_cto_missing(self, monkeypatch):
        monkeypatch.setattr("bookbag.list_bookbags", lambda repo=None: ["b1"])
        monkeypatch.setattr("bookbag.read_bookbag", lambda b, r=None: {"cto_verdict": "", "coo_verdict": "PASS"})
        assert "b1" in _find_unreviewed_beads_for("__global__")

    def test_returns_empty_list_when_no_beads(self, monkeypatch):
        monkeypatch.setattr("bookbag.list_bookbags", lambda repo=None: [])
        assert _find_unreviewed_beads_for("__global__") == []


class TestLaunchServe:
    def test_launch_does_NOT_create_automations(self, tmp_path, mock_mgr):
        mock_mgr._run_orca.return_value = {"ok": True, "result": {"terminals": []}}
        state_path = tmp_path / "serve-state.json"
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._find_or_create_terminal", side_effect=["hp", "ht"]), \
             patch("conductor._send_to_terminal"), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.load_config", return_value={"target_repos": []}), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock())
            _launch_serve(state_path=state_path)
        for c in mock_mgr._run_orca.call_args_list:
            called = c[0][0] if c[0] else []
            assert "create" not in called, f"created: {called}"

    def test_launch_sends_two_daemon_launchers(self, tmp_path, mock_mgr):
        sent = []
        mock_mgr._run_orca.return_value = {"ok": True, "result": {"terminals": []}}
        state_path = tmp_path / "serve-state.json"
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._find_or_create_terminal", side_effect=["hp", "ht"]), \
             patch("conductor._send_to_terminal", side_effect=lambda h, t, e=True: sent.append(t)), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.load_config", return_value={"target_repos": []}), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock())
            _launch_serve(state_path=state_path)
        assert len(sent) == 2
        assert "--principal-daemon" in sent[0]
        assert "--daemon-interval 1800" in sent[0]
        assert "--teacher-both-daemon" in sent[1]
        assert "--daemon-interval 60" in sent[1]

    def test_launch_persists_terminal_handles(self, tmp_path, mock_mgr):
        mock_mgr._run_orca.return_value = {"ok": True, "result": {"terminals": []}}
        state_path = tmp_path / "serve-state.json"
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._find_or_create_terminal", side_effect=["hp-xyz", "ht-abc"]), \
             patch("conductor._send_to_terminal"), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.load_config", return_value={"target_repos": []}), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock())
            _launch_serve(state_path=state_path)
        state = load_serve_state(state_path)
        assert state["principal_terminal_handle"] == "hp-xyz"
        assert state["teacher_both_terminal_handle"] == "ht-abc"
        assert "created_at" in state

    def test_launch_runs_legacy_cleanup(self, tmp_path, mock_mgr):
        mock_mgr._run_orca.return_value = {"ok": True, "result": {"terminals": []}}
        state_path = tmp_path / "serve-state.json"
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._find_or_create_terminal", side_effect=["hp", "ht"]), \
             patch("conductor._send_to_terminal"), \
             patch("conductor._cleanup_legacy_automations") as mc, \
             patch("conductor.load_config", return_value={"target_repos": []}), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock())
            _launch_serve(state_path=state_path)
        mc.assert_called_once()


class TestTeardownServe:
    def test_teardown_closes_both_terminals(self, tmp_path, mock_mgr):
        state_path = tmp_path / "serve-state.json"
        save_serve_state({
            "principal_terminal_handle": "h-principal-xyz",
            "teacher_both_terminal_handle": "h-teacher-both-abc",
        }, state_path)
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock(), close=MagicMock())
            _teardown_serve(state_path=state_path)
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-principal-xyz" in closed
        assert "h-teacher-both-abc" in closed

    def test_teardown_deletes_state_file(self, tmp_path, mock_mgr):
        state_path = tmp_path / "serve-state.json"
        save_serve_state({"x": 1}, state_path)
        assert state_path.exists()
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock(), close=MagicMock())
            _teardown_serve(state_path=state_path)
        assert not state_path.exists()

    def test_teardown_best_effort_when_state_missing(self, tmp_path, mock_mgr):
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock(), close=MagicMock())
            _teardown_serve(state_path=tmp_path / "missing.json")

    def test_teardown_skips_missing_handles(self, tmp_path, mock_mgr):
        state_path = tmp_path / "serve-state.json"
        save_serve_state({"principal_terminal_handle": "h-only"}, state_path)
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock(), close=MagicMock())
            _teardown_serve(state_path=state_path)
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-only" in closed
        assert len(closed) == 1


class TestTeacherBothLoopOnce:
    def test_once_runs_single_tick_and_exits(self, monkeypatch):
        called = {"n": 0}
        ft = MagicMock()
        def fake_review():
            called["n"] += 1
            return 1
        ft.review_cycle.side_effect = fake_review
        ft.boot = MagicMock()
        ft.close = MagicMock()
        monkeypatch.setattr("conductor._find_unreviewed_beads_for", lambda r: ["bead-x"])
        def fake_sleep(_):
            raise KeyboardInterrupt()
        monkeypatch.setattr("conductor.time.sleep", fake_sleep)
        with patch("conductor.TeacherWorktree", return_value=ft):
            teacher_both_loop(_args(teacher_both_daemon=True, once=True, daemon_interval=1))
        assert called["n"] == 2

    def test_loop_handles_no_beads(self, monkeypatch):
        monkeypatch.setattr("conductor._find_unreviewed_beads_for", lambda r: [])
        def fake_sleep(_):
            raise KeyboardInterrupt()
        monkeypatch.setattr("conductor.time.sleep", fake_sleep)
        ft = MagicMock()
        ft.boot = MagicMock()
        ft.close = MagicMock()
        ft.review_cycle = MagicMock(return_value=0)
        with patch("conductor.TeacherWorktree", return_value=ft):
            teacher_both_loop(_args(teacher_both_daemon=True, once=True, daemon_interval=1))
        ft.review_cycle.assert_not_called()


class TestPrincipalDispatchLoopOnce:
    def test_once_runs_single_dispatch(self, monkeypatch):
        called = {"n": 0}
        def fake(*args, **kwargs):
            called["n"] += 1
            return {"status": "ok", "bead": "b"}
        monkeypatch.setattr("conductor._principal_dispatch", fake)
        monkeypatch.setattr("conductor.time.sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        principal_dispatch_loop(_args(principal_daemon=True, once=True, daemon_interval=1))
        assert called["n"] == 1


class TestServeStateConstants:
    def test_serve_state_path_is_in_user_home(self):
        assert SERVE_STATE_PATH.parent == Path.home() / ".school-core"
    def test_serve_state_extension_is_json(self):
        assert SERVE_STATE_PATH.suffix == ".json"


# ── TestGcTerminals ──────────────────────────────────────────────────────────


@pytest.fixture
def gc_terminals_mock_response():
    return {"result": {"terminals": [
        {"handle": "h-empty-1", "title": ""},
        {"handle": "h-named", "title": "Conductor serve command..."},
        {"handle": "h-empty-2", "title": None},
        {"handle": "h-legacy", "title": "agent-school-teacher-cto-3"},
        {"handle": "h-current-principal", "title": "agent-school-principal"},
        {"handle": "h-current-both", "title": "agent-school-teacher-both"},
        {"handle": "h-main-branch", "title": "Main branch worktree"},
    ]}}


class TestGcTerminals:
    def test_gc_closes_empty_title_terminals(self, gc_terminals_mock_response, mock_mgr):
        mock_mgr._run_orca.return_value = gc_terminals_mock_response
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-empty-1" in closed
        assert "h-empty-2" in closed

    def test_gc_closes_stale_agent_school_terminals(self, gc_terminals_mock_response, mock_mgr):
        mock_mgr._run_orca.return_value = gc_terminals_mock_response
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-legacy" in closed
        assert "h-current-principal" in closed
        assert "h-current-both" in closed

    def test_gc_preserves_named_user_terminals(self, gc_terminals_mock_response, mock_mgr):
        mock_mgr._run_orca.return_value = gc_terminals_mock_response
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-named" not in closed
        assert "h-main-branch" not in closed

    def test_gc_dry_run_lists_but_does_not_close(self, gc_terminals_mock_response, mock_mgr):
        mock_mgr._run_orca.return_value = gc_terminals_mock_response
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals(dry_run=True)
        assert n == 0
        mock_mgr.close_terminal.assert_not_called()

    def test_gc_returns_zero_when_orca_list_fails(self):
        with patch("conductor.OrcaExecutionManager") as MockMgr:
            instance = MockMgr.return_value
            instance._run_orca.side_effect = RuntimeError("orca dead")
            n = _gc_terminals()
        assert n == 0

    def test_gc_continues_on_per_close_failure(self, mock_mgr):
        terminals = [
            {"handle": "h-1", "title": ""},
            {"handle": "h-2", "title": ""},
            {"handle": "h-3", "title": ""},
        ]
        mock_mgr._run_orca.return_value = {"result": {"terminals": terminals}}
        n = {"n": 0}
        def maybe_fail(handle):
            n["n"] += 1
            if n["n"] == 2:
                raise RuntimeError("Terminal wedged")
        mock_mgr.close_terminal.side_effect = maybe_fail
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            closed = _gc_terminals()
        assert closed == 2

    def test_gc_handles_bare_list_response(self, mock_mgr):
        mock_mgr._run_orca.return_value = [
            {"handle": "h-empty", "title": ""},
            {"handle": "h-keep", "title": "Main branch"},
        ]
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-empty" in closed
        assert "h-keep" not in closed
        assert n == 1

    def test_gc_handles_flat_terminals_response(self, mock_mgr):
        mock_mgr._run_orca.return_value = {"terminals": [
            {"handle": "h-empty", "title": ""},
        ]}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-empty" in closed

    def test_gc_handles_result_wrapped_response(self, mock_mgr):
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-empty", "title": ""},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-empty" in closed

    def test_gc_returns_zero_when_no_matches(self, mock_mgr):
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-1", "title": "Conductor serve command"},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        assert n == 0
        mock_mgr.close_terminal.assert_not_called()

    def test_gc_skips_terminal_without_handle(self, mock_mgr):
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"title": ""},
            {"handle": "h-empty", "title": ""},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            closed = _gc_terminals()
        assert closed == 1
        assert mock_mgr.close_terminal.call_args.args[0] == "h-empty"


# ── TestTeardownWithGc ───────────────────────────────────────────────────────


class TestTeardownWithGc:
    def test_teardown_automatically_calls_gc_terminals(self, tmp_path, mock_mgr):
        state_path = tmp_path / "serve-state.json"
        save_serve_state({
            "principal_terminal_handle": "h-principal",
            "teacher_both_terminal_handle": "h-teacher-both",
        }, state_path)
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor._gc_terminals") as mock_gc, \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock(), close=MagicMock())
            _teardown_serve(state_path=state_path)
        mock_gc.assert_called()
        assert mock_gc.call_args.kwargs.get("mgr") is mock_mgr

    def test_gc_runs_after_serve_state_deleted(self, tmp_path, mock_mgr):
        state_path = tmp_path / "serve-state.json"
        save_serve_state({"x": 1}, state_path)
        assert state_path.exists()
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr), \
             patch("conductor._cleanup_legacy_automations", return_value=0), \
             patch("conductor._gc_terminals", return_value=0), \
             patch("conductor.TeacherWorktree") as MockTW:
            MockTW.return_value = MagicMock(boot=MagicMock(), close=MagicMock())
            _teardown_serve(state_path=state_path)
        assert not state_path.exists()


# ── TestGcTerminalsFailureSummary ─────────────────────────────────────────────


class TestGcTerminalsFailureSummary:
    """Reviewer #5: the 200-terminal failure-summary case."""

    def test_summary_printed_when_per_close_failures(self, mock_mgr, capsys):
        terminals = [
            {"handle": "h-wedged-1", "title": ""},
            {"handle": "h-wedged-2", "title": ""},
            {"handle": "h-wedged-3", "title": ""},
        ]
        mock_mgr._run_orca.return_value = {"result": {"terminals": terminals}}
        mock_mgr.close_terminal.side_effect = RuntimeError("wedge")
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            closed = _gc_terminals()
        captured = capsys.readouterr().out
        assert "summary: failed to close 3 terminal(s)" in captured
        assert closed == 0

    def test_no_summary_when_all_succeeded(self, mock_mgr, capsys):
        terminals = [{"handle": "h-ok", "title": ""}]
        mock_mgr._run_orca.return_value = {"result": {"terminals": terminals}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals()
        captured = capsys.readouterr().out
        assert "summary" not in captured


# ── TestGcTerminalsEdgeCases ─────────────────────────────────────────────────


class TestGcTerminalsEdgeCases:
    """Reviewer #7: malformed orca payloads must not crash the gc path."""

    def test_returns_zero_when_orca_returns_none(self, mock_mgr):
        mock_mgr._run_orca.return_value = None
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        assert n == 0

    def test_handles_string_instead_of_list_with_no_crash(self, mock_mgr):
        mock_mgr._run_orca.return_value = "not a list"
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        assert n == 0

    def test_handles_termininals_field_as_string(self, mock_mgr):
        mock_mgr._run_orca.return_value = {"terminals": "not a list"}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        assert n == 0


    def test_is_match_coerces_non_string_title_gracefully(self, mock_mgr):
        """Non-string titles (int/list/None) coerce to empty and match.

        The isinstance guard prevents ``["x","y"].strip()`` from raising; if
        it regressed the call itself would explode before the assertions run,
        so simply reaching ``assert n == 3`` is the load-bearing signal.
        """
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-int", "title": 42, "name": ""},
            {"handle": "h-list", "title": ["x", "y"], "name": ""},
            {"handle": "h-none", "title": None, "name": ""},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        assert n == 3
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert sorted(closed) == ["h-int", "h-list", "h-none"]

    def test_crash_guarded_against_non_str_handle_id(self, mock_mgr):
        """handle=None falls through to id via the `or` chain."""
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": None, "id": "h-real", "title": ""},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals()
        assert n == 1
        assert mock_mgr.close_terminal.call_args.args[0] == "h-real"

    def test_skips_handles_in_active_serve_state(self, tmp_path, mock_mgr):
        """Reviewer #8: live daemons in serve-state.json must NOT be closed.

        The dry-run preview surfaced this regression — ``is_match`` matched
        every ``agent-school-*`` handle, including the two live daemons Path
        A just launched. This test pins the contract: ``--gc-terminals``
        MUST preserve any handle recorded in the active serve-state.
        """
        state_path = tmp_path / "serve-state.json"
        save_serve_state({
            "principal_terminal_handle": "h-live-principal",
            "teacher_both_terminal_handle": "h-live-teacher-both",
        }, state_path)
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-live-principal", "title": "agent-school-principal"},
            {"handle": "h-live-teacher-both", "title": "agent-school-teacher-both"},
            {"handle": "h-stale-cto", "title": "agent-school-teacher-cto"},
            {"handle": "h-stale-coo", "title": "agent-school-teacher-coo"},
            {"handle": "h-empty-orphan", "title": ""},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals(state_path=state_path)
        closed = sorted(c.args[0] for c in mock_mgr.close_terminal.call_args_list)
        assert closed == ["h-empty-orphan", "h-stale-coo", "h-stale-cto"]
        assert n == 3

    def test_no_skip_when_serve_state_absent(self, tmp_path, mock_mgr):
        """Without serve-state.json, every match closes (legacy behavior)."""
        state_path = tmp_path / "serve-state.json"  # never created on disk
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-1", "title": "agent-school-principal"},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals(state_path=state_path)
        assert n == 1
        assert mock_mgr.close_terminal.call_args.args[0] == "h-1"

    def test_skips_when_serve_state_payload_corrupt(self, tmp_path, mock_mgr):
        """Corrupt JSON must NOT cause the guard to flip and skip legitimate work."""
        state_path = tmp_path / "serve-state.json"
        state_path.write_text("{ this is not valid json")
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-1", "title": "agent-school-principal"},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            n = _gc_terminals(state_path=state_path)
        assert n == 1

    def test_dry_run_excludes_live_daemons_from_would_close(
        self, tmp_path, mock_mgr, capsys
    ):
        """Reviewer #8 polish: live handles must NOT appear under 'would close'.

        Locks in the operator-facing contract: ``--gc-terminals --gc-terminals-dry-run``
        is the pre-flight check — if a live daemon is silently swallowed by
        the guard without explanation, the operator can't reason about what's
        protected vs. what's at risk of closure.
        """
        state_path = tmp_path / "serve-state.json"
        save_serve_state({
            "principal_terminal_handle": "h-live-principal",
            "teacher_both_terminal_handle": "h-live-teacher-both",
        }, state_path)
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-live-principal", "title": "agent-school-principal"},
            {"handle": "h-live-teacher-both", "title": "agent-school-teacher-both"},
            {"handle": "h-stale-cto", "title": "agent-school-teacher-cto"},
        ]}}
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            _gc_terminals(dry_run=True, state_path=state_path)
        captured = capsys.readouterr().out
        # Live handles must NOT be listed as 'would close' candidates.
        for line in captured.splitlines():
            if "would close" in line:
                assert "h-live-principal" not in line
                assert "h-live-teacher-both" not in line
        # The stale candidate is still surfaced normally.
        assert "h-stale-cto" in captured
        # The 🛡️ trace must be emitted so operators see WHY live tabs are absent.
        assert "would skip live daemon" in captured
        assert "h-live-principal" in captured
        assert "h-live-teacher-both" in captured

# ── TestGcTerminalsFlagDispatch ───────────────────────────────────────────────


class TestGcTerminalsFlagDispatch:
    """Reviewer #6: end-to-end wire-through to main() via --gc-terminals.

    These tests verify the argparse -> main() -> _gc_terminals path is intact,
    not just _gc_terminals in isolation. Catches flag-name typos, dest
    mismatches, and main-callback regressions that unit tests miss.
    """

    def test_gc_terminals_flag_invokes_close(self, monkeypatch, mock_mgr):
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-empty", "title": ""},
        ]}}
        monkeypatch.setattr("sys.argv", ["conductor.py", "--gc-terminals"])
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            from conductor import main as conductor_main
            conductor_main()
        closed = [c.args[0] for c in mock_mgr.close_terminal.call_args_list]
        assert "h-empty" in closed

    def test_gc_terminals_dry_run_invokes_no_close(self, monkeypatch, mock_mgr):
        mock_mgr._run_orca.return_value = {"result": {"terminals": [
            {"handle": "h-empty", "title": ""},
        ]}}
        monkeypatch.setattr(
            "sys.argv",
            ["conductor.py", "--gc-terminals", "--gc-terminals-dry-run"],
        )
        with patch("conductor.OrcaExecutionManager", return_value=mock_mgr):
            from conductor import main as conductor_main
            conductor_main()
        mock_mgr.close_terminal.assert_not_called()


