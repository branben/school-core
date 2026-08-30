"""Contract tests for the repository-owned FirstMate Hermes wrapper."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "hermes-fm-wrapper"


def test_wrapper_submits_noninteractive_query_with_capability_context(tmp_path):
    profile = tmp_path / "config" / "profiles" / "student-coder" / "SOUL.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("PERSONA_MARKER\n", encoding="utf-8")

    capture = tmp_path / "hermes-args.txt"
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\0' \"$@\" > \"$HERMES_ARGS_CAPTURE\"\n"
        "printf 'stub-ok\\n'\n",
        encoding="utf-8",
    )
    hermes.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "HERMES": str(hermes),
        "HERMES_ARGS_CAPTURE": str(capture),
        "FM_AGENT_PROFILE": "student-coder",
        "FM_AGENT_TOOLSETS": "file,terminal,skills",
        "FM_AGENT_SKILL_ANCHORS": "TDD Chicago School",
        "FM_AGENT_MAX_TURNS": "3",
    })
    result = subprocess.run(
        [str(WRAPPER), "BRIEF_MARKER"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "stub-ok" in result.stdout
    args = capture.read_bytes().split(b"\0")[:-1]
    args = [item.decode() for item in args]
    assert "chat" in args
    assert "--query" in args
    query = args[args.index("--query") + 1]
    assert "PERSONA_MARKER" in query
    assert "BRIEF_MARKER" in query
    assert "TDD Chicago School" in query
    assert "--yolo" in args
    assert "--quiet" in args
    assert args[args.index("--max-turns") + 1] == "3"


def test_wrapper_rejects_invalid_turn_policy(tmp_path):
    env = os.environ.copy()
    env.update({"HERMES": "/bin/true", "FM_AGENT_MAX_TURNS": "not-a-number"})
    result = subprocess.run(
        [str(WRAPPER), "probe"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert "invalid max-turns policy" in result.stderr


def _wrapper_env(tmp_path, hermes_exit, status_text="", max_turns="3"):
    """Stub Hermes with a fixed exit code and point the wrapper at a status file."""
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env bash\n"
        f"exit {hermes_exit}\n",
        encoding="utf-8",
    )
    hermes.chmod(0o755)
    status = tmp_path / "fm-state" / "crew.status"
    status.parent.mkdir(parents=True)
    if status_text:
        status.write_text(status_text, encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "HERMES": str(hermes),
        "FM_STATUS_FILE": str(status),
        "FM_AGENT_MAX_TURNS": max_turns,
    })
    return hermes, status, env


def test_wrapper_writes_blocked_handshake_when_hermes_exits_with_work_in_progress(tmp_path):
    """Tri-state: working: but no terminal → blocked (recoverable), not failed."""
    _, status, env = _wrapper_env(
        tmp_path,
        hermes_exit=0,
        status_text="working: implementing round-bounds extraction\n",
    )

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    lines = status.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "blocked: hermes-exit-0-mid-work"
    assert not any(line.startswith("failed:") for line in lines)


def test_wrapper_writes_blocked_handshake_when_resolved_but_no_terminal(tmp_path):
    """Tri-state: resolved: but no terminal → blocked (recoverable)."""
    _, status, env = _wrapper_env(
        tmp_path,
        hermes_exit=0,
        status_text="working: coding\nresolved: MIN_ROUNDS extracted\n",
    )

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    lines = status.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "blocked: hermes-exit-0-mid-work"


def test_wrapper_writes_no_output_handshake_when_status_file_empty(tmp_path):
    """Tri-state: empty status file → failed: no-output (genuine silence)."""
    _, status, env = _wrapper_env(
        tmp_path,
        hermes_exit=0,
        status_text="",
    )

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    lines = status.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "failed: hermes-exit-0-no-output"


def test_wrapper_writes_failed_handshake_when_hermes_exits_without_terminal_status(tmp_path):
    """U10: a crew that exits without a terminal status line must land a bounded
    failed status on the exact supervised path, never a fabricated pass."""
    _, status, env = _wrapper_env(tmp_path, hermes_exit=0, status_text="some random text\n")

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    lines = status.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "failed: hermes-exit-0-no-terminal-status"
    assert not any(line.startswith("done:") for line in lines)


def test_wrapper_records_nonzero_hermes_exit_in_handshake(tmp_path):
    """U10: a non-zero Hermes exit is recorded in the handshake reason."""
    _, status, env = _wrapper_env(
        tmp_path,
        hermes_exit=7,
        status_text="some random non-verb text\n",
    )

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 7
    lines = status.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "failed: hermes-exit-7-no-terminal-status"


def test_wrapper_preserves_terminal_done_status(tmp_path):
    """U10: a crew that already wrote done: keeps its own terminal evidence; the
    wrapper must not overwrite it with a failed handshake."""
    _, status, env = _wrapper_env(
        tmp_path,
        hermes_exit=0,
        status_text="working: coding\ndone: branch=fm/task commit=abc123 base=main@def456\n",
    )

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    text = status.read_text(encoding="utf-8")
    assert text.endswith("done: branch=fm/task commit=abc123 base=main@def456\n")
    assert "failed: hermes-exit" not in text


def test_wrapper_preserves_terminal_failed_status(tmp_path):
    """U10: an existing failed: line is terminal evidence and stays untouched."""
    _, status, env = _wrapper_env(
        tmp_path,
        hermes_exit=3,
        status_text="failed: tests failed\n",
    )

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert status.read_text(encoding="utf-8").strip() == "failed: tests failed"


def test_wrapper_no_status_file_is_noop(tmp_path):
    """U10: without the supervised status path, the wrapper only forwards the
    Hermes result (manual/diagnostic usage)."""
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)
    env = os.environ.copy()
    env.update({"HERMES": str(hermes), "FM_AGENT_MAX_TURNS": "3"})

    result = subprocess.run(
        [str(WRAPPER), "brief"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_tristate_integration_all_outcomes(tmp_path):
    """All three outcomes of the tri-state handshake."""
    def run_with(status_text, hermes_exit=0):
        hermes = tmp_path / "hermes"
        hermes.write_text(f"#!/usr/bin/env bash\nexit {hermes_exit}\n", encoding="utf-8")
        hermes.chmod(0o755)
        status = tmp_path / f"crew-{hermes_exit}-{hash(status_text)}.status"
        if status_text:
            status.write_text(status_text, encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "HERMES": str(hermes),
            "FM_STATUS_FILE": str(status),
            "FM_AGENT_MAX_TURNS": "3",
        })
        result = subprocess.run(
            [str(WRAPPER), "brief"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, status

    # Case 1: done: → no wrapper line appended
    r, s = run_with("working: coding\ndone: branch=fm/task commit=abc123 base=main@def456\n")
    assert r.returncode == 0
    assert "failed:" not in s.read_text()
    assert "blocked:" not in s.read_text()

    # Case 2: working: only → blocked: hermes-exit-0-mid-work
    r, s = run_with("working: implementing round-bounds extraction\n")
    assert r.returncode == 0
    assert s.read_text().endswith("blocked: hermes-exit-0-mid-work\n")

    # Case 3: empty file → failed: hermes-exit-0-no-output
    r, s = run_with("")
    assert r.returncode == 0
    assert s.read_text().endswith("failed: hermes-exit-0-no-output\n")

    # Case 4: crashed (exit 7) with no terminal → failed: hermes-exit-7-no-terminal-status
    r, s = run_with("some random text\n", hermes_exit=7)
    assert r.returncode == 7
    assert s.read_text().endswith("failed: hermes-exit-7-no-terminal-status\n")



