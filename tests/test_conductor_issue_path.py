"""Regression tests for the real GitHub issue async path."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import conductor


def test_issue_task_shape_is_conservative_new_implementation():
    shape = conductor._issue_task_shape({"title": "Fix a bug"})

    assert shape["is_new_implementation"] is True
    assert shape["complexity"] == 1
    assert shape["has_failed_gate"] is False


def test_persist_issue_route_writes_full_contract():
    writes = []

    def writer(bead, repo, **fields):
        writes.append((bead, repo, fields))
        return fields

    with patch.object(conductor, "locked_update_bookbag", side_effect=writer):
        route = conductor._persist_issue_route(
            "bead-1",
            "branben/school-core",
            conductor._issue_task_shape({"title": "Fix a bug"}),
        )

    assert route["logged"] is True
    bead, repo, fields = writes[0]
    assert (bead, repo) == ("bead-1", "branben/school-core")
    assert fields["chosen_skill"] == route["chosen_skill"]
    assert fields["primary_workflow"] == route["primary_workflow"]
    assert fields["overlays"] == route["overlays"]
    assert fields["discarded_overlays"] == route["discarded_overlays"]
    assert fields["curiosity_required"] is False
    assert fields["human_gate_required"] is False


def test_persist_issue_route_blocks_signal_when_writer_fails():
    with patch.object(conductor, "locked_update_bookbag", return_value=None):
        with pytest.raises(RuntimeError, match="ready signal withheld"):
            conductor._persist_issue_route(
                "bead-1",
                "branben/school-core",
                conductor._issue_task_shape({"title": "Fix a bug"}),
            )


def test_prepare_issue_context_fails_closed_on_clone_failure():
    with patch("repo_reader.clone_repo", side_effect=RuntimeError("network down")):
        with pytest.raises(RuntimeError, match="could not clone target repo"):
            conductor._prepare_issue_context("branben/school-core", "Fix a bug")


def test_record_issue_dispatch_failure_without_bead_writes_last_run_and_alerts(tmp_path):
    last_run = tmp_path / "last_run.json"
    with (
        patch.object(conductor, "__file__", str(tmp_path / "conductor.py")),
        patch.object(conductor, "notify_issue_alert") as notify,
    ):
        # The helper's path is module-relative, so create the expected data dir
        # beside the patched module path.
        (tmp_path / "data").mkdir(exist_ok=True)
        conductor._record_issue_dispatch_failure(
            None,
            "branben/school-core",
            "network down",
            issue_number=42,
            issue_title="Fix the issue",
        )

    last_run = tmp_path / "data" / "last_run.json"
    assert last_run.exists()
    record = last_run.read_text()
    assert '"status": "school-failed"' in record
    assert '"issue": 42' in record
    notify.assert_called_once_with(
        42,
        "Fix the issue",
        "school-failed",
        error="network down",
        repo="branben/school-core",
        attempt=1,
    )


def test_prepare_issue_context_keeps_clone_when_reader_fails(tmp_path):
    with (
        patch("repo_reader.clone_repo", return_value=tmp_path),
        patch("repo_reader.build_codebase_context", side_effect=RuntimeError("reader down")),
    ):
        target_path, context = conductor._prepare_issue_context("branben/school-core", "Fix a bug")

    assert target_path == tmp_path
    assert context == ""


def test_sync_issue_entrypoint_forwards_target_clone_and_context(tmp_path):
    args = SimpleNamespace(
        issue="branben/school-core#42",
        async_mode=False,
        agent=None,
    )
    issue = {
        "title": "Fix the issue",
        "prompt": "Fix the issue",
        "domain": "code-implementation",
        "difficulty": "medium",
        "state": "ready-for-agent",
    }
    captured = {}

    def capture_run(args, store):
        captured.update({
            "task": args.task,
            "repo": args.repo,
            "repo_path": args.repo_path,
            "context_chars": args.codebase_context_chars,
            "task_shape": args.task_shape,
        })

    with (
        patch.object(conductor, "fetch_single_issue", return_value=issue),
        patch.object(conductor, "_resolve_agent", return_value="coder"),
        patch.object(conductor, "_prepare_issue_context", return_value=(tmp_path, "Repository context")),
        patch.object(conductor, "_run_single_task", side_effect=capture_run),
    ):
        conductor._run_issue(args, MagicMock())

    assert captured["repo"] == "branben/school-core"
    assert captured["repo_path"] == tmp_path
    assert captured["context_chars"] == len("Repository context")
    assert "Repository context" in captured["task"]
    assert captured["task_shape"]["is_new_implementation"] is True


def test_sync_issue_returned_failure_is_durable_and_alerted(tmp_path):
    args = SimpleNamespace(
        issue="branben/school-core#42",
        async_mode=False,
        agent=None,
    )
    issue = {
        "title": "Fix the issue",
        "prompt": "Fix the issue",
        "domain": "code-implementation",
        "difficulty": "medium",
        "state": "ready-for-agent",
    }

    with (
        patch.object(conductor, "fetch_single_issue", return_value=issue),
        patch.object(conductor, "_resolve_agent", return_value="coder"),
        patch.object(conductor, "_prepare_issue_context", return_value=(tmp_path, "")),
        patch.object(
            conductor,
            "_run_single_task",
            return_value={"status": "error", "error": "gateway hiccup"},
        ),
        patch.object(conductor, "_record_issue_dispatch_failure") as record,
    ):
        conductor._run_issue(args, MagicMock())

    record.assert_called_once()
    failure_args, failure_kwargs = record.call_args
    assert failure_args[:2] == (None, "branben/school-core")
    assert str(failure_args[2]) == "gateway hiccup"
    assert failure_kwargs == {"issue_number": 42, "issue_title": "Fix the issue"}


def test_sync_issue_dispatch_failure_is_durable_and_alerted(tmp_path):
    args = SimpleNamespace(
        issue="branben/school-core#42",
        async_mode=False,
        agent=None,
    )
    issue = {
        "title": "Fix the issue",
        "prompt": "Fix the issue",
        "domain": "code-implementation",
        "difficulty": "medium",
        "state": "ready-for-agent",
    }

    with (
        patch.object(conductor, "fetch_single_issue", return_value=issue),
        patch.object(conductor, "_resolve_agent", return_value="coder"),
        patch.object(conductor, "_prepare_issue_context", return_value=(tmp_path, "")),
        patch.object(conductor, "_run_single_task", side_effect=RuntimeError("route write failed")),
        patch.object(conductor, "_record_issue_dispatch_failure") as record,
    ):
        conductor._run_issue(args, MagicMock())

    record.assert_called_once()
    failure_args, failure_kwargs = record.call_args
    assert failure_args[:2] == (None, "branben/school-core")
    assert str(failure_args[2]) == "route write failed"
    assert failure_kwargs == {"issue_number": 42, "issue_title": "Fix the issue"}


def test_principal_dispatch_does_not_route_or_signal_failed_leaf(tmp_path):
    events = []

    def fake_run_leaf(**kwargs):
        events.append("run_leaf")
        return {"status": "error", "error": "gateway hiccup", "bead": "failed-bead"}

    with (
        patch.object(conductor, "run_leaf", side_effect=fake_run_leaf),
        patch.object(conductor, "_persist_issue_route", side_effect=lambda *args, **kwargs: events.append("route")),
        patch.object(conductor, "BookbagSignal") as signal,
    ):
        result = conductor._principal_dispatch(
            task="Fix the issue",
            role="coder",
            domain="code-implementation",
            difficulty="medium",
            store=MagicMock(),
            repo="branben/school-core",
            repo_path=tmp_path,
            task_shape=conductor._issue_task_shape({"title": "Fix the issue"}),
        )

    assert result["status"] == "error"
    assert events == ["run_leaf"]
    signal.assert_not_called()


def test_sync_dispatch_persists_route_before_ready_and_uses_target_clone(tmp_path):
    events = []
    fake_result = {
        "status": "success",
        "agent": "coder",
        "domain": "code-implementation",
        "bead": "sync-bead-1",
    }
    route = {
        "chosen_skill": "rank2_ce_workflow",
        "primary_workflow": "rank2_ce_workflow",
        "overlays": [],
        "discarded_overlays": [],
        "curiosity_required": False,
        "human_gate_required": False,
        "logged": True,
    }

    class FakeSignal:
        def __init__(self, bead, repo):
            events.append(("signal_init", bead, repo))

        def ready(self):
            events.append("signal_ready")

    def fake_run_leaf(**kwargs):
        events.append("run_leaf")
        assert kwargs["repo_path"] == tmp_path
        assert kwargs["signal_ready"] is False
        return fake_result

    def fake_persist(bead, repo, task_shape, strict=True):
        events.append("route")
        return route

    with (
        patch.object(conductor, "run_leaf", side_effect=fake_run_leaf),
        patch.object(conductor, "_persist_issue_route", side_effect=fake_persist),
        patch.object(conductor, "BookbagSignal", FakeSignal),
    ):
        out = conductor._principal_dispatch(
            task="Fix the issue",
            role="coder",
            domain="code-implementation",
            difficulty="medium",
            store=MagicMock(),
            repo="branben/school-core",
            repo_path=tmp_path,
            task_shape=conductor._issue_task_shape({"title": "Fix the issue"}),
        )

    assert out["status"] == "success"
    assert events == [
        "run_leaf",
        "route",
        ("signal_init", "sync-bead-1", "branben/school-core"),
        "signal_ready",
    ]


def test_async_teacher_boot_exception_records_pre_bead_failure():
    args = SimpleNamespace(task="Fix", domain="code-implementation", difficulty="medium", handoff_timeout=1)
    with (
        patch.object(conductor, "_boot_teachers", side_effect=RuntimeError("orca down")),
        patch.object(conductor, "_record_issue_dispatch_failure") as record,
    ):
        conductor._run_issue_async(
            args,
            MagicMock(),
            "coder",
            target_repo="branben/school-core",
            issue_number=42,
            issue_title="Fix",
        )

    record.assert_called_once()
    args, kwargs = record.call_args
    assert args[:2] == (None, "branben/school-core")
    assert str(args[2]) == "orca down"
    assert kwargs == {"issue_number": 42, "issue_title": "Fix"}


def test_signal_write_then_raise_does_not_overwrite_failure(tmp_path):
    args = SimpleNamespace(task="Fix", domain="code-implementation", difficulty="medium", handoff_timeout=1)
    teachers = {"cto": SimpleNamespace(worktree_name="cto"), "coo": SimpleNamespace(worktree_name="coo")}
    leaf = MagicMock()
    leaf.bead = "bead-signal-race"
    leaf.worktree_path = str(tmp_path / "student")
    leaf.run_via_hermes.return_value = {"status": "success", "response": "fixed"}
    route = {
        "chosen_skill": "rank2_ce_workflow", "primary_workflow": "rank2_ce_workflow",
        "overlays": [], "discarded_overlays": [], "curiosity_required": False,
        "human_gate_required": False, "logged": True,
    }
    signal = MagicMock()
    signal.ready.side_effect = RuntimeError("wrapper reported late")
    signal.check.return_value = True

    def real_signal_call():
        conductor.BookbagSignal(leaf.bead, repo="branben/school-core").ready()

    leaf.signal_ready.side_effect = real_signal_call

    with (
        patch.object(conductor, "load_principal_soul", return_value="Principal"),
        patch.object(conductor, "_boot_teachers", return_value=teachers),
        patch.object(conductor, "_prepare_issue_context", return_value=(tmp_path, "")),
        patch.object(conductor, "StudentLeaf", return_value=leaf),
        patch.object(conductor, "_persist_issue_route", return_value=route),
        patch.object(conductor, "BookbagSignal", return_value=signal),
        patch.object(conductor, "_record_issue_dispatch_failure") as record,
        patch.object(conductor, "_shutdown_teachers"),
    ):
        conductor._run_issue_async(args, MagicMock(), "coder", target_repo="branben/school-core", issue_number=42, issue_title="Fix")

    signal.ready.assert_called_once()
    signal.check.assert_called_once()
    record.assert_not_called()


def test_async_issue_failure_persists_terminal_status_and_alerts(tmp_path):
    args = SimpleNamespace(
        task="Fix the issue",
        domain="code-implementation",
        difficulty="medium",
        handoff_timeout=1,
    )
    teachers = {
        "cto": SimpleNamespace(worktree_name="teacher-cto"),
        "coo": SimpleNamespace(worktree_name="teacher-coo"),
    }
    leaf = MagicMock()
    leaf.bead = "coder-code-implementation-failure1"
    leaf.run_via_hermes.return_value = {
        "status": "error",
        "error": "gateway hiccup",
    }
    writes = []

    def record_write(bead, repo, **fields):
        writes.append((bead, repo, fields))
        return fields

    with (
        patch.object(conductor, "load_principal_soul", return_value="Principal"),
        patch.object(conductor, "_boot_teachers", return_value=teachers),
        patch("repo_reader.clone_repo", return_value=tmp_path / "target"),
        patch("repo_reader.build_codebase_context", return_value="Repository context"),
        patch.object(conductor, "StudentLeaf", return_value=leaf),
        patch.object(conductor, "locked_update_bookbag", side_effect=record_write),
        patch.object(conductor, "notify_issue_alert") as notify,
        patch.object(conductor, "_shutdown_teachers"),
    ):
        conductor._run_issue_async(
            args,
            MagicMock(),
            "coder",
            target_repo="branben/school-core",
            issue_number=42,
            issue_title="Fix the issue",
        )

    assert writes
    bead, repo, fields = writes[-1]
    assert (bead, repo) == (leaf.bead, "branben/school-core")
    assert fields["dispatch_status"] == "school-failed"
    assert fields["dispatch_error"] == "gateway hiccup"
    assert fields["dispatch_failed_at"]
    notify.assert_called_once_with(
        42,
        "Fix the issue",
        "school-failed",
        error="gateway hiccup",
        repo="branben/school-core",
        attempt=1,
    )
    leaf.signal_ready.assert_not_called()


def test_async_issue_path_persists_route_after_context_enrichment(tmp_path):
    args = SimpleNamespace(
        task="Fix the issue",
        domain="code-implementation",
        difficulty="medium",
        handoff_timeout=1,
    )
    teachers = {
        "cto": SimpleNamespace(worktree_name="teacher-cto"),
        "coo": SimpleNamespace(worktree_name="teacher-coo"),
    }
    leaf = MagicMock()
    leaf.bead = "coder-code-implementation-test1234"
    leaf.worktree_path = str(tmp_path / "student")
    leaf.run_via_hermes.return_value = {
        "status": "success",
        "agent": "coder",
        "domain": "code-implementation",
        "response": "fixed",
    }
    route = {
        "chosen_skill": "rank2_ce_workflow",
        "primary_workflow": "rank2_ce_workflow",
        "overlays": ["security-and-hardening"],
        "discarded_overlays": [],
        "curiosity_required": False,
        "human_gate_required": False,
        "logged": True,
    }
    bag = {
        "cto_score": 80,
        "coo_score": 80,
        "cto_findings": [],
        "coo_findings": [],
        "findings": [],
    }
    target_repo = "branben/school-core"
    task_shape = conductor._issue_task_shape({"title": args.task})

    with (
        patch.object(conductor, "load_principal_soul", return_value="Principal"),
        patch.object(conductor, "_boot_teachers", return_value=teachers),
        patch("repo_reader.clone_repo", return_value=tmp_path / "target"),
        patch("repo_reader.build_codebase_context", return_value="Repository context"),
        patch.object(conductor, "StudentLeaf", return_value=leaf),
        patch.object(conductor, "_persist_issue_route", return_value=route) as persist_route,
        patch.object(conductor, "run_entire_review", return_value={"status": "skipped", "findings": []}),
        patch.object(conductor, "wait_for_verdicts", return_value=("PASS", "PASS")),
        patch.object(conductor, "read_bookbag", return_value=bag),
        patch.object(conductor, "_persist_acceptance", return_value=True),
        patch.object(conductor, "_validate_verdict"),
        patch.object(conductor, "_compute_task_score", return_value=80),
        patch.object(conductor, "evaluate_and_update", return_value={"old_score": 70, "new_score": 80}),
        patch.object(conductor, "notify_verdict"),
        patch.object(conductor, "_shutdown_teachers"),
    ):
        conductor._run_issue_async(
            args,
            MagicMock(),
            "coder",
            target_repo=target_repo,
            task_shape=task_shape,
        )

    assert "Repository context" in leaf.write_brief.call_args.args[0]
    assert "Repository context" in leaf.run_via_hermes.call_args.args[0]
    persist_route.assert_called_once_with(leaf.bead, target_repo, task_shape)
    assert leaf.signal_ready.called
    assert leaf.dispose.called
