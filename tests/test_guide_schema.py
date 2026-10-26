"""Tests for the plannotator Guided Review guide.json shape.

The schema lives in ``docs/templates/guided-review-guide.schema.json``.

WHY THIS FILE RUNS THE REAL TOOL
--------------------------------
The previous version of this file validated a hand-written schema with 11
passing tests while ``plannotator guide export`` rejected the repo's own
example outright::

    Guide is not valid:
    - `intent` must be a non-empty string.
    - `sections` must be a non-empty array.

The schema said ``chapters``; the tool wanted ``sections``. Nothing executed
the artifact, so the contract rotted silently and the tests stayed green. A
schema that only agrees with itself proves nothing. The authoritative shape
here is read from ``plannotator guide export --help`` and asserted by running
it -- see ``test_real_plannotator_export_produces_html``.

Dependency posture: the structural tests stay green with stdlib only. The two
real-export tests skip when ``plannotator`` is not installed, because a missing
optional binary must not fail a hermetic suite -- but CI runs the full suite on
a runner that has it, so the contract cannot drift unnoticed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs/templates/guided-review-guide.schema.json"
EXAMPLE_PATH = REPO_ROOT / "docs/templates/review_guide_example.json"

# Read from `plannotator guide export --help`. Not from memory.
REQUIRED_TOP_LEVEL = {"title", "intent", "sections"}
REQUIRED_SECTION = {"title", "overview", "diffs"}
REQUIRED_DIFF_ENTRY = {"file", "summary"}


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _guide_files(guide):
    return [
        d["file"]
        for section in guide.get("sections") or []
        for d in section.get("diffs") or []
        if isinstance(d, dict) and isinstance(d.get("file"), str)
    ]


def _structural_checker(guide):
    """Minimal validator mirroring the tool's hard rules, stdlib-only.

    Returns a list of violation strings (empty == well-formed).
    """
    errors = []
    if not isinstance(guide, dict):
        return ["guide root must be an object"]
    missing = REQUIRED_TOP_LEVEL - set(guide)
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")
    if not isinstance(guide.get("title"), str) or not guide["title"]:
        errors.append("title must be a non-empty string")
    if not isinstance(guide.get("intent"), str) or not guide["intent"]:
        errors.append("intent must be a non-empty string")
    sections = guide.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("sections must be a non-empty array")
        return errors
    for i, section in enumerate(sections):
        label = f"sections[{i}]"
        if not isinstance(section, dict):
            errors.append(f"{label} must be an object")
            continue
        missing_s = REQUIRED_SECTION - set(section)
        if missing_s:
            errors.append(f"{label} missing fields: {sorted(missing_s)}")
        if not isinstance(section.get("title"), str) or not section["title"]:
            errors.append(f"{label}.title must be a non-empty string")
        if not isinstance(section.get("overview"), str) or not section["overview"]:
            errors.append(f"{label}.overview must be a non-empty string")
        diffs = section.get("diffs")
        if not isinstance(diffs, list):
            errors.append(f"{label}.diffs must be an array (it may be empty)")
            continue
        seen = set()
        for j, entry in enumerate(diffs):
            flabel = f"{label}.diffs[{j}]"
            if not isinstance(entry, dict):
                errors.append(f"{flabel} must be an object")
                continue
            missing_d = REQUIRED_DIFF_ENTRY - set(entry)
            if missing_d:
                errors.append(f"{flabel} missing fields: {sorted(missing_d)}")
            path = entry.get("file")
            if not isinstance(path, str) or not path:
                errors.append(f"{flabel}.file must be a non-empty string")
            if path in seen:
                errors.append(
                    f"{flabel}: duplicate file '{path}' within section "
                    f"'{section.get('title', '?')}' (diffs enforce uniqueItems)"
                )
            seen.add(path)
            if not isinstance(entry.get("summary"), str) or not entry["summary"]:
                errors.append(f"{flabel}.summary must be a non-empty string")
    if len(sections) > 6:
        errors.append(
            f"sections has {len(sections)} entries; the tool accepts at most 6"
        )
    counts = {}
    for path in _guide_files(guide):
        counts[path] = counts.get(path, 0) + 1
    for path, n in counts.items():
        if n > 1:
            errors.append(
                f"file '{path}' appears in {n} sections; the tool allows a "
                f"file in at most one section (duplicate placement rejected)")
    return errors


def test_schema_and_example_are_valid_json():
    schema = _load_json(SCHEMA_PATH)
    example = _load_json(EXAMPLE_PATH)
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")
    assert schema["$comment"], "schema should carry authoring-calibration $comment"
    assert isinstance(example, dict) and example


def test_schema_required_matches_the_real_tool_shape():
    """Guard against reverting to the stale `chapters` contract.

    The old schema required ``schema/title/summary/chapters``; the tool wants
    ``title/intent/sections``. Asserting both directions makes a silent revert
    impossible: the new keys must be required AND the old keys must be gone.
    """
    schema = _load_json(SCHEMA_PATH)
    assert set(schema["required"]) == REQUIRED_TOP_LEVEL, (
        "schema required keys must match `plannotator guide export --help`: "
        f"{sorted(REQUIRED_TOP_LEVEL)}, got {sorted(schema['required'])}"
    )
    props = schema["properties"]
    for stale in ("chapters", "summary", "schema"):
        assert stale not in props, (
            f"stale key {stale!r} is back in the schema; the tool does not "
            "accept it. Re-read `plannotator guide export --help`."
        )
    section = props["sections"]["items"]
    assert set(section["required"]) == REQUIRED_SECTION
    assert set(section["properties"]["diffs"]["items"]["required"]) == REQUIRED_DIFF_ENTRY


def test_schema_carries_guide_not_review_calibration():
    """The $comment must state the calibration, not just enforce structure."""
    comment = _load_json(SCHEMA_PATH)["$comment"].lower()
    assert "guide" in comment, "comment should call out the calibration"
    assert "not review" in comment or "not critique" in comment, (
        "calibration must say it is a guide (orientation), not a review (critique)"
    )
    assert "orient" in comment


def test_schema_documents_the_measured_cross_section_rules():
    """The $comment must record what the binary ACTUALLY enforces.

    Measured 2026-10-26 by running the installed binary both ways. The previous
    version of this test asserted "exactly one section" and "uniqueItems", which
    the tool does not require -- a file may be in zero sections, and unplacedFiles
    is optional. Asserting the measured rules is the point: the comment is the
    only place the contract is written down, and it had drifted.
    """
    comment = _load_json(SCHEMA_PATH)["$comment"].lower()
    for needle in ("1..6", "at most one section", "unplacedfiles", "may be empty"):
        assert needle in comment, (
            f"$comment should document the measured rule around {needle!r}"
        )


def test_example_passes_structural_check():
    errors = _structural_checker(_load_json(EXAMPLE_PATH))
    assert errors == [], f"example failed structural check: {errors}"


def test_duplicate_file_across_sections_rejected():
    example = _load_json(EXAMPLE_PATH)
    dup = dict(example)
    dup["sections"] = list(example["sections"]) + [
        {
            "title": "Duplicate section",
            "overview": "Re-lists a file from section one to prove rejection.",
            "diffs": [{"file": _guide_files(example)[0], "summary": "dup"}],
        }
    ]
    errors = _structural_checker(dup)
    assert any("exactly-one-section" in e or "appears in" in e for e in errors), errors


def test_duplicate_file_within_one_section_rejected():
    example = _load_json(EXAMPLE_PATH)
    dup = dict(example)
    first = dict(example["sections"][0])
    first["diffs"] = list(first["diffs"]) + [dict(first["diffs"][0])]
    dup["sections"] = [first] + list(example["sections"])[1:]
    errors = _structural_checker(dup)
    assert any("duplicate file" in e for e in errors), errors


def _jsonschema_or_skip():
    return pytest.importorskip(
        "jsonschema",
        reason="jsonschema not installed; the structural checker covers the contract",
        exc_type=ImportError,
    )


def test_example_validates_under_jsonschema():
    jsonschema = _jsonschema_or_skip()
    validator = jsonschema.Draft7Validator(_load_json(SCHEMA_PATH))
    errs = sorted(validator.iter_errors(_load_json(EXAMPLE_PATH)), key=lambda e: list(e.path))
    assert not errs, [e.message for e in errs]


def test_schema_is_well_formed_draft07():
    jsonschema = _jsonschema_or_skip()
    schema = _load_json(SCHEMA_PATH)
    errs = sorted(
        jsonschema.Draft7Validator(jsonschema.Draft7Validator.META_SCHEMA).iter_errors(schema),
        key=lambda e: list(e.path),
    )
    assert not errs, [e.message for e in errs]


# --------------------------------------------------------------------------
# The anti-rot layer: run the real tool.
# --------------------------------------------------------------------------


def _plannotator():
    exe = shutil.which("plannotator")
    if not exe:
        pytest.skip("plannotator not installed; real-export contract unverified here")
    return exe


def _patch_for(files):
    """A minimal valid unified diff containing exactly ``files``."""
    chunks = []
    for path in files:
        chunks.append(
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1,2 @@\n"
            "+placeholder\n"
            "+placeholder two\n"
        )
    return "".join(chunks)


def _export(guide_path, patch_path, out_path):
    return subprocess.run(
        [
            _plannotator(), "guide", "export",
            "--guide", str(guide_path),
            "--patch", str(patch_path),
            "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_real_plannotator_export_produces_html(tmp_path):
    """The repo's own example must be accepted by the real tool.

    This is the test the previous 11 could not replace: they agreed with the
    schema, and the schema was wrong.
    """
    example = _load_json(EXAMPLE_PATH)
    patch = tmp_path / "guide.patch"
    patch.write_text(_patch_for(_guide_files(example)), encoding="utf-8")
    out = tmp_path / "guide.html"

    proc = _export(EXAMPLE_PATH, patch, out)
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, (
        "export of a guide that names exactly the patched files should succeed. "
        f"output:\n{combined}"
    )
    assert out.is_file(), f"plannotator wrote no HTML. output:\n{combined}"
    html = out.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html>"), html[:120]
    assert 'name="plannotator-guided-review"' in html, (
        "HTML is not a guided-review document"
    )
    assert len(html) > 1000, f"HTML suspiciously small: {len(html)} bytes"


def test_real_plannotator_rejects_a_file_absent_from_the_patch(tmp_path):
    """A guide naming a changed file that is not in the diff must be refused.

    Measured behaviour: the tool prints ``Guide is not valid``, names the
    offending file and the files that WERE in the patch, writes no HTML, and
    exits **1**. Asserted here as the artifact's absence AND the exit code.

    NOTE ON MEASURING THIS: an earlier version of this test's comment claimed
    the tool exits 0 on rejection. That came from piping the command through
    ``| tail`` and reading ``$?`` -- the exit status of ``tail``, not of
    plannotator. Measured properly with ``subprocess.run`` the code is 1, which
    matches the skill's documentation. Do not infer an exit code from a shell
    pipeline.
    """
    example = _load_json(EXAMPLE_PATH)
    files = _guide_files(example)
    assert len(files) >= 2, "example must name at least two files for this test"
    omitted = files[-1]
    patch = tmp_path / "guide.patch"
    patch.write_text(_patch_for(files[:-1]), encoding="utf-8")
    out = tmp_path / "guide.html"

    proc = _export(EXAMPLE_PATH, patch, out)
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode != 0, (
        f"a guide naming {omitted!r} (absent from the patch) must be refused. "
        f"output:\n{combined}"
    )
    assert not out.is_file(), (
        f"guide naming {omitted!r} (absent from the patch) must not export. "
        f"output:\n{combined}"
    )
    lowered = combined.lower()
    assert "not valid" in lowered or omitted.lower() in lowered, (
        f"expected a validation complaint naming {omitted!r}, got:\n{combined}"
    )


TIER3_TEMPLATES = (
    "docs/templates/prd-pitch.html",
    "docs/templates/spec-implementation-plan.html",
    "docs/templates/pr-writeup.html",
)


def test_tier3_html_templates_carry_canonical_md_source_header():
    """Each Tier-3 template must name its canonical .md source (see U1 #3/#4/#5)."""
    for rel in TIER3_TEMPLATES:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert '<meta name="source"' in text, f"{rel} missing <meta name=source>"
        assert re.search(r"content=\"docs/plans/[^\"]+\.md\"", text), (
            f"{rel} source header must point at docs/plans/...md"
        )
    for rel in TIER3_TEMPLATES:
        assert "CANONICAL SOURCE CONVENTION" in (REPO_ROOT / rel).read_text(encoding="utf-8")
