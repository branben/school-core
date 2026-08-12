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
