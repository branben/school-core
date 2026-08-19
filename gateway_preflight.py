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
* **No retries.** This is a liveness question, not a work attempt. Retrying here
  would reintroduce the delay we are trying to eliminate.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional, Tuple

# Default probe target. Mirrors executor.OMNIROUTE_BASE's host:port — the same
# process serves /v1 and /a2a, so one probe covers both transports.
DEFAULT_GATEWAY_URL = os.environ.get(
    "OMNIROUTE_BASE_URL", "http://localhost:20128/v1"
).rstrip("/") + "/models"

# Deliberately short. A preflight is allowed to be wrong-but-fast; a hung
# preflight is strictly worse than no preflight.
DEFAULT_TIMEOUT_SECONDS = 5


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


def check_gateway(
    url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[bool, str]:
    """Return ``(healthy, detail)`` for the gateway.

    Never raises: a preflight that explodes is just another failure mode to
    handle at the call site. ``detail`` is short and log-ready.
    """
    target = url or DEFAULT_GATEWAY_URL
    try:
        status, reason = _probe(target, timeout)
    except urllib.error.URLError as e:
        return False, f"unreachable: {e.reason}"
    except TimeoutError as e:
        return False, f"probe timed out after {timeout}s: {e}"
    except OSError as e:
        return False, f"unreachable: {e}"
    except Exception as e:  # unexpected transport shape — still a failure
        return False, f"probe error: {type(e).__name__}: {e}"

    if 200 <= status < 300:
        return True, f"HTTP {status}"
    if status in (401, 403):
        # Reachable but unauthorized is NOT healthy: every model call will fail
        # exactly as it does when the process is absent.
        return False, f"HTTP {status} {reason} — gateway reachable but not authorized"
    return False, f"HTTP {status} {reason}"


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
    """
    healthy, detail = check_gateway()
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
