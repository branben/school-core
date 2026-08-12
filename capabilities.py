"""Canonical runtime capability bundles for Agent School personas.

This module joins the existing declarative school-rank roles with the existing
specialized task roles. It is intentionally additive: routing.py and executor.py
remain the compatibility path while callers migrate to ``resolve_capability``.

A capability bundle answers one operational question:
"Why did this persona receive this profile, skill set, and tool contract?"
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from anchor_loader import AnchorRegistry
from executor import DOMAIN_ROLE_MAP, get_role_for_domain
from role_loader import RoleLoader


TASK_ROLE_PROFILES: dict[str, str] = {
    "coder": "student-coder",
    "searcher": "student-searcher",
    "executor": "student-executor",
    "browser": "student-browser",
    "reviewer": "student-reviewer",
    "tester": "student-coder",
    "debugger": "student-coder",
}

# These are the tool contracts already described by ROLE_SYSTEM_PROMPTS and
# the Hermes/Orca call sites. They are metadata, not an authorization bypass.
TASK_ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "coder": ("python", "testing", "git"),
    "searcher": ("rg", "ast-grep", "git"),
    "executor": ("shell", "git", "build"),
    "browser": ("browser", "screenshots", "forms"),
    "reviewer": ("adversarial-review", "security-analysis"),
    "tester": ("python", "testing", "git"),
    "debugger": ("python", "testing", "git"),
}

# Hermes receives native toolset names, not the school-facing semantic labels
# above. Keep this allowlisted mapping in the canonical capability manifest so
# every launcher uses the same role policy. These names are the toolsets already
# proven by the FirstMate wrapper; absence from a bundle is intentional.
TASK_ROLE_HERMES_TOOLSETS: dict[str, tuple[str, ...]] = {
    "coder": (
        "clarify", "delegation", "engram", "file", "memory", "serena",
        "skills", "terminal", "todo",
    ),
    "searcher": (
        "clarify", "codegraph", "cocoindex", "file", "memory",
        "session_search", "serena", "skills", "terminal",
    ),
    "executor": ("file", "memory", "skills", "terminal", "todo"),
    "browser": ("file", "memory", "skills", "terminal", "web"),
    "reviewer": (
        "codegraph", "cocoindex", "file", "memory", "serena", "skills",
        "terminal",
    ),
    "tester": (
        "clarify", "file", "memory", "serena", "skills", "terminal", "todo",
    ),
    "debugger": (
        "clarify", "codegraph", "file", "memory", "serena", "skills",
        "terminal", "todo",
    ),
}

# Keep this mapping in one place for prompt/skill observability. The names are
# the domains already used by director._anchor_context.
TASK_ROLE_SKILL_DOMAINS: dict[str, tuple[str, ...]] = {
    "coder": ("code-implementation", "python-testing"),
    "searcher": ("debugging",),
    "executor": ("git-operations",),
    "reviewer": ("code-review",),
    "browser": (),
    "tester": ("python-testing",),
    "debugger": ("debugging",),
}


@dataclass(frozen=True)
class CapabilityBundle:
    """Resolved capability contract for one task dispatch."""

    version: str
    domain: str
    difficulty: str
    score: float
    school_role: str
    task_role: str
    profile: str
    skills: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    gate_min: int
    gate_max: int
    escalation: tuple[str, ...]
    # Trailing default preserves positional construction compatibility for
    # callers that only know the school-facing capability contract.
    hermes_toolsets: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """Return a JSON-safe representation for run records and bookbags."""
        return asdict(self)


def _base_task_role(role: str) -> str:
    """Normalize LoRA and compatibility role names to a task-role contract."""
    if role.startswith("lora-"):
        return "coder"
    return role if role in TASK_ROLE_PROFILES else "coder"


def _skill_names(registry: AnchorRegistry, task_role: str, difficulty: str) -> tuple[str, ...]:
    names: list[str] = []
    for domain in TASK_ROLE_SKILL_DOMAINS.get(task_role, ()):
        for name in registry.get_anchor_names(domain=domain, difficulty_gate=difficulty):
            if name not in names:
                names.append(name)
    return tuple(names)


def resolve_capability(
    domain: str,
    score: float,
    *,
    task_role: Optional[str] = None,
    difficulty: str = "easy",
    role_loader: Optional[RoleLoader] = None,
    anchor_registry: Optional[AnchorRegistry] = None,
) -> CapabilityBundle:
    """Resolve the canonical capability bundle for a dispatch.

    ``score`` selects the existing Student/Teacher/Faculty school rank. The
    domain selects the existing specialized task role. A caller may pass
    ``task_role`` when it already performed that mapping (for example a forced
    leaf or a LoRA role).
    """

    loader = role_loader or RoleLoader()
    registry = anchor_registry or AnchorRegistry()
    normalized_task_role = _base_task_role(task_role or get_role_for_domain(domain))
    school_role = loader.get_role(float(score), domain=domain)

    return CapabilityBundle(
        version="1.0.0",
        domain=domain,
        difficulty=difficulty,
        score=float(score),
        school_role=school_role.name,
        task_role=normalized_task_role,
        profile=TASK_ROLE_PROFILES[normalized_task_role],
        skills=_skill_names(registry, normalized_task_role, difficulty),
        allowed_tools=TASK_ROLE_TOOLS[normalized_task_role],
        hermes_toolsets=TASK_ROLE_HERMES_TOOLSETS[normalized_task_role],
        gate_min=school_role.gate_min,
        gate_max=school_role.gate_max,
        escalation=tuple(school_role.escalation),
    )


def capability_for_task_role(role: str) -> tuple[str, tuple[str, ...]]:
    """Return the compatibility profile and tool contract for a task role."""

    normalized = _base_task_role(role)
    return TASK_ROLE_PROFILES[normalized], TASK_ROLE_TOOLS[normalized]


__all__ = [
    "CapabilityBundle",
    "TASK_ROLE_PROFILES",
    "TASK_ROLE_TOOLS",
    "TASK_ROLE_HERMES_TOOLSETS",
    "TASK_ROLE_SKILL_DOMAINS",
    "capability_for_task_role",
    "resolve_capability",
]
