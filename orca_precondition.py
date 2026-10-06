#!/usr/bin/env python3
"""Orca precondition probe for the School Loop execute job.

Why this exists
---------------
`.github/workflows/school-loop.yml` runs the execute job under
`set -euo pipefail`. The old step was:

    if ! orca status --json >/dev/null 2>&1; then orca open --json >/dev/null; fi
    orca repo add --path "$PWD" --json

`orca status` SUCCEEDS (rc=0) while `orca repo add` cannot reach the runtime and
returns `{"code":"runtime_unavailable"}` — and because `set -e` treats that
non-zero rc as fatal, the whole job died at "Register checkout with Orca". Steps
6-11, including "Run bridge loop (executes issues)", were SKIPPED. Measured:
21 of the last 30 School Loop runs failed this way, and the bridge did no work
for days while the run went red for an environment reason.

The fix is not to swallow the failure — it is to classify it. This probe:

  * exits 0 (READY)   when `orca repo add` actually registers the checkout,
  * exits 1 (BLOCKED_ENV) when the runtime is unreachable, emitting a
    `::error::BLOCKED_ENV ...` annotation so the red run is self-describing,
  * prints `::warning::` when it cannot even run (missing CLI) but still exits 1.

The workflow step runs this with `continue-on-error: true` and records the
outcome in a step output, so the job reports BLOCKED_ENV and the bridge loop
still runs (and degrades loudly) instead of being silently skipped.

Exit codes
----------
0  READY        — checkout registered with a reachable Orca runtime
1  BLOCKED_ENV  — environment precondition unmet (runtime unreachable / CLI absent)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

READY = 0
BLOCKED_ENV = 1

# `orca repo add` returns this when the desktop runtime is not reachable over
# its WebSocket. It is an environment condition, not a defect in the checkout.
RUNTIME_UNAVAILABLE = "runtime_unavailable"


def _run_orca(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["orca", *args, "--json"],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _classify_repo_add(rc: int, stdout: str, stderr: str) -> tuple[int, str]:
    """Return (exit_code, message) for the `orca repo add` outcome."""
    parsed = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None

    if isinstance(parsed, dict) and parsed.get("code") == RUNTIME_UNAVAILABLE:
        return BLOCKED_ENV, (
            "orca repo add could not reach the Orca runtime "
            f"(code={RUNTIME_UNAVAILABLE})"
        )
    if rc != 0:
        detail = (stderr.strip() or stdout.strip())[:300]
        return BLOCKED_ENV, f"orca repo add failed (rc={rc}): {detail}"
    return READY, "orca repo add registered the checkout"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_path = Path(argv[0]).resolve() if argv else Path.cwd().resolve()

    if shutil.which("orca") is None:
        print("::warning::orca CLI not found on PATH — cannot register checkout")
        print(f"::error::BLOCKED_ENV orca CLI missing (repo_path={repo_path})")
        return BLOCKED_ENV

    # `orca status` is necessary but NOT sufficient: it returns 0 even when
    # `orca repo add` cannot connect. Probe it, repair once if down, then verify
    # with the operation that actually matters.
    try:
        status_rc, _, _ = _run_orca(["status"], timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"::warning::orca status probe failed: {exc}")
        status_rc = 1

    if status_rc != 0:
        print("orca runtime unreachable — launching it (idempotent: `orca open`)")
        try:
            _run_orca(["open"], timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"::warning::orca open failed: {exc}")

    try:
        rc, stdout, stderr = _run_orca(["repo", "add", "--path", str(repo_path)])
    except subprocess.TimeoutExpired:
        print("::error::BLOCKED_ENV orca repo add timed out")
        return BLOCKED_ENV
    except OSError as exc:
        print(f"::error::BLOCKED_ENV orca repo add could not execute: {exc}")
        return BLOCKED_ENV

    code, message = _classify_repo_add(rc, stdout, stderr)
    if code == READY:
        print(message)
        return READY

    # BLOCKED_ENV: name the condition explicitly so the red run is not
    # indistinguishable from a real defect, and never silently skip the bridge.
    print(f"::error::BLOCKED_ENV {message}")
    print(
        "The bridge loop still runs and will report its own degradation; "
        "this cycle did no Orca-backed work."
    )
    return BLOCKED_ENV


if __name__ == "__main__":
    sys.exit(main())
