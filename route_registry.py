"""Deterministic route/verifier registry for the local school-core stack.

The global CE matrix contains workflow names that are not all installed as
standalone skills. This registry makes those substitutions explicit and gives
each route a mechanical verifier. A missing fallback or verifier is ``blocked``
—not a silent pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class RouteResolution:
    target: str
    skill: Optional[str]
    verifier: Optional[str]
    status: str
    fallback: bool = False
    reason: Optional[str] = None


# These are intentional compatibility mappings, not claims that the fallback
# is semantically identical to a future CE-native skill.
_ROUTE_FALLBACKS = {
    "ce-work": "incremental-implementation",
    "ce-debug": "debugging-and-error-recovery",
    "ce-compound": "loop-library",
    "ce-optimize": "performance-optimization",
    "ce-ideate": "idea-refine",
    "ce-brainstorm": "idea-refine",
    "verify-delegated-work": "code-review-and-quality",
}

_VERIFIERS = {
    "ce-plan": "bd lint + plan acceptance review",
    "ce-work": ".venv/bin/python -m pytest",
    "ce-debug": ".venv/bin/python -m pytest",
    "ce-compound": ".venv/bin/python -m pytest tests/test_compound_learning.py",
    "ce-optimize": ".venv/bin/python -m pytest tests/test_fast_lane.py tests/test_skillopt_contract.py",
    "ce-ideate": "human decision record + bd issue before implementation",
    "ce-brainstorm": "human decision record + bd issue before implementation",
    "verify-delegated-work": ".venv/bin/python -m pytest tests/test_review_packet.py tests/test_issue_bridge.py",
}


def _skill_exists(skill: str, roots: Iterable[Path]) -> bool:
    return any((Path(root) / skill / "SKILL.md").is_file() for root in roots)


def resolve_route(target: str, *, skill_roots: Iterable[Path]) -> RouteResolution:
    """Resolve one matrix target, explicitly using a fallback when needed."""
    target = str(target or "").strip()
    if not target:
        return RouteResolution("", None, None, "blocked", reason="target_missing")
    roots = tuple(Path(root) for root in skill_roots)
    if _skill_exists(target, roots) and _VERIFIERS.get(target):
        return RouteResolution(target, target, _VERIFIERS[target], "ready")
    fallback = _ROUTE_FALLBACKS.get(target)
    if fallback and _skill_exists(fallback, roots) and _VERIFIERS.get(target):
        return RouteResolution(
            target,
            fallback,
            _VERIFIERS[target],
            "ready",
            fallback=True,
            reason=f"matrix target unavailable; explicit fallback={fallback}",
        )
    return RouteResolution(
        target,
        None,
        None,
        "blocked",
        reason="skill_or_verifier_unavailable",
    )


def audit_routes(targets: Iterable[str], *, skill_roots: Iterable[Path]) -> dict:
    """Audit every target and return a bounded, machine-readable report."""
    resolutions = [resolve_route(target, skill_roots=skill_roots) for target in targets]
    blocked = [item.target for item in resolutions if item.status != "ready"]
    return {
        "schema_version": 1,
        "status": "ready" if not blocked else "blocked",
        "targets": [
            {
                "target": item.target,
                "skill": item.skill,
                "verifier": item.verifier,
                "status": item.status,
                "fallback": item.fallback,
                "reason": item.reason,
            }
            for item in resolutions
        ],
        "blocked_targets": blocked,
    }


__all__ = ["RouteResolution", "audit_routes", "resolve_route"]
