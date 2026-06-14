"""Semantic Anchor Registry loader.

Loads anchors from config/anchors.yaml and provides query functions for
prompt_composer.py and other components.

Usage:
    from anchor_loader import AnchorRegistry

    registry = AnchorRegistry()
    anchors = registry.get_anchors(domain="code-review", tier="methodology", difficulty_gate="hard")
    anchor = registry.get_anchor("Fagan Inspection")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class Anchor:
    """A semantic anchor — a term that activates known patterns in LLMs."""

    name: str
    tier: str  # methodology | principle | technique
    domain: str
    activation_pattern: str
    expected_behaviors: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    difficulty_gate: str = "easy"

    def bracket_notation(self) -> str:
        """Return the bracket notation used in prompts: [Fagan Inspection]."""
        return f"[{self.name}]"


class AnchorRegistry:
    """Loads and queries semantic anchors from config/anchors.yaml."""

    def __init__(self, config_path: str | Path | None = None):
        if config_path is None:
            config_path = Path(__file__).parent / "config" / "anchors.yaml"
        self._config_path = Path(config_path)
        self._anchors: list[Anchor] = []
        self._load()

    def _load(self) -> None:
        """Load anchors from the YAML registry file."""
        if not self._config_path.exists():
            return

        with open(self._config_path) as f:
            data = yaml.safe_load(f) or {}

        for entry in data.get("anchor_registry", {}).get("anchors", []):
            self._anchors.append(
                Anchor(
                    name=entry["name"],
                    tier=entry["tier"],
                    domain=entry["domain"],
                    activation_pattern=entry.get("activation_pattern", ""),
                    expected_behaviors=entry.get("expected_behaviors", []),
                    examples=entry.get("examples", []),
                    difficulty_gate=entry.get("difficulty_gate", "easy"),
                )
            )

    def get_anchors(
        self,
        domain: str | None = None,
        tier: str | None = None,
        difficulty_gate: str | None = None,
    ) -> list[Anchor]:
        """Query anchors by domain, tier, and/or difficulty gate.

        All parameters are optional filters. Pass None to skip a filter.
        Results are filtered by all provided criteria (AND logic).
        """
        results = self._anchors

        if domain is not None:
            results = [a for a in results if a.domain == domain]

        if tier is not None:
            results = [a for a in results if a.tier == tier]

        if difficulty_gate is not None:
            gate_order = {"easy": 0, "medium": 1, "hard": 2, "blocker": 3}
            gate_level = gate_order.get(difficulty_gate, 0)
            results = [
                a for a in results
                if gate_order.get(a.difficulty_gate, 0) <= gate_level
            ]

        return results

    def get_anchor(self, name: str) -> Optional[Anchor]:
        """Get a single anchor by exact name. Returns None if not found."""
        for anchor in self._anchors:
            if anchor.name == name:
                return anchor
        return None

    def get_all_anchors(self) -> list[Anchor]:
        """Return all loaded anchors."""
        return list(self._anchors)

    def get_anchor_names(
        self,
        domain: str | None = None,
        tier: str | None = None,
        difficulty_gate: str | None = None,
    ) -> list[str]:
        """Return anchor names matching the given filters."""
        return [a.name for a in self.get_anchors(domain, tier, difficulty_gate)]

    def get_domains(self) -> list[str]:
        """Return all unique domain names in the registry."""
        return sorted(set(a.domain for a in self._anchors))

    def get_tiers(self) -> list[str]:
        """Return all unique tier names in the registry."""
        return sorted(set(a.tier for a in self._anchors))

    def __len__(self) -> int:
        return len(self._anchors)

    def __contains__(self, name: str) -> bool:
        return self.get_anchor(name) is not None
