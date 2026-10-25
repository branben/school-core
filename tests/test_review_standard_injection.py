"""The review checklist must actually reach the reviewer's system prompt.

`docs/quality-gates-checklist.md` is cited as canonical by the `code-quality-gates`
skill and by `config/profiles/student-reviewer/SOUL.md` ("checklist-driven review").
Before this wiring, nothing read it: `grep -nE 'read_text|open\\(|checklist|STANDARD'
src/review/adversarial_reviewer.py` returned zero hits.

Two properties are pinned here:
  1. when the standard exists, its text is IN the system prompt;
  2. when it is absent, that fact is RECORDED, not silently skipped. A reviewer that
     quietly reviews without its standard is the fail-open pattern this repo keeps
     paying for -- "no standard" and "standard applied" must not look identical.

Run: python -m pytest tests/test_review_standard_injection.py -v
"""

import json
import logging

import pytest

from src.review.adversarial_reviewer import (
    DEFAULT_REVIEW_STANDARD_PATH,
    AdversarialReviewer,
    LensType,
)

PASS_JSON = json.dumps(
    {"findings": [], "verdict": "PASS", "score": 90, "confidence": 0.8}
)


def _capture_review(standard_path):
    """Run one review, returning (result, captured_prompts)."""
    captured = {}

    def call_model(prompt, system_prompt=None, **kw):
        captured["system"] = system_prompt
        captured["user"] = prompt
        return PASS_JSON

    reviewer = AdversarialReviewer(
        call_model_fn=call_model, review_standard_path=standard_path
    )
    result = reviewer.review(
        output="def f():\n    return 1\n",
        task={"title": "t", "body": "b", "domain": "code-implementation"},
        lens_types=[LensType.CORRECTNESS],
    )
    return result, captured, reviewer


class TestStandardReachesThePrompt:
    def test_standard_text_is_in_the_system_prompt(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("UNIQUE_MARKER_ABC123")
        _, captured, _ = _capture_review(std)
        assert "UNIQUE_MARKER_ABC123" in captured["system"]

    def test_standard_block_is_labelled(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("rule one")
        _, captured, _ = _capture_review(std)
        assert "[REVIEW STANDARD]" in captured["system"]

    def test_standard_does_not_pollute_codebase_context(self, tmp_path):
        """codebase_context is a FILE-REFERENCE signal for heuristic_scorer.

        Injecting the checklist there would corrupt the grounding score, so the
        standard must travel in the system prompt only.
        """
        std = tmp_path / "checklist.md"
        std.write_text("UNIQUE_MARKER_ABC123")
        _, captured, _ = _capture_review(std)
        assert "UNIQUE_MARKER_ABC123" not in captured["user"]


class TestMissingStandardIsVisible:
    def test_absent_standard_omits_the_block(self, tmp_path):
        missing = tmp_path / "absent.md"
        _, captured, _ = _capture_review(missing)
        assert "[REVIEW STANDARD]" not in captured["system"]

    def test_absent_standard_is_recorded_on_the_reviewer(self, tmp_path):
        missing = tmp_path / "absent.md"
        _, _, reviewer = _capture_review(missing)
        assert reviewer.standard_loaded is False

    def test_absent_standard_logs_a_warning(self, tmp_path, caplog):
        missing = tmp_path / "absent.md"
        with caplog.at_level(logging.WARNING):
            _capture_review(missing)
        assert any("standard" in r.message.lower() for r in caplog.records), (
            "a review that ran without its checklist must say so"
        )

    def test_present_standard_is_recorded_as_loaded(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("rule one")
        _, _, reviewer = _capture_review(std)
        assert reviewer.standard_loaded is True

    def test_empty_standard_file_counts_as_absent(self, tmp_path):
        std = tmp_path / "empty.md"
        std.write_text("   \n\n")
        _, captured, reviewer = _capture_review(std)
        assert reviewer.standard_loaded is False
        assert "[REVIEW STANDARD]" not in captured["system"]


class TestDefaultPath:
    def test_default_path_names_the_canonical_artifact(self):
        assert DEFAULT_REVIEW_STANDARD_PATH.name == "quality-gates-checklist.md"
    def test_default_path_resolves_inside_the_repo(self):
        assert DEFAULT_REVIEW_STANDARD_PATH.parent.name == "docs"

    def test_the_canonical_checklist_exists(self):
        """The two references (skill + reviewer SOUL) must point at a real file."""
        assert DEFAULT_REVIEW_STANDARD_PATH.exists(), (
            f"{DEFAULT_REVIEW_STANDARD_PATH} is cited as canonical but is absent"
        )


class TestRetryPathKeepsTheStandard:
    """The parse-failure retry fires exactly when things are already going wrong.

    Regression (found by an adversarial review, then confirmed by reading
    adversarial_reviewer.py): the retry rebuilt its system prompt inline and
    dropped BOTH the [LENS] block and the [REVIEW STANDARD] block. The retry
    result is the one that gets RETURNED, so the answer a caller receives could
    come from a prompt carrying no checklist at all -- the checklist's own
    thesis (silence is not a pass) violated by its carrier.
    """

    def test_retry_system_prompt_carries_the_standard(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("UNIQUE_MARKER_ABC123")
        seen = []

        def call_model(prompt, system_prompt=None, **kw):
            seen.append(system_prompt)
            if len(seen) == 1:
                return "this is not json at all, and it is longer than twenty chars"
            return PASS_JSON

        reviewer = AdversarialReviewer(
            call_model_fn=call_model, review_standard_path=std
        )
        reviewer.review(
            output="def f():\n    return 1\n",
            task={"title": "t", "body": "b", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert len(seen) >= 2, "the parse-failure retry did not fire"
        assert "UNIQUE_MARKER_ABC123" in seen[1], (
            "the retry system prompt dropped the review standard"
        )
