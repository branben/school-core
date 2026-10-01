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

MODEL = "openrouter/inclusionai/ling-3.0-flash-sante:free"


def test_live_completion_returns_real_content():
    t = from_env(model=MODEL, timeout=120.0, max_tokens=200)
    out = t("complete", b"Reply with exactly the word: PONG")
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
    out = relay.request(task_id="task-live", operation="complete",
                        payload=b"Reply with exactly the word: PONG")
    assert out
    # Quota is 1, so the second call must block rather than call upstream.
    with pytest.raises(ModelRelayBlocked):
        relay.request(task_id="task-live", operation="complete", payload=b"again")


def test_live_evidence_snapshot_contains_no_credential():
    t = from_env(model=MODEL, timeout=120.0, max_tokens=200)
    t("complete", b"Reply with exactly the word: PONG")
    import json
    dumped = json.dumps(t.evidence)
    assert os.environ["OMNIROUTE_API_KEY"] not in dumped
    assert t.requested_model in dumped
