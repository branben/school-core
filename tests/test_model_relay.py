"""Contract tests for the task-scoped model relay capability.

The relay is the only path from the student guest to model inference. These
tests pin the threat-model contract in docs/student-vm-boundary.md: explicit
allowed operations, per-task quota and deadline, bounded request/response,
task binding, revocation on terminal state, fail-closed transport errors, and
redacted bounded evidence.
"""

import json

import pytest

from model_relay import (
    ModelRelayBlocked,
    RelayPolicy,
    TaskScopedModelRelay,
)


def _policy(**over):
    values = {
        "task_id": "task-1",
        "allowed_operations": ("complete",),
        "max_requests": 4,
        "max_request_bytes": 1024,
        "max_response_bytes": 1024,
        "max_total_bytes": 4096,
        "max_duration_seconds": 60,
    }
    values.update(over)
    return RelayPolicy(**values)


class SpyTransport:
    def __init__(self, response=b"model output", exc=None):
        self.calls = []
        self.response = response
        self.exc = exc

    def __call__(self, operation, payload):
        self.calls.append((operation, bytes(payload)))
        if self.exc is not None:
            raise self.exc
        return self.response


def _relay(transport=None, *, clock=None, **policy_over):
    policy = _policy(**policy_over)
    kwargs = {"task_id": policy.task_id, "policy": policy,
              "transport": transport or SpyTransport()}
    if clock is not None:
        kwargs["clock"] = clock
    return TaskScopedModelRelay(**kwargs)


# ---------------------------------------------------------- happy path ---

def test_allowed_operation_succeeds_and_counts_usage():
    transport = SpyTransport(response=b"answer")
    relay = _relay(transport)
    out = relay.request(task_id="task-1", operation="complete", payload=b"prompt")
    assert out == b"answer"
    assert transport.calls == [("complete", b"prompt")]
    snap = relay.snapshot()
    assert snap["requests_used"] == 1
    assert snap["bytes_used"] == len(b"prompt") + len(b"answer")


# ------------------------------------------------------ operation scope ---

def test_unknown_operation_is_refused_before_the_transport():
    transport = SpyTransport()
    relay = _relay(transport, allowed_operations=("complete",))
    with pytest.raises(ModelRelayBlocked, match="operation"):
        relay.request(task_id="task-1", operation="embed", payload=b"prompt")
    assert transport.calls == [], "a refused operation must never reach the transport"


def test_operation_matching_is_exact_not_prefix_based():
    transport = SpyTransport()
    relay = _relay(transport, allowed_operations=("complete",))
    with pytest.raises(ModelRelayBlocked):
        relay.request(task_id="task-1", operation="complete-anything", payload=b"p")
    assert transport.calls == []


# ---------------------------------------------------------- task binding ---

def test_cross_task_calls_are_refused():
    transport = SpyTransport()
    relay = _relay(transport, task_id="task-1")
    with pytest.raises(ModelRelayBlocked, match="task"):
        relay.request(task_id="task-2", operation="complete", payload=b"prompt")
    assert transport.calls == [], "a handle scoped to task-1 must not serve task-2"


# -------------------------------------------------------- size bounds ---

def test_oversized_request_is_refused_before_the_transport():
    transport = SpyTransport()
    relay = _relay(transport, max_request_bytes=10)
    with pytest.raises(ModelRelayBlocked, match="request"):
        relay.request(task_id="task-1", operation="complete", payload=b"x" * 11)
    assert transport.calls == []


def test_oversized_response_is_rejected_fail_closed_not_truncated():
    transport = SpyTransport(response=b"y" * 100)
    relay = _relay(transport, max_response_bytes=10)
    with pytest.raises(ModelRelayBlocked, match="response"):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")
    # the bounded output must not be silently handed back as a partial answer


def test_empty_response_is_rejected_fail_closed_not_returned_as_success():
    # An upstream that answers HTTP 200 with no body is an upstream FAULT.
    # Returning b"" would hand the guest an empty result indistinguishable
    # from a real inference, which is exactly the fail-open the relay forbids.
    relay = _relay(SpyTransport(response=b""))
    with pytest.raises(ModelRelayBlocked, match="empty"):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")


def test_malformed_payload_type_is_rejected():
    relay = _relay()
    with pytest.raises(ValueError):
        relay.request(task_id="task-1", operation="complete", payload="not-bytes")


def test_malformed_transport_response_is_rejected_fail_closed():
    relay = _relay(SpyTransport(response="not-bytes"))
    with pytest.raises(ModelRelayBlocked, match="response"):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")


# ------------------------------------------------------------- quota ---

def test_request_quota_exhaustion_refuses_further_calls():
    transport = SpyTransport()
    relay = _relay(transport, max_requests=2)
    relay.request(task_id="task-1", operation="complete", payload=b"a")
    relay.request(task_id="task-1", operation="complete", payload=b"b")
    with pytest.raises(ModelRelayBlocked, match="quota"):
        relay.request(task_id="task-1", operation="complete", payload=b"c")
    assert len(transport.calls) == 2, "the transport must not be touched past quota"


def test_total_byte_budget_exhaustion_refuses_further_calls():
    transport = SpyTransport(response=b"r" * 50)
    relay = _relay(transport, max_total_bytes=100)
    relay.request(task_id="task-1", operation="complete", payload=b"p" * 40)  # 90 used
    with pytest.raises(ModelRelayBlocked, match="quota"):
        relay.request(task_id="task-1", operation="complete", payload=b"p" * 40)
    assert len(transport.calls) == 1


def test_response_bytes_are_charged_against_the_total_budget_fail_closed():
    """max_total_bytes is a hard cap across requests AND responses. A response
    that would push the total over the budget is refused entirely — a partial
    or silently oversized answer must never be handed back (WDE seams run,
    school-core-sjv.7.18)."""
    transport = SpyTransport(response=b"r" * 200)
    relay = _relay(transport, max_total_bytes=100, max_response_bytes=1000)
    with pytest.raises(ModelRelayBlocked, match="budget"):
        relay.request(task_id="task-1", operation="complete", payload=b"p" * 40)
    snap = relay.snapshot()
    assert snap["bytes_used"] <= 100, (
        "the total byte budget must never be exceeded by response accounting"
    )


def test_response_that_exactly_fits_the_budget_is_accepted():
    transport = SpyTransport(response=b"r" * 60)
    relay = _relay(transport, max_total_bytes=100)
    out = relay.request(task_id="task-1", operation="complete", payload=b"p" * 40)
    assert out == b"r" * 60
    assert relay.snapshot()["bytes_used"] == 100  # exact fit stays inside the cap


def test_budget_refused_response_still_consumes_the_attempt_and_request_bytes():
    """The upstream saw the request, so the attempt and its bytes are spent even
    when the response is refused on budget grounds."""
    transport = SpyTransport(response=b"r" * 200)
    relay = _relay(transport, max_requests=1, max_total_bytes=100, max_response_bytes=1000)
    with pytest.raises(ModelRelayBlocked):
        relay.request(task_id="task-1", operation="complete", payload=b"p" * 40)
    with pytest.raises(ModelRelayBlocked, match="quota"):
        relay.request(task_id="task-1", operation="complete", payload=b"x")
    snap = relay.snapshot()
    assert snap["bytes_used"] == 40  # request bytes charged; discarded response not counted


# ----------------------------------------------------------- deadline ---

def test_deadline_expiry_refuses_and_revokes_the_capability():
    transport = SpyTransport()
    now = [1000.0]
    relay = _relay(transport, max_duration_seconds=10, clock=lambda: now[0])
    relay.request(task_id="task-1", operation="complete", payload=b"a")
    now[0] = 1011.0
    with pytest.raises(ModelRelayBlocked):
        relay.request(task_id="task-1", operation="complete", payload=b"b")
    snap = relay.snapshot()
    assert snap["revoked"] is True
    assert "deadline" in snap["revoke_reason"]
    assert len(transport.calls) == 1


# --------------------------------------------------------- revocation ---

def test_revoke_is_idempotent_permanent_and_refuses_all_calls():
    transport = SpyTransport()
    relay = _relay(transport)
    relay.revoke("task terminal")
    relay.revoke("second reason")  # idempotent: no error, first reason wins
    with pytest.raises(ModelRelayBlocked, match="revoked"):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")
    snap = relay.snapshot()
    assert snap["revoked"] is True
    assert snap["revoke_reason"] == "task terminal"
    assert transport.calls == []


# ------------------------------------------------------ fail closed ---

def test_transport_failure_blocks_and_never_falls_back():
    transport = SpyTransport(exc=RuntimeError("upstream 500"))
    relay = _relay(transport)
    with pytest.raises(ModelRelayBlocked, match="transport"):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")
    # no fallback transport was consulted; the failure is a visible block


def test_transport_failure_still_consumes_the_request_attempt():
    transport = SpyTransport(exc=RuntimeError("upstream 500"))
    relay = _relay(transport, max_requests=1)
    with pytest.raises(ModelRelayBlocked):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")
    with pytest.raises(ModelRelayBlocked, match="quota"):
        relay.request(task_id="task-1", operation="complete", payload=b"prompt")


# --------------------------------------------------------- redaction ---

def test_snapshot_is_bounded_and_contains_no_payload_or_response_content():
    transport = SpyTransport(response=b"SECRET-RESPONSE")
    relay = _relay(transport)
    relay.request(task_id="task-1", operation="complete", payload=b"SECRET-PROMPT")
    snap = relay.snapshot()
    dumped = json.dumps(snap)
    assert "SECRET-PROMPT" not in dumped
    assert "SECRET-RESPONSE" not in dumped
    assert set(snap.keys()) <= {
        "task_id", "requests_used", "requests_remaining", "bytes_used",
        "bytes_remaining", "revoked", "revoke_reason", "expired",
    }


# ---------------------------------------------------- policy validation ---

def test_policy_rejects_invalid_values():
    for over in (
        {"task_id": "Bad/Id"},
        {"task_id": ""},
        {"allowed_operations": ()},
        {"allowed_operations": ("has space",)},
        {"allowed_operations": "complete"},  # not a tuple
        {"max_requests": 0},
        {"max_requests": True},  # bool is not an int here
        {"max_request_bytes": -1},
        {"max_response_bytes": 0},
        {"max_total_bytes": 0},
        {"max_duration_seconds": -5},
    ):
        with pytest.raises(ValueError):
            _policy(**over)
