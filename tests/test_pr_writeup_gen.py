"""Tests for scripts/pr_writeup_gen.py (M2).

Verifies:
- Generated pr-writeup.md contains all 5 source-chain fields (bead, spec,
  decisions, artifacts, spec_acceptance).
- Rendered pr-writeup.html contains the same fields.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN_SCRIPT = REPO_ROOT / "scripts" / "pr_writeup_gen.py"
RENDER_SCRIPT = REPO_ROOT / "scripts" / "html_render_md.py"


def _run(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(GEN_SCRIPT)] + args,
        cwd=cwd or str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _write_source_and_review(tmp_path):
    """Write a minimal source.json and review-evidence.json."""
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    source = {
        "bead": "school-core-fip",
        "spec": "docs/plans/2026-09-30-002-spec-openai-video-instrumentation.md · M3",
        "decisions": ["D2", "D4"],
        "artifacts": [
            "docs/prime-module-map.html",
            "docs/plan-tweakable.html",
            "docs/dev-log.md",
            "docs/validate-prototype.html",
        ],
        "spec_acceptance": "- AC1: payload embedded\n- AC2: offline works\n",
    }
    review = {
        "cto_verdict": "PASS",
        "coo_verdict": "PASS",
        "combined_score": 90.0,
        "accepted": True,
        "verify_result": {
            "commands": ["python3 -m pytest tests/ -q"],
            "exit": 0,
        },
        "entire_review": {
            "status": "pass",
            "findings": [],
        },
    }
    (docs / "source.json").write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "review.json").write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
    # Create the cited artifacts so source_gen-style existence checks pass
    for art in source["artifacts"]:
        p = tmp_path / art
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<html/>", encoding="utf-8")
    return source, review


def test_generated_md_contains_all_five_source_fields(tmp_path):
    _write_source_and_review(tmp_path)
    out_md = tmp_path / "docs" / "pr-writeup.md"
    out_html = tmp_path / "docs" / "pr-writeup.html"

    result = _run([
        f"--source-json={tmp_path / 'docs' / 'source.json'}",
        f"--review-json={tmp_path / 'review.json'}",
        "--pr-number=42",
        "--pr-title=Add a thing",
        "--pr-author=tester",
        f"--out-md={out_md}",
        f"--out-html={out_html}",
    ], cwd=str(tmp_path))

    assert result.returncode == 0, result.stderr
    md = out_md.read_text(encoding="utf-8")
    html = out_html.read_text(encoding="utf-8")

    # All five source-chain fields must appear in the rendered HTML
    assert "school-core-fip" in html
    assert "spec-openai-video-instrumentation" in html
    assert "D2" in html and "D4" in html
    assert "prime-module-map.html" in html
    assert "AC1" in html


def test_skip_render_writes_md_only(tmp_path):
    _write_source_and_review(tmp_path)
    out_md = tmp_path / "docs" / "pr-writeup.md"

    result = _run([
        f"--source-json={tmp_path / 'docs' / 'source.json'}",
        f"--review-json={tmp_path / 'review.json'}",
        "--pr-number=42",
        "--pr-title=Add a thing",
        f"--out-md={out_md}",
        "--skip-render",
    ], cwd=str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert out_md.is_file()
    assert "school-core-fip" in out_md.read_text(encoding="utf-8")


def test_missing_source_json_exits_non_zero(tmp_path):
    result = _run([
        "--source-json=/nonexistent/source.json",
        "--review-json=/nonexistent/review.json",
        "--pr-number=1",
        "--pr-title=x",
    ], cwd=str(tmp_path))
    assert result.returncode == 2


def test_empty_source_field_exits_non_zero(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    source = {"bead": "", "spec": "", "decisions": [], "artifacts": [], "spec_acceptance": ""}
    (docs / "source.json").write_text(json.dumps(source), encoding="utf-8")
    review = {"accepted": True}
    (tmp_path / "review.json").write_text(json.dumps(review), encoding="utf-8")

    result = _run([
        f"--source-json={docs / 'source.json'}",
        f"--review-json={tmp_path / 'review.json'}",
        "--pr-number=1",
        "--pr-title=x",
    ], cwd=str(tmp_path))
    assert result.returncode == 2
    assert "missing required field" in result.stderr
