"""M3 guard test: verify inner-loop artifact generators are called in order.

The guard asserts that `_generate_inner_loop_artifacts` invokes source_gen.py
first, then pr_writeup_gen.py, with the correct arguments derived from the
issue, task_result, and review_evidence.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import issue_bridge
from issue_bridge import _generate_inner_loop_artifacts


def _base_issue():
    return {
        "bd_id": "school-core-fip",
        "title": "Add a thing",
        "number": 42,
    }


def _base_task_result():
    return {
        "spec": "docs/plans/2026-09-30-002-spec-openai-video-instrumentation.md",
        "bead": "school-core-fip",
    }


def _base_review_evidence():
    return {
        "cto_verdict": "PASS",
        "coo_verdict": "PASS",
        "combined_score": 90.0,
        "accepted": True,
        "decisions": ["D2", "D4"],
    }


def test_generators_called_in_order(tmp_path):
    """source_gen.py must be called before pr_writeup_gen.py."""
    docs = tmp_path / "docs"
    docs.mkdir()
    # Create the four standard artifacts so source_gen's existence check passes
    for name in ("prime-module-map.html", "plan-tweakable.html", "dev-log.md", "validate-prototype.html"):
        (docs / name).write_text("<html/>", encoding="utf-8")
    (docs / "source.json").write_text(json.dumps({
        "bead": "school-core-fip",
        "spec": "docs/plans/2026-09-30-002-spec-openai-video-instrumentation.md · M3",
        "decisions": ["D2", "D4"],
        "artifacts": [
            "docs/prime-module-map.html",
            "docs/plan-tweakable.html",
            "docs/dev-log.md",
            "docs/validate-prototype.html",
        ],
        "spec_acceptance": "- AC1: payload embedded\n",
    }), encoding="utf-8")

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch.object(issue_bridge.subprocess, "run", fake_run):
        _generate_inner_loop_artifacts(
            repo_path=tmp_path,
            issue=_base_issue(),
            task_result=_base_task_result(),
            review_evidence=_base_review_evidence(),
            verify_result={"passed": True},
            pr_number=42,
            pr_title="Add a thing",
        )

    assert len(calls) == 2, f"expected 2 subprocess calls, got {len(calls)}: {calls}"

    # First call: source_gen.py
    source_args = calls[0]
    assert source_args[1].endswith("source_gen.py")
    assert f"--bead=school-core-fip" in source_args
    assert "--spec-milestone=M3" in source_args
    assert "--decisions=D2,D4" in source_args
    out_idx = source_args.index(next(a for a in source_args if a.startswith("--out=")))
    out_val = source_args[out_idx]
    assert "source.json" in out_val

    # Second call: pr_writeup_gen.py
    writeup_args = calls[1]
    assert writeup_args[1].endswith("pr_writeup_gen.py")
    assert f"--source-json={docs / 'source.json'}" in writeup_args
    assert "--pr-number=42" in writeup_args
    assert "--pr-title=Add a thing" in writeup_args
    assert "--skip-render" not in writeup_args


def test_generator_failure_is_logged_not_raised(tmp_path):
    """A failing source_gen.py must not raise — it logs and returns."""
    docs = tmp_path / "docs"
    docs.mkdir()

    def fake_run(args, **kwargs):
        # Simulate source_gen.py failing
        if "source_gen.py" in str(args):
            return subprocess.CompletedProcess(args, 1, "", "ERROR: missing field\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch.object(issue_bridge.subprocess, "run", fake_run):
        # Should not raise
        _generate_inner_loop_artifacts(
            repo_path=tmp_path,
            issue=_base_issue(),
            task_result=_base_task_result(),
            review_evidence=_base_review_evidence(),
            verify_result=None,
            pr_number=42,
            pr_title="Add a thing",
        )


def test_review_evidence_json_is_cleaned_up(tmp_path):
    """The temp review_evidence.json must not survive after the call."""
    docs = tmp_path / "docs"
    docs.mkdir()

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    with patch.object(issue_bridge.subprocess, "run", fake_run):
        _generate_inner_loop_artifacts(
            repo_path=tmp_path,
            issue=_base_issue(),
            task_result=_base_task_result(),
            review_evidence=_base_review_evidence(),
            verify_result=None,
            pr_number=42,
            pr_title="Add a thing",
        )

    assert not (tmp_path / "review_evidence.json").exists(), (
        "temp review_evidence.json should be cleaned up after generation"
    )
