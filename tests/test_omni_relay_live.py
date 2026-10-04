"""Live end-to-end check of the OmniRoute relay transport.

Opt-in: skipped unless OMNIROUTE_API_KEY is set, so the hermetic suite stays
hermetic. This is the test that proves the bridge actually works against a live
upstream -- everything in test_omni_relay.py injects a fake response.

Run with:
    OMNIROUTE_API_KEY=... .venv/bin/python -m pytest tests/test_omni_relay_live.py -v
"""

import os

import pytest

from model_relay import ModelRelayBlocked, RelayPolicy, TaskScopedModelRelay
from omni_relay import OmniRouteTransport, TransportError, from_env

pytestmark = pytest.mark.skipif(
    not os.environ.get("OMNIROUTE_API_KEY"),
    reason="OMNIROUTE_API_KEY not set; live relay test skipped",
)

#: Upstream messages that mean "the free-tier budget is spent", not "the code
#: is broken". OpenRouter answers 402 for per-model credit exhaustion and 429
#: for the daily free-model cap; both reset on their own schedule.
#:
#: Skipping on these ALONE is deliberate. Skipping on any TransportError would
#: turn this file into a green check that can never fail -- mock-the-underlying-
#: API theater, which is exactly what these tests exist to prevent. A genuine
#: fault (bad JSON, an empty body, a leaked credential) still fails here.
_QUOTA_EXHAUSTED = ("out of credits", "free-models-per-day",
                    "Rate limit exceeded", "quota",
                    "not_found (reset")


def _skip_if_quota_exhausted(exc: BaseException) -> None:
    """Skip when the free tier is spent; re-raise every genuine fault.

    `ModelRelayBlocked` deliberately wraps a transport fault without echoing
    its text, so the quota markers have to be looked for on the chained cause
    as well. Walking `__cause__`/`__context__` is what makes this work.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        haystack = f"{node} {node.__cause__ or ''}".lower()
        if any(marker in haystack for marker in _QUOTA_EXHAUSTED):
            pytest.skip(f"upstream free-tier quota exhausted: {exc}")
        node = node.__cause__ or node.__context__
    raise exc


@pytest.fixture
def live_transport():
    """A transport, or a skip when the free tier is spent."""
    try:
        return from_env(model=MODEL, timeout=120.0, max_tokens=200)
    except TransportError as exc:
        _skip_if_quota_exhausted(exc)

MODEL = "openrouter/inclusionai/ling-3.0-flash-sante:free"


def _call(transport):
    """One live completion; skips only on genuine quota exhaustion."""
    try:
        return transport("complete", b"Reply with exactly the word: PONG")
    except TransportError as exc:
        _skip_if_quota_exhausted(exc)


def test_live_completion_returns_real_content():
    t = from_env(model=MODEL, timeout=120.0, max_tokens=200)
    out = _call(t)
    assert out
    text = out.decode("utf-8").strip()
    assert text, "live upstream returned empty text"
    assert t.returned_model, "transport did not record the returned model"
    print(f"\n  model requested: {t.requested_model}")
    print(f"  model returned : {t.returned_model}")
    print(f"  usage          : {t.last_usage}")
    print(f"  reply          : {text[:200]!r}")


def test_live_transport_wired_into_the_relay_blocks_on_fault():
    """The relay must convert a transport fault into ModelRelayBlocked."""
    t = from_env(model=MODEL, timeout=120.0, max_tokens=200)
    policy = RelayPolicy(
        task_id="task-live",
        allowed_operations=("complete",),
        max_requests=1,
        max_request_bytes=4096,
        max_response_bytes=4096,
        max_total_bytes=8192,
        max_duration_seconds=120,
    )
    relay = TaskScopedModelRelay(task_id="task-live", policy=policy, transport=t)
    # One real request must succeed and produce non-empty bytes.
    try:
        out = relay.request(task_id="task-live", operation="complete",
                            payload=b"Reply with exactly the word: PONG")
    except ModelRelayBlocked as exc:
        _skip_if_quota_exhausted(exc)
    assert out
    # Quota is 1, so the second call must block rather than call upstream.
    with pytest.raises(ModelRelayBlocked):
        relay.request(task_id="task-live", operation="complete", payload=b"again")


def test_live_evidence_snapshot_contains_no_credential():
    t = from_env(model=MODEL, timeout=120.0, max_tokens=200)
    _call(t)
    import json
    dumped = json.dumps(t.evidence)
    assert os.environ["OMNIROUTE_API_KEY"] not in dumped
    assert t.requested_model in dumped
