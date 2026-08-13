"""Contract tests for the standalone FirstMate/Orca crew lifecycle (U7)."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import crew_dispatch
from capabilities import resolve_capability
from crew_dispatch import CrewResult, CrewUnavailableError, dispatch_crew


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def sleep(self, seconds):
        self.value += seconds

    def now(self):
        return self.value


def configure_paths(monkeypatch, tmp_path):
    fm_home = tmp_path / "fm-home"
    state = fm_home / "state"
    data = fm_home / "data"
    state.mkdir(parents=True)
    data.mkdir(parents=True)
    monkeypatch.setattr(crew_dispatch, "FM_HOME", fm_home)
    monkeypatch.setattr(crew_dispatch, "STATE_DIR", state)
    monkeypatch.setattr(crew_dispatch, "DATA_DIR", data)
    monkeypatch.setattr(crew_dispatch, "CREW_RUNS_FILE", tmp_path / "crew_runs.json")
    return fm_home, state, data


def spawn_process(worktree_id="repo::/tmp/crew-worktree"):
    return subprocess.CompletedProcess(
        ["fm-spawn"],
        0,
        f"spawned fm-loop-20260811-120000-42 harness=hermes kind=ship "
        f"window=terminal worktree=/tmp/crew-worktree\n",
        "",
    )


def test_happy_path_reads_report_and_tears_down(monkeypatch, tmp_path):
    _, state, data = configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-42"
    (state / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/crew-worktree\n")
    (state / f"{crew_id}.status").write_text(
        "working: coding\n"
        "done: branch=fm/task-42 commit=abc123 base=main@def456\n"
    )
    report = data / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "# Deliverable\nDone.\n\n"
        "Branch: fm/task-42\nCommit: abc123\nBase: main@def456\n"
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, '{"removed": true}', "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=42,
        task_text="Fix the bug",
        project_dir=tmp_path / "school-project",
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )

    assert result == CrewResult(
        crew_id=crew_id,
        status="done",
        report_path=report,
        fallback_reason=None,
        teardown_ok=True,
        orca_worktree_id="repo::/tmp/crew-worktree",
    )
    spawn_args, spawn_kwargs = calls[0]
    args = spawn_args
    assert args[args.index("--mode") + 1] == "local-only"
    assert args[args.index("--yolo") + 1] == "on"
    assert args[args.index("--backend") + 1] == "orca"
    harness_arg = args[args.index("--harness") + 1]
    assert "hermes-fm-wrapper" in harness_arg
    assert "__OPINPUT__" in harness_arg
    assert "__BRIEF__" in harness_arg
    assert "--scout" not in args
    # fm-spawn must see the same FM_HOME/state/data this module uses, or it
    # resolves its own clone root and cannot find the brief (issue #49).
    env = spawn_kwargs["env"]
    assert env["FM_HOME"] == str(crew_dispatch.FM_HOME)
    assert env["FM_STATE_OVERRIDE"] == str(crew_dispatch.STATE_DIR)
    assert env["FM_DATA_OVERRIDE"] == str(crew_dispatch.DATA_DIR)
    assert calls[-1][0] == [
        "orca", "worktree", "rm", "--worktree", "id:repo::/tmp/crew-worktree",
        "--force", "--json",
    ]
    brief = (data / crew_id / "brief.md").read_text()
    assert "Fix the bug" in brief
    assert "report.md" in brief
    assert "local commit" in brief
    assert "branch, commit, and base identity" in brief
    # U8 handoff contract (issue #50): the brief must name the EXACT status
    # file the poller reads and the report path that survives teardown, or the
    # agent cannot append the terminal `done:` line. FM runtime paths are
    # embedded; the project checkout path must not leak into the brief.
    assert str(state / f"{crew_id}.status") in brief
    assert str(data / crew_id / "report.md") in brief
    assert "done: branch=<branch> commit=<commit> base=<base>" in brief
    assert str(tmp_path / "school-project") not in brief
    assert calls[-1][0] == [
        "orca", "worktree", "rm", "--worktree", "id:repo::/tmp/crew-worktree",
        "--force", "--json",
    ]
    runs = json.loads((tmp_path / "crew_runs.json").read_text())
    assert runs[-1]["status"] == "done"
    assert runs[-1]["issue_number"] == 42
    assert runs[-1]["orca_worktree_present"] is True
    assert "orca_worktree_id" not in runs[-1]
    assert str(tmp_path) not in json.dumps(runs[-1])


def test_artifact_identity_normalizes_markdown_wrappers():
    status = "done: branch=fm/task-58 commit=abc123 base=main@def456"
    report = (
        "- branch: `fm/task-58`\n"
        "- commit: `abc123`\n"
        "- base: `main@def456`\n"
    )

    assert crew_dispatch._artifact_identity(status) == {
        "branch": "fm/task-58",
        "commit": "abc123",
        "base": "main@def456",
    }
    assert crew_dispatch._artifact_identity(report) == crew_dispatch._artifact_identity(status)


def test_artifact_identity_reads_sectioned_hermes_report():
    status = "done: branch=fm/task-59 commit=abc123 base=main@def456"
    report = (
        "## Branch\n"
        "- `fm/task-59`\n\n"
        "## Commit\n"
        "- `abc123`\n\n"
        "## Base\n"
        "- Branch: `branben/fm-fm-task-59`\n"
        "- Commit: `main@def456`\n"
    )

    assert crew_dispatch._artifact_identity(report) == crew_dispatch._artifact_identity(status)


def test_artifact_identity_reads_bare_bullet_base_section():
    """U10 regression: a bare-bullet ``## Base`` (the brief's own report format)
    must be captured, not silently dropped."""
    status = (
        "done: branch=fm/fm-live-smoke-20260813-123335-700001 "
        "commit=0f03c7897162796ea0e56bc99b99dfbd30970cc9 base=origin/main"
    )
    report = (
        "## Branch\n"
        "- `fm/fm-live-smoke-20260813-123335-700001`\n\n"
        "## Commit\n"
        "- `0f03c7897162796ea0e56bc99b99dfbd30970cc9`\n\n"
        "## Base\n"
        "- `origin/main`\n\n"
        "## Isolation Check\n"
        "- `pwd -P` resolves to the disposable Orca worktree.\n"
    )

    assert crew_dispatch._artifact_identity(report) == crew_dispatch._artifact_identity(status)


def test_artifact_identity_keeps_nested_base_commit_style():
    """The nested ``## Base`` style (Branch:/Commit: labels) still sets base."""
    report = (
        "## Branch\n"
        "- `fm/task-60`\n\n"
        "## Commit\n"
        "- `abc123`\n\n"
        "## Base\n"
        "- Branch: `branben/fm-fm-task-60`\n"
        "- Commit: `main@def456`\n"
    )

    assert crew_dispatch._artifact_identity(report) == {
        "branch": "fm/task-60",
        "commit": "abc123",
        "base": "main@def456",
    }


def test_capability_bundle_reaches_firstmate_launch_contract(monkeypatch, tmp_path):
    _, state, data = configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260812-120000-43"
    (state / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/crew-worktree\n")
    (state / f"{crew_id}.status").write_text(
        "working: coding\n"
        "done: branch=fm/task-43 commit=abc123 base=main@def456\n"
    )
    report = data / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "# Deliverable\nDone.\n\n"
        "Branch: fm/task-43\nCommit: abc123\nBase: main@def456\n"
    )
    capability = resolve_capability(
        "python-testing", 30, task_role="coder", difficulty="medium"
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, '{"removed": true}', "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=43,
        task_text="Use the selected coder capability",
        project_dir=tmp_path / "school-project",
        cycle_session_id="loop-20260812-120000",
        capability=capability,
        timeout=1,
        poll_interval=0,
    )

    assert result.status == "done"
    assert result.capability["profile"] == "student-coder"
    spawn_args, spawn_kwargs = calls[0]
    env = spawn_kwargs["env"]
    assert env["FM_AGENT_TASK_ROLE"] == "coder"
    assert env["FM_AGENT_PROFILE"] == "student-coder"
    assert env["FM_AGENT_SKILL_ANCHORS"] == ",".join(capability.skills)
    assert env["FM_AGENT_ALLOWED_TOOLS"] == "python,testing,git"
    assert env["FM_AGENT_TOOLSETS"] == ",".join(capability.hermes_toolsets)
    assert "FM_AGENT_CAPABILITY_FILE" not in env
    assert "FM_AGENT_PERSONA_FILE" not in env
    brief = (data / crew_id / "brief.md").read_text()
    assert "## Capability contract" in brief
    assert "Hermes profile: student-coder" in brief
    assert "Hermes toolsets:" in brief
    payload = json.loads((data / crew_id / "capability.json").read_text())
    assert payload["task_role"] == "coder"
    assert payload["hermes_toolsets"] == list(capability.hermes_toolsets)
    assert "school-project" not in json.dumps(payload)


def test_empty_capability_tool_policy_fails_closed(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    capability = resolve_capability(
        "python-testing", 30, task_role="coder", difficulty="medium"
    )
    invalid = replace(capability, hermes_toolsets=())
    monkeypatch.setattr(
        crew_dispatch,
        "_spawn",
        lambda *args, **kwargs: pytest.fail("empty policy must not spawn"),
    )

    with pytest.raises(CrewUnavailableError, match="no Hermes toolsets"):
        dispatch_crew(
            issue_number=44,
            task_text="Reject an empty policy",
            project_dir=tmp_path,
            cycle_session_id="loop-20260812-120000",
            capability=invalid,
        )


def test_spawn_timeout_raises_typed_error(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)

    def timeout_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))

    monkeypatch.setattr(crew_dispatch, "_run", timeout_run)
    with pytest.raises(CrewUnavailableError):
        dispatch_crew(
            issue_number=6,
            task_text="Timed spawn",
            project_dir=tmp_path,
            cycle_session_id="loop-20260811-120000",
        )


def test_spawn_failure_raises_typed_error(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)

    def fail_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 7, "", "gateway unavailable")

    monkeypatch.setattr(crew_dispatch, "_run", fail_run)
    with pytest.raises(CrewUnavailableError, match="gateway unavailable"):
        dispatch_crew(
            issue_number=7,
            task_text="Do work",
            project_dir=tmp_path,
            cycle_session_id="loop-20260811-120000",
        )


def test_spawn_failure_persists_bounded_redacted_error(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    token = "github_pat_1234567890abcdefghijklmnopqrstuvwxyz"
    bearer = "Bearer abcdefghijklmnopqrstuvwxyz123456"
    private_path = "/Users/another-user/.hermes/private"

    def fail_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            7,
            "",
            f"gateway unavailable token={token} auth={bearer} path={private_path}",
        )

    monkeypatch.setattr(crew_dispatch, "_run", fail_run)
    with pytest.raises(CrewUnavailableError, match="gateway unavailable"):
        dispatch_crew(
            issue_number=70,
            task_text="Record the failure",
            project_dir=tmp_path,
            cycle_session_id="loop-20260811-120000",
        )

    runs = json.loads((tmp_path / "crew_runs.json").read_text())
    record = runs[-1]
    assert record["status"] == "spawn_failed"
    assert record["spawn_error"].startswith("SpawnError: returncode=7:")
    assert "gateway unavailable" in record["spawn_error"]
    assert token not in record["spawn_error"]
    assert bearer not in record["spawn_error"]
    assert "github_pat_" not in record["spawn_error"]
    assert "another-user" not in record["spawn_error"]
    assert str(Path.home()) not in record["spawn_error"]
    assert "/Users/" not in record["spawn_error"]
    assert len(record["spawn_error"]) <= crew_dispatch.MAX_SPAWN_ERROR_CHARS


def test_spawn_timeout_persists_type_without_command_arguments(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)

    def timeout_run(args, **kwargs):
        raise subprocess.TimeoutExpired(
            args,
            kwargs.get("timeout", 0),
            stderr="provider token=github_pat_1234567890",
        )

    monkeypatch.setattr(crew_dispatch, "_run", timeout_run)
    with pytest.raises(CrewUnavailableError, match="timed out"):
        dispatch_crew(
            issue_number=71,
            task_text="Record the timeout",
            project_dir=tmp_path,
            cycle_session_id="loop-20260811-120000",
        )

    runs = json.loads((tmp_path / "crew_runs.json").read_text())
    record = runs[-1]
    assert record["status"] == "spawn_failed"
    assert record["spawn_error"] == "TimeoutExpired: spawn subprocess timed out"
    assert "github_pat_" not in json.dumps(record)
    assert str(tmp_path) not in json.dumps(record)


def test_metadata_is_retried_after_spawn_before_teardown(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-17"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("done: branch=fm/task-9 commit=abc123 base=main@def456\n")
    calls = []
    reads = 0

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    real_read_meta = crew_dispatch._read_meta

    def delayed_meta(path):
        nonlocal reads
        reads += 1
        if reads == 1:
            return {}
        path.write_text("orca_worktree_id=repo::/tmp/late-worktree\n")
        return real_read_meta(path)

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    monkeypatch.setattr(crew_dispatch, "_read_meta", delayed_meta)
    result = dispatch_crew(
        issue_number=17,
        task_text="Delayed metadata",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
        metadata_timeout=1,
        metadata_poll_interval=1,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    assert result.orca_worktree_id == "repo::/tmp/late-worktree"
    assert calls[-1][0][4] == "id:repo::/tmp/late-worktree"


def test_metadata_retry_is_bounded_with_non_advancing_clock(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-18"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("done: branch=fm/task-9 commit=abc123 base=main@def456\n")

    def fake_run(args, **kwargs):
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=18,
        task_text="No metadata",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=0.01,
        metadata_timeout=0.01,
        metadata_poll_interval=0,
        now_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
    )
    assert result.orca_worktree_id is None
    assert result.teardown_ok is False


def test_timeout_returns_timeout_without_report(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-8"
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text(
        "orca_worktree_id=repo::/tmp/worktree\n"
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=8,
        task_text="Wait",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=2,
        poll_interval=1,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    assert result.status == "timeout"
    assert result.report_path is None
    assert result.fallback_reason == "timeout"
    assert result.teardown_ok is True


def test_mismatched_status_and_report_identity_is_not_success(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-24"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text(
        "done: branch=fm/task-24 commit=abc123 base=main@def456\n"
    )
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("Branch: fm/task-other\nCommit: abc123\nBase: main@def456\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )

    result = dispatch_crew(
        issue_number=24,
        task_text="Mismatched identity",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.report_path is None
    assert result.fallback_reason == "artifact_identity_mismatch"


def test_done_without_worktree_identity_is_not_success(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-21"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("done: branch=fm/task-9 commit=abc123 base=main@def456\n")
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("done")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )

    result = dispatch_crew(
        issue_number=21,
        task_text="Missing identity",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.report_path is None
    assert result.fallback_reason == "artifact_identity_missing"


def test_empty_report_is_not_a_deliverable(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-19"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text(
        "done: branch=fm/task-19 commit=abc123 base=main@def456\n"
    )
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("  \n")

    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )
    result = dispatch_crew(
        issue_number=19,
        task_text="Empty report",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.report_path is None
    assert result.fallback_reason == "report_empty"


def test_oversized_report_is_not_a_deliverable(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-22"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("done: branch=fm/task-9 commit=abc123 base=main@def456\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("x" * (crew_dispatch.MAX_REPORT_BYTES + 1))
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )

    result = dispatch_crew(
        issue_number=22,
        task_text="Large report",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.report_path is None
    assert result.fallback_reason == "report_too_large"


def test_missing_spawn_executable_raises_typed_error(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("fm-spawn.sh")),
    )
    with pytest.raises(CrewUnavailableError, match="fm-spawn.sh"):
        dispatch_crew(
            issue_number=14,
            task_text="Missing executable",
            project_dir=tmp_path,
            cycle_session_id="loop-20260811-120000",
        )


def test_failed_status_is_terminal(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-9"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("failed: tests failed\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text(
        "orca_worktree_id=repo::/tmp/worktree\n"
    )

    def fake_run(args, **kwargs):
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=9,
        task_text="Fail",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.fallback_reason == "crew_failed"


def test_needs_decision_uses_blocked_grace(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-15"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("needs-decision: human\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )
    result = dispatch_crew(
        issue_number=15,
        task_text="Needs decision",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=100,
        poll_interval=10,
        blocked_grace=20,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert result.status == "blocked"
    assert result.fallback_reason == "blocked"


def test_unknown_status_keeps_polling_until_timeout(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-16"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("checkpointing: still working\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )
    result = dispatch_crew(
        issue_number=16,
        task_text="Unknown status",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=20,
        poll_interval=10,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert result.status == "timeout"
    assert result.fallback_reason == "timeout"


def test_crew_ids_include_issue_and_cycle(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    assert crew_dispatch._crew_id("loop-a", 1) != crew_dispatch._crew_id("loop-b", 1)
    assert crew_dispatch._crew_id("loop-a", 1) != crew_dispatch._crew_id("loop-a", 2)


def test_wrapper_failed_handshake_is_terminal_failure(monkeypatch, tmp_path):
    """U10: the wrapper's deterministic handshake (Hermes exited without a
    terminal status) reaches dispatch_crew as an immediate failed result, so the
    supervisor never polls an idle session to the full timeout."""
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260812-120000-30"
    # The scout went idle: no report, no done:/failed: line from the crew. The
    # wrapper appends the handshake line when Hermes exits.
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text(
        "working: reconnaissance\n"
        "failed: hermes-exit-0-no-terminal-status\n"
    )
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )

    result = dispatch_crew(
        issue_number=30,
        task_text="Idle scout",
        project_dir=tmp_path,
        cycle_session_id="loop-20260812-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.report_path is None
    assert result.fallback_reason == "crew_failed"
    assert result.teardown_ok is True
    runs = json.loads((tmp_path / "crew_runs.json").read_text())
    assert runs[-1]["status"] == "failed"
    assert runs[-1]["fallback_reason"] == "crew_failed"


def test_supervisor_unexpected_failure_records_terminal_state(monkeypatch, tmp_path):
    """U10: an unexpected supervisor-side failure during polling/collection still
    lands a deterministic terminal record (never a stale `running`), so the
    next-cycle sweep and the bridge fallback see a bounded failure."""
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260812-120000-31"
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )

    def boom(*args, **kwargs):
        raise OSError("status channel unreadable")

    monkeypatch.setattr(crew_dispatch, "_read_status_detail", boom)
    result = dispatch_crew(
        issue_number=31,
        task_text="Supervisor failure",
        project_dir=tmp_path,
        cycle_session_id="loop-20260812-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.fallback_reason == "supervisor_unexpected"
    assert result.report_path is None
    runs = json.loads((tmp_path / "crew_runs.json").read_text())
    assert runs[-1]["status"] == "failed"
    assert runs[-1]["fallback_reason"] == "supervisor_unexpected"
    assert runs[-1]["teardown_ok"] is True


def test_terminal_state_persisted_before_teardown(monkeypatch, tmp_path):
    """U10: the outcome record lands in the registry BEFORE teardown, so a
    cleanup failure can never hide or lose the terminal result."""
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260812-120000-32"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text(
        "done: branch=fm/task-32 commit=abc123 base=main@def456\n"
    )
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("done\nBranch: fm/task-32\nCommit: abc123\nBase: main@def456\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )
    order = []
    real_update = crew_dispatch._update_run

    def ordered_update(crew_id, updates, path=None):
        order.append((crew_id, updates))
        real_update(crew_id, updates, path)

    monkeypatch.setattr(crew_dispatch, "_update_run", ordered_update)
    monkeypatch.setattr(crew_dispatch, "teardown_worktree", lambda _: order.append("teardown") or False)

    result = dispatch_crew(
        issue_number=32,
        task_text="Ordering",
        project_dir=tmp_path,
        cycle_session_id="loop-20260812-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "done"
    assert result.teardown_ok is False
    # The terminal outcome is persisted before teardown is attempted; the
    # teardown_ok update follows cleanup.
    terminal_update = next(u for _, u in order if u.get("status") == "done")
    assert order.index((crew_id, terminal_update)) < order.index("teardown")
    runs = json.loads((tmp_path / "crew_runs.json").read_text())
    assert runs[-1]["status"] == "done"
    assert runs[-1]["teardown_ok"] is False
    assert runs[-1]["report_path"] == f"{crew_id}/report.md"


def test_blocked_grace_expires_as_blocked(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-10"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("blocked: waiting\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text(
        "orca_worktree_id=repo::/tmp/worktree\n"
    )

    def fake_run(args, **kwargs):
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=10,
        task_text="Blocked",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=100,
        poll_interval=10,
        blocked_grace=20,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert result.status == "blocked"
    assert result.fallback_reason == "blocked"


def test_blocked_then_resolved_continues_to_done(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-11"
    status = crew_dispatch.STATE_DIR / f"{crew_id}.status"
    status.write_text("blocked: waiting\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text(
        "orca_worktree_id=repo::/tmp/worktree\n"
    )
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "resolved\nBranch: fm/task-11\nCommit: abc123\nBase: main@def456\n"
    )
    reads = 0

    def fake_run(args, **kwargs):
        nonlocal reads
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_read_status_detail(path):
        nonlocal reads
        reads += 1
        if reads == 1:
            return "blocked", "waiting"
        status.write_text("resolved: continuing\ndone: branch=fm/task-11 commit=abc123 base=main@def456\n")
        return "done", "branch=fm/task-11 commit=abc123 base=main@def456"

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    monkeypatch.setattr(crew_dispatch, "_read_status_detail", fake_read_status_detail)
    result = dispatch_crew(
        issue_number=11,
        task_text="Resolve",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=100,
        poll_interval=0,
        blocked_grace=20,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert result.status == "done"
    assert result.report_path == report


def test_paused_waits_until_timeout_not_blocked(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    clock = FakeClock()
    crew_id = "fm-loop-20260811-120000-12"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("paused: external\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text(
        "orca_worktree_id=repo::/tmp/worktree\n"
    )

    def fake_run(args, **kwargs):
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=12,
        task_text="Paused",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=20,
        poll_interval=10,
        blocked_grace=1,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    assert result.status == "timeout"
    assert result.fallback_reason == "timeout"


def test_missing_report_is_done_with_warning(monkeypatch, tmp_path, caplog):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-13"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text("done: branch=fm/task-9 commit=abc123 base=main@def456\n")
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text(
        "orca_worktree_id=repo::/tmp/worktree\n"
    )

    def fake_run(args, **kwargs):
        if args[0].endswith("fm-spawn.sh"):
            return spawn_process()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    result = dispatch_crew(
        issue_number=13,
        task_text="No report",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    assert result.status == "failed"
    assert result.report_path is None
    assert result.fallback_reason == "report_missing"
    assert "without report.md" in caplog.text


def test_malformed_stale_entry_is_preserved(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    path = tmp_path / "crew_runs.json"
    path.write_text(json.dumps([{
        "crew_id": "unknown",
        "issue_number": 99,
        "status": "running",
        "orca_worktree_id": "repo::/tmp/unknown",
    }]))
    calls = []
    monkeypatch.setattr(
        crew_dispatch,
        "teardown_worktree",
        lambda worktree_id: calls.append(worktree_id) or True,
    )

    assert crew_dispatch.sweep_stale_runs(now=2000000000, stale_after=60, path=path) == 0
    assert calls == []
    assert json.loads(path.read_text())[0]["crew_id"] == "unknown"


def test_registry_report_path_is_relative(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-20"
    (crew_dispatch.STATE_DIR / f"{crew_id}.status").write_text(
        "done: branch=fm/task-20 commit=abc123 base=main@def456\n"
    )
    (crew_dispatch.STATE_DIR / f"{crew_id}.meta").write_text("orca_worktree_id=repo::/tmp/worktree\n")
    report = crew_dispatch.DATA_DIR / crew_id / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("done\nBranch: fm/task-20\nCommit: abc123\nBase: main@def456\n")
    monkeypatch.setattr(
        crew_dispatch,
        "_run",
        lambda args, **kwargs: spawn_process() if args[0].endswith("fm-spawn.sh") else subprocess.CompletedProcess(args, 0, "", ""),
    )

    dispatch_crew(
        issue_number=20,
        task_text="Portable record",
        project_dir=tmp_path,
        cycle_session_id="loop-20260811-120000",
        timeout=1,
        poll_interval=0,
    )
    record = json.loads((tmp_path / "crew_runs.json").read_text())[-1]
    assert record["report_path"] == f"{crew_id}/report.md"
    assert record["orca_worktree_present"] is True
    assert "orca_worktree_id" not in record
    assert str(tmp_path) not in json.dumps(record)


def test_stale_sweep_uses_local_sidecar_for_portable_registry(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    crew_id = "fm-loop-20260811-120000-sidecar"
    (tmp_path / "crew_runs.json").write_text(json.dumps([{
        "crew_id": crew_id,
        "issue_number": 23,
        "status": "running",
        "orca_worktree_present": True,
        "started_at": "2020-01-01T00:00:00+00:00",
    }]))
    sidecar = crew_dispatch.DATA_DIR / crew_id / "orca_worktree_id"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("repo::/tmp/sidecar-worktree\n")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"removed": true}', "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    assert crew_dispatch.sweep_stale_runs(now=2000000000, stale_after=60) == 1
    assert calls == [[
        "orca", "worktree", "rm", "--worktree", "id:repo::/tmp/sidecar-worktree",
        "--force", "--json",
    ]]
    assert json.loads((tmp_path / "crew_runs.json").read_text()) == []


def test_stale_running_sweep_removes_worktree(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    (tmp_path / "crew_runs.json").write_text(json.dumps([
        {
            "crew_id": "old",
            "issue_number": 1,
            "status": "running",
            "orca_worktree_id": "repo::/tmp/old",
            "started_at": "2020-01-01T00:00:00+00:00",
        }
    ]))
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"removed": true}', "")

    monkeypatch.setattr(crew_dispatch, "_run", fake_run)
    crew_dispatch.sweep_stale_runs(now=2000000000, stale_after=60)
    assert calls == [[
        "orca", "worktree", "rm", "--worktree", "id:repo::/tmp/old",
        "--force", "--json",
    ]]
    assert json.loads((tmp_path / "crew_runs.json").read_text()) == []


def test_teardown_failure_is_nonfatal(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    calls = []

    def fail_cleanup(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "", "refused")

    monkeypatch.setattr(crew_dispatch, "_run", fail_cleanup)
    assert crew_dispatch.teardown_worktree("repo::/tmp/worktree") is False
    assert calls[0][1]["timeout"] == 30


def test_teardown_timeout_is_nonfatal(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)

    def timeout_cleanup(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))

    monkeypatch.setattr(crew_dispatch, "_run", timeout_cleanup)
    assert crew_dispatch.teardown_worktree("repo::/tmp/worktree") is False


def test_stale_entry_is_retained_when_cleanup_fails(monkeypatch, tmp_path):
    configure_paths(monkeypatch, tmp_path)
    path = tmp_path / "crew_runs.json"
    path.write_text(json.dumps([{
        "crew_id": "old",
        "issue_number": 1,
        "status": "running",
        "orca_worktree_id": "repo::/tmp/old",
        "started_at": "2020-01-01T00:00:00+00:00",
    }]))
    monkeypatch.setattr(crew_dispatch, "teardown_worktree", lambda _: False)
    assert crew_dispatch.sweep_stale_runs(now=2000000000, stale_after=60, path=path) == 0
    runs = json.loads(path.read_text())
    assert runs[0]["crew_id"] == "old"
    assert runs[0]["cleanup_failed"] is True
