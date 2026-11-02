"""Tests for scripts/source_gen.py (M1).

Uses real subprocess invocation and temp git repos where the round-trip
through review_gate.py check #5 is needed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "source_gen.py"


def _run(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        cwd=cwd or str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _write_fake_artifacts(tmp_path):
    """Create the four standard inner-loop artifacts under tmp_path/docs/."""
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "prime-module-map.html").write_text("<html>prime</html>", encoding="utf-8")
    (docs / "plan-tweakable.html").write_text("<html>plan</html>", encoding="utf-8")
    (docs / "dev-log.md").write_text("# Dev Log\n", encoding="utf-8")
    (docs / "validate-prototype.html").write_text("<html>validate</html>", encoding="utf-8")
    return docs


def test_happy_path_writes_source_json(tmp_path):
    docs = _write_fake_artifacts(tmp_path)
    spec = docs / "spec.md"
    spec.write_text(
        "## Milestone 1\n\n## Acceptance criteria\n\n- AC1: payload embedded\n- AC2: offline works\n",
        encoding="utf-8",
    )

    result = _run([
        f"--bead=school-core-fip",
        f"--spec={spec}",
        "--spec-milestone=M3",
        "--decisions=D2,D4",
        "--artifacts=" + ",".join([
            str(docs / "prime-module-map.html"),
            str(docs / "plan-tweakable.html"),
            str(docs / "dev-log.md"),
            str(docs / "validate-prototype.html"),
        ]),
        f"--out={tmp_path / 'source.json'}",
    ], cwd=str(tmp_path))

    assert result.returncode == 0, result.stderr
    out = json.loads((tmp_path / "source.json").read_text(encoding="utf-8"))
    assert out["bead"] == "school-core-fip"
    assert "spec.md" in out["spec"]
    assert out["decisions"] == ["D2", "D4"]
    assert len(out["artifacts"]) == 4
    assert "AC1" in out["spec_acceptance"]


def test_missing_bead_exits_non_zero(tmp_path):
    _write_fake_artifacts(tmp_path)
    spec = Path(tmp_path) / "docs" / "spec.md"
    spec.write_text("## Acceptance criteria\n\n- AC1\n", encoding="utf-8")

    result = _run([
        "--bead=",
        f"--spec={spec}",
        "--spec-milestone=M3",
        "--decisions=D1",
        "--artifacts=" + str(tmp_path / "docs" / "prime-module-map.html"),
        f"--out={tmp_path / 'source.json'}",
    ], cwd=str(tmp_path))

    assert result.returncode == 2
    assert "bead must be non-empty" in result.stderr


def test_missing_artifact_exits_non_zero(tmp_path):
    docs = _write_fake_artifacts(tmp_path)
    spec = Path(tmp_path) / "docs" / "spec.md"
    spec.write_text("## Acceptance criteria\n\n- AC1\n", encoding="utf-8")

    result = _run([
        "--bead=school-core-fip",
        f"--spec={spec}",
        "--spec-milestone=M3",
        "--decisions=D1",
        "--artifacts=" + str(docs / "prime-module-map.html") + ",docs/missing.html",
        f"--out={tmp_path / 'source.json'}",
    ], cwd=str(tmp_path))

    assert result.returncode == 2
    assert "not found" in result.stderr


def test_missing_acceptance_extraction_requires_flag(tmp_path):
    _write_fake_artifacts(tmp_path)
    spec = Path(tmp_path) / "docs" / "spec.md"
    # No acceptance section at all
    spec.write_text("## Milestone 1\n\nNothing here.\n", encoding="utf-8")

    result = _run([
        "--bead=school-core-fip",
        f"--spec={spec}",
        "--spec-milestone=M3",
        "--decisions=D1",
        "--artifacts=" + str(tmp_path / "docs" / "prime-module-map.html"),
        f"--out={tmp_path / 'source.json'}",
    ], cwd=str(tmp_path))

    assert result.returncode == 2
    assert "acceptance criteria" in result.stderr.lower()


def test_spec_acceptance_flag_overrides_spec(tmp_path):
    _write_fake_artifacts(tmp_path)
    spec = Path(tmp_path) / "docs" / "spec.md"
    spec.write_text("## Milestone 1\n\nNo acceptance section.\n", encoding="utf-8")

    result = _run([
        "--bead=school-core-fip",
        f"--spec={spec}",
        "--spec-milestone=M3",
        "--decisions=D1",
        "--artifacts=" + str(tmp_path / "docs" / "prime-module-map.html"),
        "--spec-acceptance=verbatim criteria here",
        f"--out={tmp_path / 'source.json'}",
    ], cwd=str(tmp_path))

    assert result.returncode == 0, result.stderr
    out = json.loads((tmp_path / "source.json").read_text(encoding="utf-8"))
    assert out["spec_acceptance"] == "verbatim criteria here"


def test_round_trips_through_review_gate_check_5(tmp_path):
    """A PR that adds a valid source.json must pass review_gate.py check #5."""
    docs = _write_fake_artifacts(tmp_path)
    spec = docs / "spec.md"
    spec.write_text(
        "## Milestone 1\n\n## Acceptance criteria\n\n- AC1: embedded payload\n",
        encoding="utf-8",
    )

    # Build source.json
    result = _run([
        f"--bead=school-core-fip",
        f"--spec={spec}",
        "--spec-milestone=M3",
        "--decisions=D2,D4",
        "--artifacts=" + ",".join([
            str(docs / "prime-module-map.html"),
            str(docs / "plan-tweakable.html"),
            str(docs / "dev-log.md"),
            str(docs / "validate-prototype.html"),
        ]),
        f"--out={tmp_path / 'docs' / 'source.json'}",
    ], cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr

    # Create a git repo with the source.json in a diff
    import subprocess as sp
    run = lambda *a: sp.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    # Base commit: README only
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "base")
    # Head commit: add source.json + a test file (so check 2 passes)
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "add source.json school-core-fip")

    gate = sp.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "review_gate.py"),
         "--base", "HEAD~1", "--head", "HEAD", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    data = json.loads(gate.stdout)
    assert gate.returncode == 0, data
    assert data["pass"] is True
    assert not any(f["check"] == "source-block" for f in data["failures"])
