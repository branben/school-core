"""Tests for the Semantic Anchor Registry.

Run: python -m pytest tests/test_anchor_loader.py -v
"""

import json
from pathlib import Path

import pytest

from anchor_loader import Anchor, AnchorRegistry
from scoring import GATES, ScoreStore


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def registry():
    """Load the real registry from config/anchors.yaml."""
    return AnchorRegistry()


@pytest.fixture
def store(tmp_path):
    """Create a ScoreStore with temp scores."""
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(json.dumps({
        "foundry-coder-7b": {"_default": 25.0, "python-testing": 65.4},
    }))
    return ScoreStore(file_path=str(scores_file))


# ── AnchorRegistry Loading ───────────────────────────────────────────────────


class TestAnchorRegistryLoad:
    def test_loads_from_yaml(self, registry):
        """Registry loads all anchors from config/anchors.yaml."""
        assert len(registry) > 0

    def test_contains_known_anchors(self, registry):
        """Registry contains anchors migrated from prompt_composer.py."""
        assert "Fagan Inspection" in registry
        assert "YAGNI" in registry
        assert "Five Whys" in registry
        assert "Chain of Thought" in registry
        assert "First Principles Thinking" in registry
        assert "TDD Chicago School" in registry
        assert "SOLID Principles" in registry
        assert "Clean Architecture" in registry
        assert "Distillation" in registry

    def test_get_anchor_returns_anchor(self, registry):
        """get_anchor returns the full Anchor object."""
        anchor = registry.get_anchor("Fagan Inspection")
        assert anchor is not None
        assert anchor.name == "Fagan Inspection"
        assert anchor.tier == "methodology"
        assert anchor.domain == "code-review"
        assert anchor.difficulty_gate == "hard"
        assert len(anchor.expected_behaviors) > 0
        assert len(anchor.examples) > 0

    def test_get_anchor_missing(self, registry):
        """get_anchor returns None for unknown anchor."""
        assert registry.get_anchor("Nonexistent Anchor") is None

    def test_bracket_notation(self, registry):
        """bracket_notation() returns [Name] format."""
        anchor = registry.get_anchor("YAGNI")
        assert anchor.bracket_notation() == "[YAGNI]"


# ── Anchor Querying ──────────────────────────────────────────────────────────


class TestAnchorQuerying:
    def test_get_anchors_by_domain(self, registry):
        """Filter by domain returns only matching anchors."""
        code_review = registry.get_anchors(domain="code-review")
        assert len(code_review) > 0
        assert all(a.domain == "code-review" for a in code_review)

    def test_get_anchors_by_tier(self, registry):
        """Filter by tier returns only matching anchors."""
        methodologies = registry.get_anchors(tier="methodology")
        assert len(methodologies) > 0
        assert all(a.tier == "methodology" for a in methodologies)

    def test_get_anchors_by_domain_and_tier(self, registry):
        """Filter by both domain and tier."""
        results = registry.get_anchors(domain="code-review", tier="principle")
        assert all(a.domain == "code-review" and a.tier == "principle" for a in results)

    def test_get_anchors_by_difficulty_gate(self, registry):
        """Filter by difficulty gate returns anchors at or below that level."""
        easy = registry.get_anchors(difficulty_gate="easy")
        hard = registry.get_anchors(difficulty_gate="hard")
        # Hard includes easy + medium + hard anchors
        assert len(hard) >= len(easy)

    def test_get_anchors_missing_domain(self, registry):
        """Unknown domain returns empty list, no error."""
        assert registry.get_anchors(domain="nonexistent") == []

    def test_get_anchor_names(self, registry):
        """get_anchor_names returns name strings."""
        names = registry.get_anchor_names(domain="general")
        assert "Chain of Thought" in names
        names = registry.get_anchor_names(domain="debugging")
        assert "Five Whys" in names

    def test_get_domains(self, registry):
        """get_domains returns all unique domains."""
        domains = registry.get_domains()
        assert "code-review" in domains
        assert "python-testing" in domains
        assert "debugging" in domains

    def test_get_tiers(self, registry):
        """get_tiers returns all unique tiers."""
        tiers = registry.get_tiers()
        assert "methodology" in tiers
        assert "principle" in tiers
        assert "technique" in tiers


# ── Backward Compatibility (Golden Test) ─────────────────────────────────────


class TestBackwardCompatibility:
    """Verify that compose_prompt produces output compatible with the
    pre-registry hardcoded dicts. The bracket notation and prompt structure
    must be identical; the registry adds activation pattern enrichment."""

    def test_compose_prompt_has_bracket_notation(self, registry, store):
        """compose_prompt output includes bracket notation for all anchors."""
        from prompt_composer import compose_prompt

        result = compose_prompt(
            domain="code-review",
            difficulty="hard",
            agent="foundry-coder-7b",
            store=store,
            domain_prompts={},
            default_prompt="",
            is_local=False,
            is_blocker=False,
        )
        # Must contain bracket notation for known anchors
        assert "[Fagan Inspection]" in result
        assert "[SOLID Principles]" in result
        assert "[Code Smells]" in result

    def test_compose_prompt_enriches_with_activation_patterns(self, registry, store):
        """compose_prompt output includes activation patterns from registry."""
        from prompt_composer import compose_prompt

        result = compose_prompt(
            domain="code-review",
            difficulty="hard",
            agent="foundry-coder-7b",
            store=store,
            domain_prompts={},
            default_prompt="",
            is_local=False,
            is_blocker=False,
        )
        # Activation patterns should appear as [Name]: pattern
        assert "[Fagan Inspection]:" in result
        assert "structured" in result.lower() or "systematic" in result.lower()

    def test_compose_prompt_role_anchors_present(self, registry, store):
        """compose_prompt includes role-based anchors."""
        from prompt_composer import compose_prompt

        result = compose_prompt(
            domain="debugging",
            difficulty="medium",
            agent="foundry-coder-7b",
            store=store,
            domain_prompts={},
            default_prompt="",
            is_local=True,
            is_blocker=False,
        )
        assert "[YAGNI]" in result
        assert "[ROLE]" in result

    def test_compose_prompt_difficulty_anchors(self, registry, store):
        """compose_prompt includes difficulty-based anchors."""
        from prompt_composer import compose_prompt

        result = compose_prompt(
            domain="debugging",
            difficulty="blocker",
            agent="foundry-coder-7b",
            store=store,
            domain_prompts={},
            default_prompt="",
            is_local=False,
            is_blocker=True,
        )
        assert "[Five Whys]" in result
        assert "[DIFFICULTY]" in result

    def test_compose_prompt_domain_extra_context(self, registry, store):
        """compose_prompt preserves domain extra_context."""
        from prompt_composer import compose_prompt

        result = compose_prompt(
            domain="python-testing",
            difficulty="easy",
            agent="foundry-coder-7b",
            store=store,
            domain_prompts={},
            default_prompt="",
            is_local=True,
            is_blocker=False,
        )
        assert "[DOMAIN_CONTEXT]" in result
        assert "state-based testing" in result.lower()


# ── Anchor Data Integrity ────────────────────────────────────────────────────


class TestAnchorDataIntegrity:
    def test_all_anchors_have_required_fields(self, registry):
        """Every anchor has name, tier, domain, and activation_pattern."""
        for anchor in registry.get_all_anchors():
            assert anchor.name, f"Anchor missing name"
            assert anchor.tier, f"Anchor '{anchor.name}' missing tier"
            assert anchor.domain, f"Anchor '{anchor.name}' missing domain"
            assert anchor.activation_pattern, f"Anchor '{anchor.name}' missing activation_pattern"

    def test_all_anchors_have_bracket_notation(self, registry):
        """Every anchor produces valid bracket notation."""
        for anchor in registry.get_all_anchors():
            bn = anchor.bracket_notation()
            assert bn.startswith("[") and bn.endswith("]"), f"Invalid bracket notation: {bn}"

    def test_tier_values_valid(self, registry):
        """All tiers are one of: methodology, principle, technique."""
        valid_tiers = {"methodology", "principle", "technique"}
        for anchor in registry.get_all_anchors():
            assert anchor.tier in valid_tiers, f"Invalid tier '{anchor.tier}' for '{anchor.name}'"

    def test_difficulty_gate_values_valid(self, registry):
        """All difficulty gates are valid."""
        valid_gates = {"easy", "medium", "hard", "blocker"}
        for anchor in registry.get_all_anchors():
            assert anchor.difficulty_gate in valid_gates, f"Invalid gate '{anchor.difficulty_gate}' for '{anchor.name}'"
