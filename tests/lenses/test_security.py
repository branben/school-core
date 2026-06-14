"""
Tests for the Security adversarial lens.

Run: python -m pytest tests/lenses/test_security.py -v
"""

import pytest

from adversarial_reviewer import LensType


class TestSecurityLensPrompt:
    def test_returns_non_empty_string(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert isinstance(prompt, str)
        assert len(prompt.strip()) > 0

    def test_contains_owasp_reference(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "owasp" in prompt.lower()

    def test_contains_injection_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "injection" in prompt.lower()

    def test_contains_auth_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "auth" in prompt.lower()

    def test_contains_data_exposure_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "data exposure" in prompt.lower() or "sensitive data" in prompt.lower()

    def test_contains_input_validation_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "input validation" in prompt.lower()

    def test_contains_cryptography_focus(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "cryptograph" in prompt.lower() or "hardcoded secret" in prompt.lower()

    def test_says_what_not_to_do(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "DO NOT" in prompt
        assert "style" in prompt.lower() or "naming" in prompt.lower()

    def test_mentions_security_relevant(self, lenses_mod):
        prompt = lenses_mod.get_lens_prompt(LensType.SECURITY)
        assert "security-relevant" in prompt.lower() or "security" in prompt.lower()
