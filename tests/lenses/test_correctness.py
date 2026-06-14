"""
Tests for the Correctness adversarial lens.

Run: python -m pytest tests/lenses/test_correctness.py -v
"""

import pytest

from adversarial_reviewer import LensType


class TestCorrectnessLensPrompt:
    def test_returns_non_empty_string(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert isinstance(prompt, str)
        assert len(prompt.strip()) > 0

    def test_contains_logic_error_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert "logic error" in prompt.lower()

    def test_contains_correctness_focus_areas(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert "solution" in prompt.lower()
        assert "problem" in prompt.lower()

    def test_contains_type_and_data_flow_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert "variable" in prompt.lower() or "return value" in prompt.lower()

    def test_contains_hallucination_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert "hallucinated" in prompt.lower()

    def test_says_what_not_to_do(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert "DO NOT" in prompt
        assert "style" in prompt.lower()

    def test_mentions_incorrect_behavior(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.CORRECTNESS)
        assert "incorrect behavior" in prompt.lower()
