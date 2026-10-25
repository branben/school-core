"""Tests for the Bot Mode dispatch adapter and its MCP tool wiring.

``bot_adapter`` is the bridge that lets School Core run work inside a full
Hermes profile (``hermes -p <bot> chat``) rather than a raw model combo. It
was ported into ``src/tools/`` from the older root-level layout, so these
tests pin both the adapter's own behaviour and the MCP server's advertised
tool surface.

The parity test (``test_advertised_tools_all_have_handlers``) is the
load-bearing one: the MCP server publishes ``TOOL_DEFINITIONS`` to clients
and dispatches through ``TOOL_HANDLERS``. If those two drift, a client is
told a tool exists and then gets ``-32601 Unknown tool`` when it calls it.
"""

from __future__ import annotations

import subprocess

import pytest

from src.tools import bot_adapter
from src.tools import mcp_server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_profiles(tmp_path, monkeypatch):
    """Point the adapter at a temp profiles dir with two fake bots."""
    profiles = tmp_path / "profiles"
    (profiles / "student-coder").mkdir(parents=True)
    (profiles / "teacher-cto").mkdir(parents=True)

    (profiles / "student-coder" / "SOUL.md").write_text("# Coder\n\nWrites code.\n")
    (profiles / "student-coder" / "config.yaml").write_text(
        "model:\n  default: meituan/longcat-2.5-preview:free\n"
    )

    monkeypatch.setattr(bot_adapter, "HERMES_PROFILES_DIR", str(profiles))
    monkeypatch.setattr(bot_adapter, "_hermes_binary", lambda: "/fake/bin/hermes")
    return profiles


class _FakeCompleted:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# list_bots / bot_exists / bot_info
# ---------------------------------------------------------------------------


def test_list_bots_returns_every_profile_dir(fake_profiles):
    assert bot_adapter.list_bots() == ["student-coder", "teacher-cto"]


def test_list_bots_is_empty_when_profiles_dir_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_adapter, "HERMES_PROFILES_DIR", str(tmp_path / "nope"))
    assert bot_adapter.list_bots() == []


def test_bot_exists_distinguishes_present_from_absent(fake_profiles):
    assert bot_adapter.bot_exists("student-coder") is True
    assert bot_adapter.bot_exists("no-such-bot") is False


def test_bot_info_reads_model_and_soul_preview(fake_profiles):
    info = bot_adapter.bot_info("student-coder")

    assert info["exists"] is True
    assert info["model"] == "meituan/longcat-2.5-preview:free"
    assert info["soul_preview"].startswith("# Coder")


def test_bot_info_for_missing_bot_reports_not_exists(fake_profiles):
    assert bot_adapter.bot_info("ghost") == {"exists": False, "name": "ghost"}


# ---------------------------------------------------------------------------
# bot_execute
# ---------------------------------------------------------------------------


def test_bot_execute_refuses_an_unknown_bot_without_shelling_out(fake_profiles, monkeypatch):
    def _explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("subprocess.run must not be called for an unknown bot")

    monkeypatch.setattr(bot_adapter.subprocess, "run", _explode)

    result = bot_adapter.bot_execute("ghost", "do a thing")

    assert result["success"] is False
    assert "not found" in result["error"]
    assert result["duration_seconds"] == 0.0


def test_bot_execute_returns_stdout_on_success(fake_profiles, monkeypatch):
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeCompleted(0, stdout="  all done  \n")

    monkeypatch.setattr(bot_adapter.subprocess, "run", _run)

    result = bot_adapter.bot_execute("student-coder", "write a function")

    assert result["success"] is True
    assert result["response"] == "all done"
    assert result["error"] is None
    assert captured["cmd"][:4] == ["/fake/bin/hermes", "-p", "student-coder", "chat"]
    assert captured["env"]["HERMES_QUIET"] == "1"


def test_bot_execute_passes_conversation_when_given(fake_profiles, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        bot_adapter.subprocess,
        "run",
        lambda cmd, **kw: (captured.update(cmd=cmd), _FakeCompleted(0, "ok"))[1],
    )

    bot_adapter.bot_execute("student-coder", "hi", conversation="Agent Inbox")

    assert "-c" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-c") + 1] == "Agent Inbox"


def test_bot_execute_surfaces_stderr_on_nonzero_exit(fake_profiles, monkeypatch):
    monkeypatch.setattr(
        bot_adapter.subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted(1, stdout="", stderr="boom"),
    )

    result = bot_adapter.bot_execute("student-coder", "hi")

    assert result["success"] is False
    assert result["error"] == "boom"
    assert result["response"] == ""


def test_bot_execute_reports_timeout_without_raising(fake_profiles, monkeypatch):
    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=7)

    monkeypatch.setattr(bot_adapter.subprocess, "run", _timeout)

    result = bot_adapter.bot_execute("student-coder", "hi", timeout=7)

    assert result["success"] is False
    assert result["error"] == "timeout after 7s"
    assert result["duration_seconds"] == 7.0


# ---------------------------------------------------------------------------
# bot_mention
# ---------------------------------------------------------------------------


def test_bot_mention_prefixes_the_message_and_targets_agent_inbox(fake_profiles, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        bot_adapter.subprocess,
        "run",
        lambda cmd, **kw: (captured.update(cmd=cmd), _FakeCompleted(0, "ack"))[1],
    )

    result = bot_adapter.bot_mention("student-coder", "teacher-cto", "please review")

    assert result["success"] is True
    assert result["from"] == "student-coder"
    assert result["to"] == "teacher-cto"
    prompt = captured["cmd"][captured["cmd"].index("-q") + 1]
    assert prompt.startswith("Message from 🤖 student-coder (@student-coder): please review")
    assert captured["cmd"][captured["cmd"].index("-c") + 1] == "Agent Inbox"


def test_bot_mention_refuses_an_unknown_target(fake_profiles, monkeypatch):
    monkeypatch.setattr(
        bot_adapter.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not shell out")),
    )

    result = bot_adapter.bot_mention("student-coder", "ghost", "hello")

    assert result["success"] is False
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------


def test_advertised_tools_all_have_handlers():
    """Every tool published in tools/list must be dispatchable.

    Without this, the server advertises a tool and then answers -32601.
    """
    advertised = {definition["name"] for definition in mcp_server.TOOL_DEFINITIONS}
    dispatchable = set(mcp_server.TOOL_HANDLERS)

    assert advertised == dispatchable, (
        f"advertised but not callable: {sorted(advertised - dispatchable)}; "
        f"callable but not advertised: {sorted(dispatchable - advertised)}"
    )


def test_the_four_bot_tools_are_registered():
    for name in (
        "school_bot_execute",
        "school_bot_mention",
        "school_bot_list",
        "school_bot_info",
    ):
        assert name in mcp_server.TOOL_HANDLERS, f"{name} is not wired"


def test_bot_tools_degrade_to_an_error_when_the_adapter_is_missing(monkeypatch):
    """A missing adapter must not take the whole server down.

    The module guards the import, so the tools stay advertised but answer with
    a per-tool error rather than killing the process at startup.
    """
    monkeypatch.setattr(mcp_server, "bot_execute", None)
    monkeypatch.setattr(mcp_server, "bot_mention", None)
    monkeypatch.setattr(mcp_server, "list_bots", None)
    monkeypatch.setattr(mcp_server, "bot_info", None)

    for name, args in (
        ("school_bot_execute", {"bot": "x", "prompt": "y"}),
        ("school_bot_mention", {"from_bot": "x", "to_bot": "y", "message": "z"}),
        ("school_bot_list", {}),
        ("school_bot_info", {"bot": "x"}),
    ):
        result = mcp_server.TOOL_HANDLERS[name](args)
        assert result.get("has_error") is True, f"{name} did not fail closed"
        assert "bot_adapter not available" in result["error"]


def test_bot_list_handler_reports_a_count(fake_profiles):
    result = mcp_server.TOOL_HANDLERS["school_bot_list"]({})

    assert result["bots"] == ["student-coder", "teacher-cto"]
    assert result["count"] == 2


def test_bot_execute_handler_marks_failure_as_an_error(fake_profiles, monkeypatch):
    monkeypatch.setattr(
        bot_adapter.subprocess,
        "run",
        lambda cmd, **kw: _FakeCompleted(1, stdout="", stderr="nope"),
    )

    result = mcp_server.TOOL_HANDLERS["school_bot_execute"](
        {"bot": "student-coder", "prompt": "hi"}
    )

    assert result["has_error"] is True
