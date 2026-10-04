"""The relay and the fleet probe hold DUPLICATED marker lists.

The duplication is deliberate -- importing `scripts/` from a production import
path would couple the relay to the repo layout, and the relay is meant to be
droppable. The cost is exactly this: one copy can gain a marker the other lacks,
and the two then disagree about whether a model is retired.

That is not hypothetical. On 2026-10-02 `unavailable for free` was added to both
files, and a parity check run inside a long-lived interpreter still reported
drift -- because that interpreter still held the pre-edit module. The code was
correct and the measurement was wrong, which is the worst combination: it looks
like a real defect and invites a "fix" to already-correct code.

So: assert equality of the lists themselves. Cheap, and it fails loudly the
moment someone edits one copy.
"""

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_probe():
    """Import the probe script by path, isolated from any cached import."""
    spec = importlib.util.spec_from_file_location(
        "_fleet_model_probe_under_test", REPO / "scripts" / "fleet_model_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe():
    return _load_probe()


def test_retirement_markers_match(probe):
    from omni_relay import _RETIREMENT_MARKERS

    assert set(_RETIREMENT_MARKERS) == set(probe.RETIREMENT_MARKERS), (
        "retirement markers drifted between omni_relay.py and "
        "scripts/fleet_model_probe.py -- a retired model would be reported by "
        "one and ignored by the other"
    )


def test_transient_markers_match(probe):
    from omni_relay import _TRANSIENT_MARKERS

    assert set(_TRANSIENT_MARKERS) == set(probe.TRANSIENT_MARKERS), (
        "transient markers drifted -- a rate limit could be recorded as a "
        "retirement by one copy and dropped by the other"
    )


def test_both_classify_real_logged_errors_identically(probe):
    """Verbatim strings from OmniRoute `call_logs`, not invented phrasings."""
    from omni_relay import _classify_upstream_failure

    real_errors = [
        "[404]: This model is unavailable for free. The paid version is "
        "available now - use this slug in your request",
        "[404]: This model's free period has ended.",
        "HTTP 429: Rate limit exceeded: free-models-per-day",
        "Model openrouter/auto out of credits (per-model billing)",
        "Model qwen/qwen3-coder:free not_found",
        "Nous Portal didn't answer after 3 attempts - temporarily unavailable",
    ]
    for err in real_errors:
        assert _classify_upstream_failure(err) == probe.classify(err), (
            f"copies disagree on {err[:60]!r}"
        )
