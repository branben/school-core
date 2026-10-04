#!/usr/bin/env python3
"""Probe fleet models for reachability, deduplicated by model.

Separate from `fleet_config_audit.py` on purpose. That script is free and
instant because it only reads configuration. This one spends requests and
time. Fusing them behind a `--live` flag would produce a check that is green
half the time by construction.

## Why deduplicate

20 profiles use 3 distinct models:

    14  meituan/longcat-2.5-preview:free
     5  poolside/laguna-s-2.1:free
     1  stealth/space-bunny-alpha

Probing per profile spends 20 requests to learn 3 facts. This spends 3.

The 2026-10-01 outage was NOT caused by probe volume -- it was caused by a
14-model sweep with no per-request timeout and no stop condition. Three
bounded calls are a different thing entirely. Every call here is bounded and
the total is capped by the number of distinct models, which is small by
construction.

## Two lessons this encodes

1. **Reachability facts expire on a schedule nobody controls.** A model can be
   retired upstream at any moment. The TTL below is a lie about that, kept
   short, and `--max-age 0` forces a fresh probe.

2. **A TTL is not a substitute for seeing real failures.** Retirement notices
   are machine-readable ("This model's free period has ended"), so any honest
   run already learns the fact for free. `record_failure()` lets a worker write
   what it saw; the probe only fills gaps on a schedule.

## Exit codes

    0  every distinct model probed OK (or cached-OK and fresh)
    1  at least one model unreachable / retired
    2  audit could not run at all
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

PROFILES = Path(os.path.expanduser("~/.hermes/profiles"))
CACHE = Path(os.path.expanduser("~/.hermes/fleet/model-probe.json"))
HERMES = os.path.expanduser("~/.local/bin/hermes")

# Bounded on purpose. The outage sweep had no timeout and hung the cell.
PER_PROBE_TIMEOUT = 90
# Retrying a retired model cannot succeed; retrying a timeout might.
DEFAULT_RETRIES = 1


# --- retirement notices -----------------------------------------------------
# These are the exact phrases a provider uses to say "stop asking". Matching
# them is what lets a probe distinguish "this model is gone" from "I hit a
# rate limit" -- a rate limit must NOT be recorded as retirement, or the cache
# poisons a healthy model for a full TTL.
RETIREMENT_MARKERS = (
    "free period has ended",
    "model has been retired",
    "model is deprecated",
    "no longer available",
    "unavailable for free",
    "model not found",
    "not_found",
    "model does not exist",
    "unknown model",
)

TRANSIENT_MARKERS = (
    "rate limit",
    "too many requests",
    "429",
    "out of credits",
    "per-model billing",
    "quota",
    "insufficient",
    "overloaded",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "didn't answer",
    "service unavailable",
    "503",
    "internal server error",
    "bad gateway",
)


def classify(text: str) -> str:
    """'retired' | 'transient' | 'unknown'.

    Retired wins over transient because a body can contain both -- a gateway
    may report quota and append a retirement notice, and only one of those
    means the model will never come back.
    """
    low = text.lower()
    if any(m in low for m in RETIREMENT_MARKERS):
        return "retired"
    if any(m in low for m in TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def record_failure(cache: dict, model: str, verdict: str, detail: str) -> None:
    """Entry point for a worker that already saw a failure.

    Only a 'retired' verdict writes a definitive entry -- a transient error
    must not poison the cache.
    """
    if verdict != "retired":
        return
    cache[model] = {
        "verdict": "retired",
        "detail": detail[:400],
        "checked_at": time.time(),
        "source": "observed",
    }
    save_cache(cache)


def distinct_models() -> dict[str, list[str]]:
    model: dict[str, list[str]] = {}
    for d in sorted(PROFILES.iterdir()) if PROFILES.exists() else []:
        cfg_path = d / "config.yaml"
        if not cfg_path.is_file():
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except yaml.YAMLError:
            continue
        m = (cfg.get("model") or {}).get("default")
        if m:
            model.setdefault(m, []).append(d.name)
    return model


def probe(model: str, retries: int = DEFAULT_RETRIES) -> tuple[str, str]:
    """Return (verdict, detail). Never raises, never hangs past the timeout."""
    for attempt in range(retries + 1):
        title = f"probe-{int(time.time())}-{attempt}"
        try:
            r = subprocess.run(
                [HERMES, "-p", "lucas", "chat", "--in", str(Path.home()),
                 "-c", title, "--create-if-missing", "-Q",
                 "-q", "Reply with exactly: PROBE_OK"],
                capture_output=True, text=True,
                timeout=PER_PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            if attempt == retries:
                return "transient", f"timeout after {PER_PROBE_TIMEOUT}s"
            continue

        out = (r.stdout or "") + (r.stderr or "")
        verdict = classify(out)
        if verdict == "unknown" and r.returncode != 0:
            verdict = "unknown"
        if verdict in ("retired", "transient") and attempt < retries:
            continue
        if verdict == "unknown" and "PROBE_OK" in out:
            return "ok", "PROBE_OK"
        return verdict, out.strip()[-300:] or f"exit={r.returncode}"
    return "unknown", "exhausted retries"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=int, default=6 * 3600,
                    help="seconds a cached result stays fresh (default 6h; 0 forces re-probe)")
    ap.add_argument("--dry-run", action="store_true", help="list distinct models and exit")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    args = ap.parse_args()

    models = distinct_models()
    if not models:
        print(f"no profiles found under {PROFILES}", file=sys.stderr)
        return 2

    total_profiles = sum(len(v) for v in models.values())
    print(f"{total_profiles} profiles -> {len(models)} distinct models\n")

    if args.dry_run:
        for m, profs in models.items():
            print(f"  {m}  ({len(profs)}: {', '.join(profs)})")
        print(f"\nprobes if run now: {len(models)} requests")
        return 0

    cache = load_cache()
    now = time.time()
    failures, probed, cached = [], 0, 0

    for model, profs in models.items():
        entry = cache.get(model)
        if entry and (now - entry.get("checked_at", 0)) < args.max_age:
            cached += 1
            state = f"cached({entry['verdict']})"
            if entry["verdict"] != "ok":
                failures.append((model, state, entry.get("detail", "")))
            print(f"  {model:38s} {state}")
            continue

        verdict, detail = probe(model, args.retries)
        probed += 1
        cache[model] = {"verdict": verdict, "detail": detail[:400],
                        "checked_at": now, "source": "probe"}
        state = f"{verdict}"
        if verdict != "ok":
            failures.append((model, state, detail))
            affected = ", ".join(profs[:4]) + ("..." if len(profs) > 4 else "")
            print(f"  {model:38s} {state}   <- {affected}")
        else:
            print(f"  {model:38s} {state}")

    save_cache(cache)
    print(f"\n{probed} probed, {cached} cached, {len(failures)} problem(s)")

    if failures:
        print("\nFAILURES:")
        for m, state, detail in failures:
            print(f"  - {m}: {state}\n      {detail.splitlines()[0][:200] if detail else ''}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
