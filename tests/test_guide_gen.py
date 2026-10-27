"""Tests for scripts/guide_gen.py.

The load-bearing tests are the last two: they run the real ``plannotator``
binary against a guide this generator produced. Everything above them checks the
invariants the generator is supposed to guarantee, so a regression in the
generator fails here even where the tool is not installed.

Measured contract the generator targets (2026-10-26, real binary both ways):
``title``/``intent`` non-empty; ``sections`` 1..6; ``diffs[].file`` exact path
with ``b/`` stripped; a file in AT MOST one section; ``unplacedFiles`` optional.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("guide_gen", REPO / "scripts" / "guide_gen.py")
guide_gen = importlib.util.module_from_spec(_spec)
sys.modules["guide_gen"] = guide_gen
_spec.loader.exec_module(guide_gen)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

PATCH = """\
diff --git a/src/core/director.py b/src/core/director.py
index 1111111..2222222 100644
--- a/src/core/director.py
+++ b/src/core/director.py
@@ -1,4 +1,6 @@
 import os
+def _run_two_judge_review(x):
+    return x
 def existing():
     pass
diff --git a/src/tools/mcp_server.py b/src/tools/mcp_server.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/tools/mcp_server.py
@@ -0,0 +1,3 @@
+def serve():
+    return 1
diff --git a/README.md b/README.md
index 4444444..5555555 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,1 @@
-# Old title
+# New title
diff --git a/old_name.py b/new_name.py
similarity index 90%
rename from old_name.py
rename to new_name.py
--- a/old_name.py
+++ b/new_name.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
diff --git a/tests/test_x.py b/tests/test_x.py
deleted file mode 100644
index 6666666..0000000
--- a/tests/test_x.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_old():
-    pass
"""


@pytest.fixture
def files():
    return guide_gen.parse_diff(PATCH)


# --------------------------------------------------------------------------
# parse_diff
# --------------------------------------------------------------------------


def test_parse_diff_extracts_exact_paths_with_b_prefix_stripped(files):
    paths = [f.path for f in files]
    assert paths == [
        "src/core/director.py",
        "src/tools/mcp_server.py",
        "README.md",
        "new_name.py",
        "tests/test_x.py",
    ]
    assert not any(p.startswith(("a/", "b/")) for p in paths), (
        "the tool rejects a prefixed path; the generator must strip it"
    )


def test_parse_diff_counts_additions_and_removals(files):
    by_path = {f.path: f for f in files}
    assert (by_path["src/core/director.py"].added, by_path["src/core/director.py"].removed) == (2, 0)
    assert (by_path["README.md"].added, by_path["README.md"].removed) == (1, 1)
    assert (by_path["tests/test_x.py"].added, by_path["tests/test_x.py"].removed) == (0, 2)


def test_parse_diff_marks_new_deleted_and_rename(files):
    by_path = {f.path: f for f in files}
    assert by_path["src/tools/mcp_server.py"].is_new
    assert by_path["tests/test_x.py"].is_deleted
    assert by_path["new_name.py"].is_rename
    assert by_path["new_name.py"].old_path == "old_name.py"


def test_parse_diff_collects_symbols_without_duplicates(files):
    by_path = {f.path: f for f in files}
    assert "_run_two_judge_review" in by_path["src/core/director.py"].symbols_added
    assert "serve" in by_path["src/tools/mcp_server.py"].symbols_added
    assert "test_old" in by_path["tests/test_x.py"].symbols_removed
    for f in files:
        assert len(f.symbols_added) == len(set(f.symbols_added))


def test_parse_diff_tolerates_garbage_without_raising():
    assert guide_gen.parse_diff("") == []
    assert guide_gen.parse_diff("not a diff at all\n") == []


# --------------------------------------------------------------------------
# grouping and assembly
# --------------------------------------------------------------------------


def test_grouping_produces_at_most_six_sections_even_for_many_areas():
    many = [guide_gen.DiffFile(path=f"area{i}/f.py") for i in range(12)]
    sections = guide_gen.group_sections(many)
    assert 1 <= len(sections) <= guide_gen.MAX_SECTIONS


def test_grouping_is_deterministic(files):
    first = json.dumps(guide_gen.group_sections(files), sort_keys=True)
    second = json.dumps(guide_gen.group_sections(guide_gen.parse_diff(PATCH)), sort_keys=True)
    assert first == second


def test_grouping_never_places_a_file_twice(files):
    sections = guide_gen.group_sections(files)
    placed = [d["file"] for s in sections for d in s["diffs"]]
    assert len(placed) == len(set(placed))


def test_every_placed_file_is_named_exactly_once_and_summary_is_non_empty(files):
    for section in guide_gen.group_sections(files):
        assert section["title"].strip()
        assert section["overview"].strip()
        for entry in section["diffs"]:
            assert entry["file"].strip()
            assert entry["summary"].strip()


def test_build_guide_summaries_are_factual_not_critical(files):
    guide = guide_gen.build_guide(files, "T", "I")
    summaries = " ".join(d["summary"] for s in guide["sections"] for d in s["diffs"]).lower()
    assert "lines." in summaries
    for banned in ("bad", "wrong", "bug", "should be", "fix"):
        assert banned not in summaries, f"generator prose must not critique: found {banned!r}"


def test_build_guide_fails_closed_on_empty_input():
    with pytest.raises(ValueError, match="no files parsed"):
        guide_gen.build_guide([], "T", "I")


@pytest.mark.parametrize("bad", [("", "I"), ("T", ""), ("   ", "I")])
def test_build_guide_fails_closed_on_blank_title_or_intent(files, bad):
    with pytest.raises(ValueError, match="non-empty"):
        guide_gen.build_guide(files, *bad)


def test_build_guide_records_unplaced_files(files):
    """An unplaced entry must be a file the diff actually contains."""
    patched = files[0].path
    guide = guide_gen.build_guide(files, "T", "I", unplaced=[patched])
    assert guide["unplacedFiles"] == [patched]


# --------------------------------------------------------------------------
# the anti-rot layer: run the real tool
# --------------------------------------------------------------------------


def _plannotator():
    exe = shutil.which("plannotator")
    if not exe:
        pytest.skip("plannotator not installed; real-export contract unverified here")
    return exe


def _export(guide_path, patch_path, out_path):
    return subprocess.run(
        [_plannotator(), "guide", "export",
         "--guide", str(guide_path), "--patch", str(patch_path), "--out", str(out_path)],
        capture_output=True, text=True, timeout=180,
    )


def test_generated_guide_is_accepted_by_the_real_tool(tmp_path, files):
    """End-to-end: generator output must pass the binary's validation.

    This is the test that makes the generator worth having -- a CI job can gate
    on `plannotator guide export` only if this guide is actually accepted.
    """
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH, encoding="utf-8")
    guide = guide_gen.build_guide(files, "Guided review: generator probe", "Orient the reviewer.")
    gpath = tmp_path / "guide.json"
    gpath.write_text(json.dumps(guide), encoding="utf-8")
    out = tmp_path / "out.html"

    proc = _export(gpath, patch, out)
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, f"generator output was rejected:\n{combined}"
    assert out.is_file(), f"no HTML written:\n{combined}"
    html = out.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert 'name="plannotator-guided-review"' in html
    # every file the guide names must appear in the artifact
    for section in guide["sections"]:
        for entry in section["diffs"]:
            assert entry["file"] in html, f"{entry['file']} missing from the exported HTML"


def test_generated_guide_survives_an_unplaced_patched_file(tmp_path, files):
    """unplacedFiles is optional, but an entry must name a file the diff contains.

    Measured against the real binary: naming a patched file is fine, naming a
    foreign path is refused with "unplacedFiles not in the patch: X".
    """
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH, encoding="utf-8")
    patched = files[0].path
    guide = guide_gen.build_guide(files, "T", "I", unplaced=[patched])
    gpath = tmp_path / "guide.json"
    gpath.write_text(json.dumps(guide), encoding="utf-8")
    out = tmp_path / "out.html"

    proc = _export(gpath, patch, out)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert out.is_file()


def test_build_guide_refuses_an_unplaced_file_absent_from_the_diff(files):
    """The generator must fail closed rather than emit a guide the tool rejects."""
    with pytest.raises(ValueError, match="absent from the diff"):
        guide_gen.build_guide(files, "T", "I", unplaced=["vendor/left_out.py"])
