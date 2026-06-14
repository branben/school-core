"""
Tests for the Completeness adversarial lens.

Run: python -m pytest tests/lenses/test_completeness.py -v
"""

import pytest

from adversarial_reviewer import LensType


class TestCompletenessLensPrompt:
    def test_returns_non_empty_string(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert isinstance(prompt, str)
        assert len(prompt.strip()) > 0

    def test_contains_requirements_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "requirement" in prompt.lower()

    def test_contains_edge_case_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "edge case" in prompt.lower()

    def test_contains_error_paths_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "error path" in prompt.lower() or "null" in prompt.lower()

    def test_contains_missing_pieces_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "missing" in prompt.lower()

    def test_contains_unstated_assumptions_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "assumption" in prompt.lower()

    def test_says_what_not_to_do(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "DO NOT" in prompt
        assert "alternative approach" in prompt.lower()

    def test_mentions_incomplete(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.COMPLETENESS)
        assert "incomplete" in prompt.lower() or "missing" in prompt.lower()
