"""Tests for the plannotator-guide guided-review guide.json shape.

The schema itself lives in ``docs/templates/guided-review-guide.schema.json``.

Dependency posture: this test must stay green in a hermetic environment with
only stdlib available.  A full draft-07 validator (``jsonschema``) is used
when importable; otherwise a minimal structural checker implemented with the
stdlib ``json`` module performs the same required-key/type assertions plus the
same "a file must appear in exactly one chapter" rule.  The duplicate-file
rejection is asserted in BOTH paths so the contract cannot silently rot.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs/templates/guided-review-guide.schema.json"
EXAMPLE_PATH = REPO_ROOT / "docs/templates/review_guide_example.json"

REQUIRED_TOP_LEVEL = {"schema", "title", "summary", "chapters"}
REQUIRED_CHAPTER = {"id", "title", "summary", "files"}
REQUIRED_FILE_ENTRY = {"path", "why"}


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _structural_checker(guide):
    """Minimal validator mirroring the schema's hard rules, stdlib-only.

    Returns a list of violation strings (empty == well-formed).  It checks the
    same things an external JSON Schema draft-07 validator would enforce, so
    the test behaves identically whether or not ``jsonschema`` is installed.
    """
    errors = []
    if not isinstance(guide, dict):
        return ["guide root must be an object"]
    missing = REQUIRED_TOP_LEVEL - set(guide)
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")
    if not isinstance(guide.get("schema"), str) or not guide["schema"]:
        errors.append("schema ref must be a non-empty string")
    if not isinstance(guide.get("title"), str) or not guide["title"]:
        errors.append("title must be a non-empty string")
    if not isinstance(guide.get("summary"), str) or not guide["summary"]:
        errors.append("summary must be a non-empty string")
    chapters = guide.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        errors.append("chapters must be a non-empty array")
        return errors
    for i, chapter in enumerate(chapters):
        label = f"chapters[{i}]"
        if not isinstance(chapter, dict):
            errors.append(f"{label} must be an object")
            continue
        missing_c = REQUIRED_CHAPTER - set(chapter)
        if missing_c:
            errors.append(f"{label} missing fields: {sorted(missing_c)}")
        if not isinstance(chapter.get("id"), str) or not chapter["id"]:
            errors.append(f"{label}.id must be a non-empty string")
        if not isinstance(chapter.get("title"), str) or not chapter["title"]:
            errors.append(f"{label}.title must be a non-empty string")
        if not isinstance(chapter.get("summary"), str) or not chapter["summary"]:
            errors.append(f"{label}.summary must be a non-empty string")
        files = chapter.get("files")
        if not isinstance(files, list) or not files:
            errors.append(f"{label}.files must be a non-empty array")
            continue
        seen_paths = set()
        for j, entry in enumerate(files):
            flabel = f"{label}.files[{j}]"
            if not isinstance(entry, dict):
                errors.append(f"{flabel} must be an object")
                continue
            missing_f = REQUIRED_FILE_ENTRY - set(entry)
            if missing_f:
                errors.append(f"{flabel} missing fields: {sorted(missing_f)}")
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                errors.append(f"{flabel}.path must be a non-empty string")
            if path in seen_paths:
                errors.append(
                    f"{flabel}: duplicate file '{path}' within chapter "
                    f"'{chapter.get('id', '?')}' (chapters enforce uniqueItems)"
                )
            seen_paths.add(path)
            if not isinstance(entry.get("why"), str) or not entry["why"]:
                errors.append(f"{flabel}.why must be a non-empty string")
    all_files = [(ch.get("id", "?"), f.get("path"), f.get("why"))
                 for ch in chapters if isinstance(ch, dict)
                 for f in (ch.get("files") if isinstance(ch.get("files"), list) else [])
                 if isinstance(f, dict)]
    counts = {}
    for _, path, _ in all_files:
        counts[path] = counts.get(path, 0) + 1
    for path, n in counts.items():
        if n > 1:
            errors.append(
                f"file '{path}' appears in {n} chapters; exactly-one-chapter "
                f"coverage rule violated (cross-section uniqueness)"
            )
    return errors


def test_schema_and_example_are_valid_json():
    schema = _load_json(SCHEMA_PATH)
    example = _load_json(EXAMPLE_PATH)
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")
    assert schema["$comment"], "schema should carry authoring-calibration $comment"
    assert example["schema"] == "plannotator-guide/1"


def test_schema_carries_guide_not_review_calibration():
    """The $comment must state the calibration, not just enforce structure."""
    schema = _load_json(SCHEMA_PATH)
    comment = schema["$comment"].lower()
    assert "guide" in comment, f"$comment should call out the calibration, got: {comment}"
    assert "not review" in comment or "not critique" in comment, (
        "calibration must say it is a guide (orientation), not a review (critique)"
    )
    assert "orient" in comment


def test_schema_documents_cross_chapter_uniqueness_limitation():
    """The $comment must document the cross-section uniqueness limitation."""
    schema = _load_json(SCHEMA_PATH)
    comment = schema["$comment"].lower()
    for needle in ("exactly one chapter", "cross-chapter", "uniqueitems"):
        assert needle in comment, f"$comment should document the limitation around {needle!r}"


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
    # the .html domain is generated-markdown (Tier-3), never hand-authored truth
    for rel in TIER3_TEMPLATES:
        assert "CANONICAL SOURCE CONVENTION" in (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_example_passes_structural_check_without_jsonschema():
    example = _load_json(EXAMPLE_PATH)
    errors = _structural_checker(example)
    assert errors == [], f"example failed structural check: {errors}"


def test_duplicate_file_across_chapters_rejected_by_structural_checker():
    example = _load_json(EXAMPLE_PATH)
    dup = dict(example)
    dup["chapters"] = [
        list(example["chapters"])[0],
        {
            "id": "dup-chapter",
            "title": "Duplicate chapter",
            "summary": "Re-lists a file from chapter one to prove rejection.",
            "files": [
                {
                    "path": example["chapters"][0]["files"][0]["path"],
                    "why": "deliberately duplicated for the rejection test",
                }
            ],
        },
    ]
    errors = _structural_checker(dup)
    assert any("exactly-one-chapter" in e or "appears in" in e for e in errors), errors


def test_duplicate_file_within_one_chapter_rejected_by_structural_checker():
    example = _load_json(EXAMPLE_PATH)
    dup = dict(example)
    ch = dict(example["chapters"][0])
    ch["files"] = list(example["chapters"][0]["files"]) + [
        dict(example["chapters"][0]["files"][0])
    ]
    dup["chapters"] = [ch] + list(example["chapters"])[1:]
    errors = _structural_checker(dup)
    assert any("duplicate file" in e for e in errors), errors


def _jsonschema_or_skip():
    """Return the jsonschema module when importable, else skip this test.

    ``exc_type=ImportError`` (not just ``ModuleNotFoundError``) so a broken
    or partial install also falls back to the structural checker instead of
    failing the suite.
    """
    return pytest.importorskip(
        "jsonschema",
        reason="jsonschema not installed; the structural checker above covers the contract",
        exc_type=ImportError,
    )


def test_example_validates_under_jsonschema():
    jsonschema = _jsonschema_or_skip()
    validator = jsonschema.Draft7Validator(_load_json(SCHEMA_PATH))
    example = _load_json(EXAMPLE_PATH)
    errs = sorted(validator.iter_errors(example), key=lambda e: list(e.path))
    assert not errs, [e.message for e in errs]


def test_schema_is_well_formed_draft07():
    jsonschema = _jsonschema_or_skip()
    schema = _load_json(SCHEMA_PATH)
    meta = jsonschema.Draft7Validator.META_SCHEMA
    errs = sorted(jsonschema.Draft7Validator(meta).iter_errors(schema),
                  key=lambda e: list(e.path))
    assert not errs, [e.message for e in errs]


def test_cross_chapter_duplicate_is_documented_schema_limitation():
    """Pure draft-07 canNOT enforce cross-chapter uniqueness.

    uniqueItems only scopes to a single array, so a file copied into a second
    chapter still validates under the schema. This is the limitation the
    schema documents in its $comment; the hard "exactly one chapter" rule is
    enforced by the tooling layer AND by the always-running structural
    checker (``test_duplicate_file_across_chapters_rejected_by_structural_checker``).
    This test pins the limitation so it stays visible rather than silently
    becoming a regression.
    """
    jsonschema = _jsonschema_or_skip()
    validator = jsonschema.Draft7Validator(_load_json(SCHEMA_PATH))
    example = _load_json(EXAMPLE_PATH)
    dup = dict(example)
    dup["chapters"] = [
        list(example["chapters"])[0],
        {
            "id": "dup-chapter",
            "title": "Duplicate chapter",
            "summary": "Re-lists a file from chapter one.",
            "files": [dict(example["chapters"][0]["files"][0])],
        },
    ]
    errs = list(validator.iter_errors(dup))
    assert not errs, (
        "if this rejects, the cross-chapter uniqueness limitation was lifted "
        f"in the schema; update $comment and this test. got: "
        f"{[e.message for e in errs]}"
    )


def test_duplicate_file_within_chapter_rejected_under_jsonschema():
    jsonschema = _jsonschema_or_skip()
    validator = jsonschema.Draft7Validator(_load_json(SCHEMA_PATH))
    example = _load_json(EXAMPLE_PATH)
    dup = dict(example)
    ch = dict(example["chapters"][0])
    ch["files"] = list(example["chapters"][0]["files"]) + [
        dict(example["chapters"][0]["files"][0])
    ]
    dup["chapters"] = [ch] + list(example["chapters"])[1:]
    errs = list(validator.iter_errors(dup))
    assert errs, "duplicate file within a chapter must reject under the schema"
    assert any("uniqueItems" in e.message or "non-unique" in e.message
               for e in errs), [e.message for e in errs]