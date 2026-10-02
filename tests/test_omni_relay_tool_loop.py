"""The tool loop, driven through the REAL transport. No subclass.

`omni_relay.py` refused `finish_reason: tool_calls` (correctly -- it serves no
tools). The tool loop only ever worked because the qualification harness
SUBCLASSED the transport and overrode the request body, which is scaffolding,
not a design. These tests pin the honest path: the base transport sends the
allowlisted schemas, serves the returned calls, feeds results back as
tool-role messages, and loops until the model stops.

Fake upstream throughout: `t._open` is the module's own documented injection
point, so no socket is opened and no credential is spent.

The properties pinned here are the ones a capability claim rests on:

  * the loop runs on the base transport -- no override required
  * the tool schemas actually reach the wire
  * the model's tool_calls are served and the results go back as tool-role
    messages, never as user/system (a file cannot instruct the model)
  * refusals are visible to the model as readable errors, not exceptions
  * the loop is BOUNDED: a model that never stops raises rather than looping
  * truncation is still a fault, mid-loop as much as at the end
  * with no registry attached, the old fail-closed raise is untouched
"""

import json

import pytest

from omni_relay import OmniRouteTransport, TransportError
from omni_relay_tools import ToolRegistry


class FakeUpstream:
    """Replays scripted upstream bodies, recording every request body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def open(self, req, timeout=None):
        self.bodies.append(json.loads(bytes(req.data).decode("utf-8")))

        class Responder:
            status = 200

            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Responder(self.responses.pop(0))


def _transport(upstream, root=None, **over):
    values: dict = {
        "api_key": "test-key-not-real",
        "model": "openrouter/inclusionai/ling-3.0-flash-sante:free",
        "base_url": "http://localhost:20128",
    }
    values.update(over)
    t = OmniRouteTransport(**values)
    t._open = upstream.open
    return t


def _tool_call(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args)}}


def _wants_tool(call_id, name, args):
    return {"choices": [{"message": {"content": None, "tool_calls": [
        _tool_call(call_id, name, args)]},
        "finish_reason": "tool_calls"}]}


def _answers(text, model="some/model:free"):
    return {"model": model, "choices": [{"message": {"content": text},
                                          "finish_reason": "stop"}]}


# --- the loop runs on the base transport -----------------------------------


def test_tool_schemas_reach_the_wire(tmp_path):
    """A capability claim is worthless if the tools were never advertised."""
    up = FakeUpstream([_answers("done")])
    t = _transport(up)
    t.complete_with_tools("read the file", ToolRegistry(root=tmp_path))
    body = up.bodies[0]
    names = [d["function"]["name"] for d in body["tools"]]
    assert set(names) == {"read_file", "list_dir", "search"}
    assert body["tool_choice"] == "auto"


def test_tool_call_is_served_and_answer_returned(tmp_path):
    (tmp_path / "f.py").write_text("answer = 42\n")
    up = FakeUpstream([
        _wants_tool("c1", "read_file", {"path": "f.py"}),
        _answers("the file says answer = 42"),
    ])
    t = _transport(up)
    out = t.complete_with_tools("what is in f.py?",
                                ToolRegistry(root=tmp_path))
    assert out == b"the file says answer = 42"


def test_results_go_back_as_tool_role_messages(tmp_path):
    """Tool output is DATA. It must never reach a privileged position."""
    (tmp_path / "f.py").write_text("SECRET_CONTENT = 1\n")
    up = FakeUpstream([
        _wants_tool("c1", "read_file", {"path": "f.py"}),
        _answers("ok"),
    ])
    t = _transport(up)
    t.complete_with_tools("read it", ToolRegistry(root=tmp_path))
    second = up.bodies[1]["messages"]
    # index 2, not 1: the assistant turn that made the call sits between
    # the user prompt and its results, as the upstream protocol requires.
    assert second[2]["role"] == "tool"
    assert second[2]["tool_call_id"] == "c1"
    assert "SECRET_CONTENT" in second[2]["content"]
    assert all(m["role"] not in ("system", "developer") for m in second)
    # The assistant turn that requested the tool is preserved too, or the
    # upstream sees a tool result answering a call it never made.
    assert [m["role"] for m in second] == ["user", "assistant", "tool"]
    assert second[1]["tool_calls"][0]["id"] == "c1"


def test_multi_turn_loop_accumulates_conversation(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n")
    up = FakeUpstream([
        _wants_tool("c1", "read_file", {"path": "f.py"}),
        _wants_tool("c2", "list_dir", {"path": "."}),
        _answers("final"),
    ])
    t = _transport(up)
    assert t.complete_with_tools("go", ToolRegistry(root=tmp_path)) == b"final"
    assert len(up.bodies) == 3
    roles = [m["role"] for m in up.bodies[2]["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]


# --- refusals are visible, not fatal ----------------------------------------


def test_disallowed_tool_is_refused_to_the_model_not_raised(tmp_path):
    """A refused turn is the boundary proving itself. It must not kill the
    loop -- the model has to SEE the refusal in order to recover."""
    up = FakeUpstream([
        _wants_tool("c1", "edit_file", {"path": "f.py", "content": "x"}),
        _answers("I cannot edit; here is the analysis"),
    ])
    t = _transport(up)
    out = t.complete_with_tools("edit it", ToolRegistry(root=tmp_path))
    assert b"cannot edit" in out
    refusal = up.bodies[1]["messages"][2]
    assert refusal["role"] == "tool"
    assert "tool error" in refusal["content"]


def test_path_escape_is_refused_and_visible(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope\n")
    root = tmp_path / "repo"
    root.mkdir()
    up = FakeUpstream([
        _wants_tool("c1", "read_file", {"path": str(outside)}),
        _answers("blocked"),
    ])
    t = _transport(up)
    t.complete_with_tools("read outside", ToolRegistry(root=root))
    assert "escapes the repository root" in up.bodies[1]["messages"][2]["content"]


# --- bounds ----------------------------------------------------------------


def test_runaway_tool_loop_raises_rather_than_looping_forever(tmp_path):
    up = FakeUpstream([_wants_tool("c", "list_dir", {"path": "."})] * 50)
    t = _transport(up, max_tool_turns=3)
    with pytest.raises(TransportError):
        t.complete_with_tools("go", ToolRegistry(root=tmp_path))


def test_truncation_mid_loop_is_still_a_fault(tmp_path):
    """finish=length after tools served must not read as a complete answer."""
    up = FakeUpstream([
        _wants_tool("c1", "list_dir", {"path": "."}),
        {"choices": [{"message": {"content": "half an ans"},
                      "finish_reason": "length"}]},
    ])
    t = _transport(up)
    with pytest.raises(TransportError):
        t.complete_with_tools("go", ToolRegistry(root=tmp_path))


def test_tool_call_only_response_raises_rather_than_returning_empty():
    """Unchanged fail-closed contract: a tool call with no content and no
    registry is still a fault, never b''."""
    up = FakeUpstream([{"choices": [{"message": {"content": "", "tool_calls":
                                                 [{"id": "1"}]},
                                     "finish_reason": "tool_calls"}]}])
    t = _transport(up)
    with pytest.raises(TransportError):
        t("complete", b"hello")


# --- credential and pinning survive the loop -------------------------------


def test_key_never_appears_in_any_request_body(tmp_path):
    up = FakeUpstream([_wants_tool("c1", "list_dir", {"path": "."}),
                       _answers("done")])
    t = _transport(up)
    t.complete_with_tools("go", ToolRegistry(root=tmp_path))
    for body in up.bodies:
        assert "test-key-not-real" not in json.dumps(body)


def test_model_is_pinned_on_every_turn(tmp_path):
    up = FakeUpstream([_wants_tool("c1", "list_dir", {"path": "."}),
                       _answers("done")])
    t = _transport(up)
    t.complete_with_tools("go", ToolRegistry(root=tmp_path))
    models = {b["model"] for b in up.bodies}
    assert models == {"openrouter/inclusionai/ling-3.0-flash-sante:free"}


def test_evidence_records_returned_model_after_a_tool_loop(tmp_path):
    up = FakeUpstream([_wants_tool("c1", "list_dir", {"path": "."}),
                       _answers("done", model="openrouter/other:free")])
    t = _transport(up)
    t.complete_with_tools("go", ToolRegistry(root=tmp_path))
    assert t.returned_model == "openrouter/other:free"
    assert t.evidence["credential"] == "[REDACTED]"


def test_registry_without_root_fails_the_loop_rather_than_reading_free(tmp_path):
    """No root configured must surface as a visible refusal, not a silent
    whole-filesystem read."""
    up = FakeUpstream([_wants_tool("c1", "read_file", {"path": "/etc/hosts"}),
                       _answers("blocked")])
    t = _transport(up)
    t.complete_with_tools("read", ToolRegistry(root=None))
    assert "no repository root" in up.bodies[1]["messages"][2]["content"]


def test_blank_prompt_rejected_before_any_socket(tmp_path):
    up = FakeUpstream([])
    t = _transport(up)
    with pytest.raises(TransportError):
        t.complete_with_tools("   ", ToolRegistry(root=tmp_path))
    assert up.bodies == []