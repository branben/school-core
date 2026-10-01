"""Contract tests for the OmniRoute-backed relay transport.

`omni_relay.py` is the bridge the whole relay path was missing: it satisfies
model_relay.RelayTransport (`__call__(operation, payload) -> bytes`) by
calling a pinned OmniRoute endpoint while holding the upstream credential, so
the capability surface carries no URL, key, or header.

These tests are written FIRST (RED) and pin the properties that matter:

  * the endpoint and model are pinned, never caller-supplied (no SSRF, no
    model steering, no key leakage)
  * the credential is never placed in the request payload or the evidence
  * every transport-level fault raises, so the relay stays fail-closed
  * an empty upstream body is an error here, not a successful inference
  * the request never carries an unbounded conversation

A live end-to-end check is separate and opt-in: see
tests/test_omni_relay_live.py, which is skipped unless OMNIROUTE_API_KEY is
set. These are hermetic.
"""

import json

import pytest

from omni_relay import (
    OmniRouteTransport,
    TransportError,
    _redact,
    build_completion_request,
)


def _transport(**over):
    values = {
        "api_key": "test-key-not-real",
        "model": "openrouter/inclusionai/ling-3.0-flash-sante:free",
        "base_url": "http://localhost:20128",
    }
    values.update(over)
    return OmniRouteTransport(**values)


# --- pinning: the caller cannot steer the target ---------------------------


def test_endpoint_is_not_caller_supplied():
    t = _transport()
    # The transport owns its endpoint. There is deliberately no parameter on
    # the request path that can redirect it.
    assert t.base_url == "http://localhost:20128"
    assert not hasattr(t, "call")


def test_model_is_pinned_at_construction_and_not_per_request():
    t = _transport()
    assert t.model == "openrouter/inclusionai/ling-3.0-flash-sante:free"
    # A per-request body may not carry a different model.
    body = build_completion_request(
        operation="complete",
        prompt="hi",
        model=t.model,
    )
    assert body["model"] == t.model


def test_api_key_never_appears_in_the_request_body():
    t = _transport(api_key="SUPERSECRETKEY123")
    body = build_completion_request(
        operation="complete", prompt="hi", model=t.model)
    assert "SUPERSECRETKEY123" not in json.dumps(body)


def test_api_key_never_appears_in_repr():
    t = _transport(api_key="SUPERSECRETKEY123")
    assert "SUPERSECRETKEY123" not in repr(t)


def test_redact_removes_keys_and_authorization():
    d = {"model": "m", "Authorization": "Bearer sk-abc",
         "api_key": "sk-abc", "messages": [{"role": "user", "content": "x"}]}
    out = _redact(d)
    assert "sk-abc" not in json.dumps(out)
    assert out["model"] == "m"
    assert out["messages"][0]["content"] == "x"  # payload content is not a secret


# --- request shape ---------------------------------------------------------


def test_request_has_exactly_one_user_message():
    body = build_completion_request(operation="complete", prompt="p", model="m")
    assert body["messages"] == [{"role": "user", "content": "p"}]


def test_request_rejects_empty_prompt():
    with pytest.raises(TransportError):
        build_completion_request(operation="complete", prompt="", model="m")


def test_request_rejects_unknown_operation():
    with pytest.raises(TransportError):
        build_completion_request(operation="rm-rf", prompt="p", model="m")


# --- fail-closed: every fault raises --------------------------------------


def test_missing_api_key_raises_rather_than_calling_out():
    t = _transport(api_key="")
    with pytest.raises(TransportError):
        t("complete", b"hello")


def test_transport_failure_raises():
    class Boom:
        def __call__(self, *a, **k):
            raise OSError("connection refused")
    t = _transport()
    t._opener = Boom()
    with pytest.raises(TransportError):
        t("complete", b"hello")


def test_http_error_raises():
    class Err(Exception):
        pass

    class HTTPErrorish(Exception):
        pass

    class Responder:
        def __init__(self, *a):
            self.status = 500
        def read(self):
            return b"upstream exploded"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    with pytest.raises(TransportError):
        t("complete", b"hello")


def test_empty_upstream_body_raises_not_succeeds():
    """HTTP 200 with an empty body is a fault, never an inference.

    This is the exact shape that Longcat returns through OmniRoute.
    """

    class Responder:
        status = 200

        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    with pytest.raises(TransportError):
        t("complete", b"hello")


def test_body_with_no_choices_raises():
    class Responder:
        status = 200

        def read(self):
            return json.dumps({"choices": []}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    with pytest.raises(TransportError):
        t("complete", b"hello")


def test_truncated_finish_reason_raises():
    """A truncated answer must never be handed back as if it were complete."""

    class Responder:
        status = 200

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "partial"},
                             "finish_reason": "length"}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    with pytest.raises(TransportError):
        t("complete", b"hello")


def test_tool_call_only_response_raises_rather_than_returning_empty():
    """finish_reason=tool_calls with no content is NOT a usable answer.

    Returning b'' here is precisely the fail-open the relay forbids, and is
    what made a free model look incapable when it was merely mid-conversation.
    """

    class Responder:
        status = 200

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "",
                                         "tool_calls": [{"id": "1"}]},
                             "finish_reason": "tool_calls"}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    with pytest.raises(TransportError):
        t("complete", b"hello")


# --- the happy path, hermetically ----------------------------------------


def test_successful_call_returns_content_bytes():
    class Responder:
        status = 200

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "the answer"},
                             "finish_reason": "stop"}],
                "model": "openrouter/inclusionai/ling-3.0-flash-sante:free",
                "usage": {"completion_tokens": 7}}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    assert t("complete", b"hello") == b"the answer"


def test_call_records_requested_and_returned_model_for_audit():
    class Responder:
        status = 200

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "x"},
                             "finish_reason": "stop"}],
                "model": "SOMETHING-ELSE"}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    t = _transport()
    t._open = lambda *a, **k: Responder()
    t("complete", b"hello")
    # Even when a route substitutes a different model, both are recorded.
    assert t.requested_model == "openrouter/inclusionai/ling-3.0-flash-sante:free"
    assert t.returned_model == "SOMETHING-ELSE"
