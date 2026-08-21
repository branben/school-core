"""Gateway preflight — fail fast instead of retry-looping against a dead gateway.

``executor.py`` points both transports at one process::

    OMNIROUTE_BASE = "http://localhost:20128/v1"
    A2A_BASE       = "http://localhost:20128/a2a"

When that process is down, every model call fails, ``_a2a_poll`` burns its
120-second deadline per attempt, retry pressure accumulates until the crew
denies admission, and the job grinds until GitHub's ``timeout-minutes`` kills
it. Measured cost: 62 of 63 School Loop runs cancelled at ~31 minutes, one
issue processed per run.

This module answers one question cheaply and definitively — *can we reach the
gateway at all?* — so a caller can end the cycle in seconds with a loud,
alerting failure instead of a silent 30-minute grind that reports as the
ambiguous ``cancelled``.

Design notes:

* **A short, explicit timeout.** The probe must never inherit the 120s call
  timeout; a preflight that hangs defeats its own purpose.
* **Auth failures count as down.** A reachable-but-unauthorized gateway (401 or
  403) produces the same grind as an absent one, so it is not "healthy".
* **No retries on the happy path.** This is a liveness question, not a work
  attempt. A healthy gateway answers in ~0.03s and must never pay a retry cost.
* **One warmup retry, on a retryable failure only.** A cold gateway compiles the
  route on first hit and can exceed the probe budget; a dead one refuses
  instantly. Those are different states and the probe must not conflate them.
  See :func:`check_gateway_with_warmup`.
"""

from __future__ import annotations

import errno
import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

# Default probe target. Mirrors executor.OMNIROUTE_BASE's host:port — the same
# process serves /v1 and /a2a, so one probe covers both transports.
DEFAULT_GATEWAY_URL = os.environ.get(
    "OMNIROUTE_BASE_URL", "http://localhost:20128/v1"
).rstrip("/") + "/models"

# Deliberately short. A preflight is allowed to be wrong-but-fast; a hung
# preflight is strictly worse than no preflight.
#
# 20s, not 5s: measured a 17.5s FIRST response on a freshly-started gateway
# (Next.js compiles the route on first hit), then 0.02s steady-state on the
# next three probes. A 5s budget therefore fails a healthy-but-cold gateway,
# which is exactly the state right after a reboot or a restart — the moment
# this check matters most. 20s still fails ~90x faster than the 30-minute
# grind it replaces, and a genuinely dead gateway refuses the connection
# immediately (ECONNREFUSED) rather than consuming the budget.
#
# 20s is NOT raised to cover a slower cold start. Cold start is compile time and
# varies with machine load, so any fixed budget is a guess that eventually loses
# the race — and every increase makes the genuinely-dead case proportionally
# slower to detect, which is the one case this module exists to make fast.
# ``check_gateway_with_warmup`` handles cold starts by retrying instead, which
# costs nothing on a healthy gateway.
DEFAULT_TIMEOUT_SECONDS = 20

# Pause between the first failed probe and the warmup retry. Only ever paid on a
# path that was already going to fail, so it buys cold-start tolerance without
# taxing a green cycle.
DEFAULT_WARMUP_SLEEP_SECONDS = 10


class GatewayDown(RuntimeError):
    """The model/A2A gateway is not usable for this cycle."""


def _probe(url: str, timeout: int) -> Tuple[int, str]:
    """Issue one bounded GET and return ``(status_code, reason)``.

    Separated out so tests can substitute transport behaviour without a live
    socket. Raises on transport-level failure (refused, DNS, timeout).
    """
    key = os.environ.get("OMNIROUTE_API_KEY", "")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, "ok"
    except urllib.error.HTTPError as e:
        # Reachable but refusing to serve us — a real answer, not a transport
        # failure, so surface the status rather than raising.
        return e.code, str(e.reason)


def _is_connection_refused(exc: BaseException) -> bool:
    """True when *exc* means nothing is listening on the port.

    A refused connection is NOT a cold start: a compiling gateway has already
    accepted the socket and is simply slow to answer, whereas ECONNREFUSED means
    there is no listener at all. Retrying the latter only burns the warmup sleep,
    so the two must be distinguished — ``check_gateway`` collapses both into a
    single ``unreachable:`` string, which is fine for reporting but useless for
    deciding whether a retry is worthwhile.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if getattr(current, "errno", None) == errno.ECONNREFUSED:
            return True
        # urllib wraps the OSError in URLError.reason; __cause__ covers
        # explicit re-raises.
        nxt = getattr(current, "reason", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None
    return False


def _probe_outcome(url: str, timeout: int) -> Tuple[bool, str, bool]:
    """Return ``(healthy, detail, retryable)`` for a single probe.

    ``retryable`` is True only for failures a warmup retry could plausibly fix:
    a timeout or a transport hiccup. Connection-refused and HTTP-level answers
    (401/403/5xx) are decided answers, not warmup states.
    """
    try:
        status, reason = _probe(url, timeout)
    except urllib.error.URLError as e:
        return False, f"unreachable: {e.reason}", not _is_connection_refused(e)
    except TimeoutError as e:
        return False, f"probe timed out after {timeout}s: {e}", True
    except OSError as e:
        return False, f"unreachable: {e}", not _is_connection_refused(e)
    except Exception as e:  # unexpected transport shape — still a failure
        return False, f"probe error: {type(e).__name__}: {e}", False

    if 200 <= status < 300:
        return True, f"HTTP {status}", False
    if status in (401, 403):
        # Reachable but unauthorized is NOT healthy: every model call will fail
        # exactly as it does when the process is absent. Warmup cannot fix
        # credentials, so this is never retryable.
        return (
            False,
            f"HTTP {status} {reason} — gateway reachable but not authorized",
            False,
        )
    return False, f"HTTP {status} {reason}", False


def check_gateway(
    url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[bool, str]:
    """Return ``(healthy, detail)`` for the gateway.

    Single probe, no retry — see :func:`check_gateway_with_warmup` for the
    cold-start-tolerant wrapper. Never raises: a preflight that explodes is just
    another failure mode to handle at the call site. ``detail`` is short and
    log-ready.
    """
    target = url or DEFAULT_GATEWAY_URL
    healthy, detail, _ = _probe_outcome(target, timeout)
    return healthy, detail


def check_gateway_with_warmup(
    url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    warmup_sleep: float = DEFAULT_WARMUP_SLEEP_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Tuple[bool, str]:
    """Like :func:`check_gateway`, with ONE retry for a cold gateway.

    THE RACE THIS CLOSES: the gateway compiles its route on first hit (measured
    17.5s cold, then 0.02-0.03s steady-state). A cron that fires during warmup
    spends the whole preflight budget on that first compile and reports a healthy
    gateway as down, killing a cycle that would have worked seconds later.

    WHY RETRY RATHER THAN A BIGGER BUDGET: cold start is compile time and scales
    with machine load, so a fixed budget is a guess that eventually loses; and
    raising it slows detection of the genuinely-dead case this module exists to
    catch fast. A retry is paid ONLY on a failing path.

    COST CONTRACT:
      * healthy first probe  -> exactly one probe, NO sleep (~0.03s)
      * refused / 4xx / 5xx  -> exactly one probe, NO sleep (decided answer)
      * timeout or hiccup    -> two probes with one ``warmup_sleep`` between

    Each probe gets the full short ``timeout`` independently; the budget is never
    summed into one long hang.
    """
    target = url or DEFAULT_GATEWAY_URL
    healthy, detail, retryable = _probe_outcome(target, timeout)
    if healthy or not retryable:
        return healthy, detail

    sleep_fn(warmup_sleep)
    healthy, retry_detail, _ = _probe_outcome(target, timeout)
    if healthy:
        return True, f"{retry_detail} (after warmup retry; first probe: {detail})"
    return False, f"{retry_detail} (warmup retry also failed; first probe: {detail})"


def require_gateway(
    url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Raise :class:`GatewayDown` unless the gateway is usable.

    The message names the target and the reason so a CI log line is actionable
    without opening the source.
    """
    target = url or DEFAULT_GATEWAY_URL
    healthy, detail = check_gateway(url=target, timeout=timeout)
    if not healthy:
        raise GatewayDown(
            f"model/A2A gateway not usable at {target}: {detail}. "
            "Every model call and A2A escalation would fail; ending the cycle "
            "now instead of retry-looping until the job timeout."
        )


def main() -> int:
    """CLI entry point for a workflow preflight step.

    Exit 0 when healthy, 1 when not. Prints one line either way so the CI log
    says what happened without needing the exception text.

    Uses the warmup-tolerant check: this is the path a cron fires, so it is
    exactly where a cold gateway must not be mistaken for a dead one.
    """
    healthy, detail = check_gateway_with_warmup()
    payload = {"gateway": DEFAULT_GATEWAY_URL, "healthy": healthy, "detail": detail}
    print(json.dumps(payload))
    if not healthy:
        print(
            f"::error::model/A2A gateway unusable ({detail}) — "
            "skipping issue execution this cycle"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
