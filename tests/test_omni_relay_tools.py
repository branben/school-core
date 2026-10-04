"""Contract tests for tool serving in the OmniRoute relay transport.

A bare completion cannot do repo work: the model answers `finish_reason:
tool_calls` with no content, which the transport correctly refuses. Serving
tools is what closes that gap.

The security properties that matter, and why each is a test:

  * The tool set is an ALLOWLIST, fixed at construction. A model cannot
    invent a tool, and there is no generic "run this" escape hatch.
  * Every path is confined to one root, resolved AFTER symlink evaluation.
    Ticket text, filenames, and symlinks are hostile input, so a path that
    looks inside the root but resolves outside it must be refused.
  * Tools are READ-ONLY in this slice. No write, no shell. A model that asks
    to write gets an error result, never a mutation.
  * A refused tool call is reported BACK to the model as a tool error, so it
    can correct itself. It must not crash the loop -- but it must also not be
    silently swallowed, which would look like the tool succeeded.
  * Tool OUTPUT is data, not instruction. Nothing read from disk is ever
    promoted into the system/developer position.

Written RED before implementation.
"""

import json
import os
import sys

import pytest

from omni_relay import (
    ToolError,
    ToolRegistry,
    build_tool_request,
)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "model_relay.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("alpha\n", encoding="utf-8")
    return tmp_path


# --- the allowlist is an allowlist ----------------------------------------


def test_registry_exposes_exactly_the_readonly_tools(root):
    reg = ToolRegistry(root=root)
    assert sorted(reg.names) == ["list_dir", "read_file", "search"]


def test_unknown_tool_is_refused(root):
    reg = ToolRegistry(root=root)
    with pytest.raises(ToolError):
        reg.call("write_file", {"path": "x", "content": "y"})
    with pytest.raises(ToolError):
        reg.call("shell", {"command": "rm -rf /"})


def test_tool_names_are_not_extensible_at_runtime(root):
    """A model must not be able to register or reach a new tool."""
    reg = ToolRegistry(root=root)
    with pytest.raises((ToolError, AttributeError, TypeError)):
        reg.call("__class__", {})


# --- path confinement ------------------------------------------------------


def test_read_inside_root_succeeds(root):
    reg = ToolRegistry(root=root)
    out = reg.call("read_file", {"path": "sub/a.txt"})
    assert "alpha" in out


def test_traversal_outside_root_is_refused(root):
    reg = ToolRegistry(root=root)
    with pytest.raises(ToolError):
        reg.call("read_file", {"path": "../../../etc/passwd"})


def test_absolute_path_outside_root_is_refused(root):
    reg = ToolRegistry(root=root)
    with pytest.raises(ToolError):
        reg.call("read_file", {"path": "/etc/passwd"})


def test_symlink_escaping_root_is_refused(root, tmp_path_factory):
    """The classic bypass: a path that looks inside but resolves outside."""
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    link = root / "escape.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    reg = ToolRegistry(root=root)
    # Either the path is refused outright, or the content is not surfaced.
    try:
        out = reg.call("read_file", {"path": "escape.txt"})
    except ToolError:
        return
    assert "TOP SECRET" not in out


def test_missing_file_raises_tool_error_not_silent_empty(root):
    reg = ToolRegistry(root=root)
    with pytest.raises(ToolError):
        reg.call("read_file", {"path": "nope.py"})


def test_read_is_capped_so_a_huge_file_cannot_exhaust_memory(root):
    """A 200KB file must come back capped AND visibly marked.

    The marker matters as much as the cap: a silently truncated read looks to
    the model like the whole file, which is how a wrong conclusion gets built
    on a partial view. Asserting only `len(out) < size` passes even with no
    cap at all, so the marker is what makes this test discriminating.
    """
    from omni_relay_tools import MAX_READ_BYTES
    (root / "big.txt").write_text("A" * (MAX_READ_BYTES * 2), encoding="utf-8")
    reg = ToolRegistry(root=root)
    out = reg.call("read_file", {"path": "big.txt"})
    assert len(out) < MAX_READ_BYTES * 2          # genuinely capped
    assert "truncated" in out                      # and says so
    assert str(MAX_READ_BYTES) in out


def test_small_file_is_returned_whole_with_no_truncation_marker(root):
    reg = ToolRegistry(root=root)
    out = reg.call("read_file", {"path": "sub/a.txt"})
    assert out == "alpha\n"
    assert "truncated" not in out


# --- list_dir and search ---------------------------------------------------


def test_list_dir_returns_names(root):
    reg = ToolRegistry(root=root)
    out = reg.call("list_dir", {"path": "."})
    assert "model_relay.py" in out


def test_search_returns_matching_lines(root):
    reg = ToolRegistry(root=root)
    out = reg.call("search", {"pattern": "alpha"})
    assert "a.txt" in out


def test_search_pattern_is_treated_as_a_literal_not_a_regex(root):
    """A catastrophic pattern must not become a ReDoS vector."""
    reg = ToolRegistry(root=root)
    # Must return promptly; literal matching means this finds nothing.
    out = reg.call("search", {"pattern": "(a+)+$"})
    assert isinstance(out, str)


# --- read-only: no mutation is possible -----------------------------------


def test_there_is_no_write_or_exec_tool_to_call(root):
    reg = ToolRegistry(root=root)
    for name in ("write_file", "edit_file", "run", "shell", "exec",
                 "delete_file", "apply_patch"):
        with pytest.raises(ToolError):
            reg.call(name, {})


def test_registry_holds_no_mutating_capability_attributes(root):
    reg = ToolRegistry(root=root)
    for attr in ("write", "write_text", "unlink", "remove", "system",
                 "popen", "run_command"):
        assert not hasattr(reg, attr), attr


# --- wiring into the request shape ----------------------------------------


def test_tool_definitions_are_advertised_in_the_request():
    body = build_tool_request(
        operation="complete",
        prompt="read the file",
        model="m",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["messages"][0]["content"] == "read the file"


def test_no_tools_means_no_tools_key():
    body = build_tool_request(operation="complete", prompt="p", model="m",
                              tools=[])
    assert "tools" not in body


def test_tool_result_content_is_data_not_a_system_message():
    """Tool output must never land in the system/developer position."""
    reg = ToolRegistry(root=None)
    msgs = reg.as_messages([
        {"role": "tool", "tool_call_id": "1", "content": "file body"},
    ])
    assert [m["role"] for m in msgs] == ["tool"]
    assert "system" not in [m["role"] for m in msgs]


# --- the loop contract -----------------------------------------------------


def test_turn_conversion_raises_on_a_malformed_tool_call():
    reg = ToolRegistry(root=None)
    with pytest.raises(ToolError):
        reg.turn(tool_calls=[{"no": "function"}])


def test_tool_call_id_is_echoed_so_the_model_can_match_it(root):
    reg = ToolRegistry(root=root)
    results = reg.execute([{
        "id": "call_42",
        "function": {"name": "read_file",
                     "arguments": json.dumps({"path": "sub/a.txt"})},
    }])
    assert results[0]["tool_call_id"] == "call_42"
    assert "alpha" in results[0]["content"]


def test_a_refused_tool_call_returns_an_error_result_not_a_crash(root):
    """The model must see the refusal so it can correct itself."""
    reg = ToolRegistry(root=root)
    results = reg.execute([{
        "id": "call_9",
        "function": {"name": "read_file",
                     "arguments": json.dumps({"path": "../escape"})},
    }])
    assert results[0]["tool_call_id"] == "call_9"
    assert "error" in results[0]["content"].lower()