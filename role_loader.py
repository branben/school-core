from __future__ import annotations

import sys

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Role:
    name: str
    gate_min: int
    gate_max: int
    domain_expertise: List[str]
    rules: List[str]
    criteria: List[str]
    escalation: List[str]
    version: str = "1.0.0"
    updated: str = ""


ROLE_DIR = Path(__file__).parent / "config" / "roles"
ROLE_NAMES = ["student", "teacher", "faculty"]


def _parse_yaml_frontmatter(text: str) -> tuple[dict, dict]:
    """Split a YAML file with frontmatter (--- ... ---) into (frontmatter, body)."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, yaml.safe_load(text) or {}
    front = yaml.safe_load(parts[1]) or {}
    body = yaml.safe_load(parts[2]) or {}
    return front, body


def _load_role_file(role_name: str) -> dict:
    """Load a single role YAML file and return merged frontmatter + role body."""
    path = ROLE_DIR / f"{role_name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Role file not found: {path}")
    text = path.read_text(encoding="utf-8")
    front, body = _parse_yaml_frontmatter(text)
    role_section = body.get("role", {})
    return {
        "name": front.get("name", role_section.get("name", role_name)),
        "version": front.get("version", "1.0.0"),
        "updated": front.get("updated", ""),
        "gate_min": front.get("gate_range", {}).get("min", role_section.get("gate_min", 0)),
        "gate_max": front.get("gate_range", {}).get("max", role_section.get("gate_max", 100)),
        "domain_expertise": front.get("domain_expertise", role_section.get("domain_expertise", [])),
        "rules": front.get("rules_of_engagement", role_section.get("rules_of_engagement", [])),
        "criteria": front.get("evaluation_criteria", role_section.get("evaluation_criteria", [])),
        "escalation": front.get("escalation_conditions", role_section.get("escalation_conditions", [])),
    }


def _build_role(data: dict) -> Role:
    return Role(
        name=data["name"],
        gate_min=data["gate_min"],
        gate_max=data["gate_max"],
        domain_expertise=data["domain_expertise"],
        rules=data["rules"],
        criteria=data["criteria"],
        escalation=data["escalation"],
        version=data.get("version", "1.0.0"),
        updated=data.get("updated", ""),
    )


class RoleLoader:
    def __init__(self, role_dir: str | Path = None):
        self._role_dir = Path(role_dir) if role_dir else ROLE_DIR
        self._cache: Dict[str, Role] = {}
        self._definitions: Dict[str, dict] = {}

    def _resolve_path(self, role_name: str) -> Path:
        return self._role_dir / f"{role_name}.yaml"

    def load_role(self, role_name: str) -> Role:
        if role_name in self._cache:
            return self._cache[role_name]
        path = self._resolve_path(role_name)
        if not path.exists():
            raise FileNotFoundError(f"Role file not found: {path}")
        text = path.read_text(encoding="utf-8")
        front, body = _parse_yaml_frontmatter(text)
        role_section = body.get("role", {})
        data = {
            "name": front.get("name", role_section.get("name", role_name)),
            "version": front.get("version", "1.0.0"),
            "updated": front.get("updated", ""),
            "gate_min": front.get("gate_range", {}).get("min", role_section.get("gate_min", 0)),
            "gate_max": front.get("gate_range", {}).get("max", role_section.get("gate_max", 100)),
            "domain_expertise": front.get("domain_expertise", role_section.get("domain_expertise", [])),
            "rules": front.get("rules_of_engagement", role_section.get("rules_of_engagement", [])),
            "criteria": front.get("evaluation_criteria", role_section.get("evaluation_criteria", [])),
            "escalation": front.get("escalation_conditions", role_section.get("escalation_conditions", [])),
        }
        self._definitions[role_name] = data
        role = _build_role(data)
        self._cache[role_name] = role
        return role

    def get_role(self, agent_score: float, domain: str | None = None) -> Role:
        """Resolve the lane for ``agent_score``, clamping to the lowest on a miss.

        The gates are integer-bounded and adjacent with no overlap:

            student   0 - 24    (config/roles/student.yaml:26-27)
            teacher  25 - 74    (config/roles/teacher.yaml:29-30)
            faculty  75 - 100   (config/roles/faculty.yaml:30-31)

        Scores are FLOATS (averaged review scores), so every value in the open
        intervals (24, 25) and (74, 75) matched no lane and this raised. Live run
        32330426471 hit exactly that on issue #338 — "No role found for score
        24.13" — which aborted capability resolution and dropped the issue to the
        direct path.

        Clamping to ROLE_NAMES[0] rather than raising, because student IS the
        bottom/remedial lane by design (student.yaml:21-22: "Cannot solve after 2
        attempts -> escalate to Teacher"); a low score is meant to route DOWN,
        not to abort dispatch.

        Clamping DOWN specifically: an unmatched score must never be promoted.
        Falling back to the lowest lane can only under-assign capability, which
        is recoverable via the documented escalation path — whereas defaulting to
        the nearest or highest lane would hand work to an unqualified-for-it lane
        silently. Widening a gate would fix 24.13 and leave 74.x to be
        rediscovered; this closes the whole class.
        """
        for role_name in ROLE_NAMES:
            role = self.load_role(role_name)
            if role.gate_min <= agent_score <= role.gate_max:
                return role
        sys.stderr.write(
            f"[role_loader] score {agent_score} matched no lane gate "
            f"(gaps exist at the integer boundaries); clamping to "
            f"'{ROLE_NAMES[0]}' — never promoting on a miss\n"
        )
        return self.load_role(ROLE_NAMES[0])

    def get_role_definition(self, role_name: str) -> dict:
        if role_name not in self._definitions:
            self.load_role(role_name)
        return self._definitions[role_name]

    def list_roles(self) -> List[Role]:
        return [self.load_role(name) for name in ROLE_NAMES]


_default_loader: Optional[RoleLoader] = None


def _get_default_loader() -> RoleLoader:
    global _default_loader
    if _default_loader is None:
        _default_loader = RoleLoader()
    return _default_loader


def get_role(agent_score: float, domain: str | None = None) -> Role:
    return _get_default_loader().get_role(agent_score, domain)


def get_role_definition(role_name: str) -> dict:
    return _get_default_loader().get_role_definition(role_name)
