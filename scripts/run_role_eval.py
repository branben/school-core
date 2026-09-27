#!/usr/bin/env python3
"""Direct harness for the OmniRoute 'school-core role routing eval' suite.

Same cases, same assertion strategies (contains / regex) as the suite
persisted in OmniRoute (suite a0846fb1), executed straight against
/v1/chat/completions. Used because OmniRoute's run endpoint is a Next.js
server action with no reachable REST contract.

Usage:  python3 scripts/run_role_eval.py [--repeats N] [--out results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

SUITE_ID = "a0846fb1-92e9-4954-995e-8be508b731c6"
GATEWAY = "http://localhost:20128"
KEY_ENV = os.path.expanduser("~/.omniroute/.env")

MODELS = [
    "mistral/codestral-latest",
    "gemini/gemini-3-flash-preview",
    "bazaarlink/deepseek-v3.2",
]


def load_key() -> str:
    with open(KEY_ENV) as fh:
        for line in fh:
            if line.startswith("OMNIROUTE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("OMNIROUTE_API_KEY not found")


def load_suite(key: str) -> dict:
    out = subprocess.run(
        ["curl", "-s", "-m", "10", "-H", f"Authorization: Bearer {key}",
         f"{GATEWAY}/api/evals/suites/{SUITE_ID}"],
        capture_output=True, text=True).stdout
    return json.loads(out)["suite"]


def ask(key: str, model: str, messages: list, timeout: int = 75):
    t0 = time.time()
    payload = json.dumps({"model": model, "messages": messages, "max_tokens": 800})
    out = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "-X", "POST",
         "-H", "Content-Type: application/json",
         "-H", f"Authorization: Bearer {key}",
         "-d", payload, f"{GATEWAY}/v1/chat/completions"],
        capture_output=True, text=True).stdout
    elapsed = time.time() - t0
    try:
        data = json.loads(out)
        if "error" in data:
            return elapsed, None, str(data["error"].get("message", ""))[:60]
        return elapsed, data["choices"][0]["message"]["content"], ""
    except Exception:
        return elapsed, None, "TIMEOUT/UNPARSEABLE"


def check(case: dict, text: str | None) -> bool:
    """Mirrors the suite's declared strategies: contains / regex."""
    if text is None:
        return False
    exp = case["expected"]
    if exp["strategy"] == "contains":
        return exp["value"].lower() in text.lower()
    if exp["strategy"] == "regex":
        return bool(re.search(exp["value"], text))
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default="role_eval_results.json")
    args = ap.parse_args()

    key = load_key()
    suite = load_suite(key)
    cases = suite["cases"]
    jobs = [(m, c, i) for m in MODELS for c in cases for i in range(args.repeats)]

    def run(job):
        model, case, _ = job
        dt, text, err = ask(key, model, case["input"]["messages"])
        return model, case["id"], check(case, text), dt, err

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(run, jobs))

    out: dict = {}
    for model, cid, ok, dt, err in rows:
        r = out.setdefault(model, {"times": [], "cases": {}, "errors": []})
        r["times"].append(dt)
        r["cases"].setdefault(cid, []).append(ok)
        if err:
            r["errors"].append(err)

    print(f"\nSCHOOL-CORE ROLE EVAL — {len(cases)} cases x {args.repeats} repeats "
          f"= {len(jobs)} calls\n")
    print(f"{'MODEL':<36} {'PASS':>6} {'AVG':>7} {'MAX':>7} {'SPREAD':>7}  PER-CASE")
    print("-" * 104)
    summary = {}
    for model, r in out.items():
        t = r["times"]
        passed = sum(sum(v) for v in r["cases"].values())
        total = sum(len(v) for v in r["cases"].values())
        per = " ".join(f"{k}:{sum(v)}/{len(v)}" for k, v in r["cases"].items())
        summary[model] = {
            "passRate": round(100 * passed / total),
            "avg": round(statistics.mean(t), 1),
            "max": round(max(t), 1),
            "spread": round(max(t) - min(t), 1),
            "perCase": per,
        }
        print(f"{model:<36} {summary[model]['passRate']:>5}% {summary[model]['avg']:>6.1f}s "
              f"{summary[model]['max']:>6.1f}s {summary[model]['spread']:>6.1f}s  {per}")

    with open(args.out, "w") as fh:
        json.dump({"suiteId": SUITE_ID, "repeats": args.repeats, "summary": summary}, fh, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
