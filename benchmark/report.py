#!/usr/bin/env python3
"""benchmark/report.py — HAL Benchmark report generator.

Reads runner JSON output and generates comparison tables, pass-rate
breakdowns, and per-model statistics.

Usage::

    python -m benchmark.runner --json --output results.json
    python -m benchmark.report results.json
    python -m benchmark.report results.json --markdown
    python -m benchmark.report results.json --domain code-implementation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional


def _color(text: str, code: int) -> str:
    """ANSI color wrapper."""
    return f"\033[{code}m{text}\033[0m"


def _pass_fail(mark: bool) -> str:
    return _color("PASS", 32) if mark else _color("FAIL", 31)


def _pct_color(pct: float) -> str:
    if pct >= 90:
        return _color(f"{pct:.0f}%", 32)
    elif pct >= 70:
        return _color(f"{pct:.0f}%", 33)
    else:
        return _color(f"{pct:.0f}%", 31)


def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {pct:.0f}%"


def report(
    results_path: Path,
    domain: Optional[str] = None,
    markdown: bool = False,
) -> str:
    """Generate a comparison report from runner JSON output.

    Returns a formatted string suitable for terminal or markdown output.
    """
    with open(results_path) as f:
        data = json.load(f)

    results = data.get("results", [])
    models = data.get("models", [])
    suite = data.get("suite", "?")
    elapsed = data.get("elapsed_total_ms", 0)

    # Filter by domain if requested
    if domain:
        results = [r for r in results if r.get("domain") == domain]
        case_ids = sorted(set(r["case_id"] for r in results))
    else:
        case_ids = sorted(set(r["case_id"] for r in results))

    lines = []

    if markdown:
        lines.append(f"# HAL Benchmark Report — `{suite}`")
        lines.append("")
        lines.append(f"**Elapsed:** {elapsed / 1000:.1f}s  |  "
                     f"**Cases:** {len(case_ids)}  |  "
                     f"**Models:** {len(models)}")
        lines.append("")
    else:
        lines.append(f"{'='*70}")
        lines.append(f"  HAL Benchmark Report — {suite}")
        lines.append(f"{'='*70}")
        lines.append(f"  Elapsed: {elapsed / 1000:.1f}s  |  "
                     f"Cases: {len(case_ids)}  |  Models: {len(models)}")
        lines.append("")

    # ── Per-model summary ────────────────────────────────────────────────

    if markdown:
        lines.append("## Per-Model Summary")
        lines.append("")
        lines.append("| Model | Passed | Total | Rate | Avg Chars | Avg ms |")
        lines.append("|-------|--------|-------|------|-----------|--------|")
    else:
        lines.append(f"  {'Model':35s}  {'Passed':>6}  {'Rate':>6}  {'Avg Chars':>10}  {'Avg ms':>8}")
        lines.append(f"  {'-'*35}  {'-'*6}  {'-'*6}  {'-'*10}  {'-'*8}")

    for model in models:
        mrs = [r for r in results if r["model"] == model]
        passed = sum(1 for r in mrs if r["passed"])
        total = len(mrs)
        pct = (passed / total * 100) if total else 0
        avg_len = sum(r["response_length"] for r in mrs) // total if total else 0
        avg_ms = sum(r["elapsed_ms"] for r in mrs) // total if total else 0

        if markdown:
            lines.append(
                f"| `{model}` | {passed} | {total} | "
                f"{pct:.0f}% | {avg_len} | {avg_ms} |"
            )
        else:
            lines.append(
                f"  {model:35s}  {passed:>4}/{total:<4}  {_pct_color(pct):>6}  "
                f"{avg_len:>10,}  {avg_ms:>8,}"
            )

    lines.append("")

    # ── Per-case matrix ───────────────────────────────────────────────────

    if markdown:
        lines.append("## Case-by-Case Matrix")
        lines.append("")
        header = "| Case | " + " | ".join(f"`{m[:20]}`" for m in models) + " |"
        sep = "|------|" + "|".join(["------" for _ in models]) + "|"
        lines.append(header)
        lines.append(sep)
    else:
        lines.append(f"  {'Case':40s}  " + "  ".join(f"{'Model':>25}" for _ in models))
        lines.append(f"  {'-'*40}  " + "  ".join(f"{'-'*25}" for _ in models))

    for cid in case_ids:
        row = []
        for model in models:
            mr = [r for r in results if r["case_id"] == cid and r["model"] == model]
            if mr:
                r = mr[0]
                cell = f"{_pass_fail(r['passed'])} ({r['response_length']}c)"
            else:
                cell = "—"
            row.append(cell)

        if markdown:
            lines.append(f"| `{cid}` | " + " | ".join(row) + " |")
        else:
            lines.append(f"  {cid:40s}  " + "  ".join(f"{c:>25}" for c in row))

    lines.append("")

    # ── Visual score bars ─────────────────────────────────────────────────

    if not markdown:
        lines.append(f"  {'Model':35s}  Score")
        lines.append(f"  {'-'*35}  {'-'*25}")
        for model in models:
            mrs = [r for r in results if r["model"] == model]
            passed = sum(1 for r in mrs if r["passed"])
            pct = (passed / len(mrs) * 100) if mrs else 0
            lines.append(f"  {model:35s}  {_bar(pct)}")

    lines.append("")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────


def _main():
    import argparse

    p = argparse.ArgumentParser(
        description="HAL Benchmark report — read JSON results, generate comparison",
    )
    p.add_argument("results", help="Path to runner JSON output")
    p.add_argument("--domain", default=None, help="Filter by domain")
    p.add_argument("--markdown", action="store_true", help="Output markdown table")
    args = p.parse_args()

    print(report(
        results_path=Path(args.results),
        domain=args.domain,
        markdown=args.markdown,
    ))


if __name__ == "__main__":
    _main()
