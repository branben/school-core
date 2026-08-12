"""Tests for the canonical persona capability manifest."""

from pathlib import Path

from capabilities import (
    TASK_ROLE_HERMES_TOOLSETS,
    TASK_ROLE_PROFILES,
    capability_for_task_role,
    resolve_capability,
)
from role_loader import RoleLoader


ROOT = Path(__file__).resolve().parents[1]


def test_resolve_capability_joins_rank_task_role_profile_tools_and_skills():
    bundle = resolve_capability(
        "python-testing",
        30,
        task_role="coder",
        difficulty="medium",
    )

    assert bundle.school_role == "Teacher"
    assert bundle.task_role == "coder"
    assert bundle.profile == "student-coder"
    assert "python" in bundle.allowed_tools
    assert "testing" in bundle.allowed_tools
    assert bundle.gate_min == 25
    assert bundle.gate_max == 74
    assert "TDD Chicago School" in bundle.skills
    assert bundle.to_dict()["allowed_tools"] == ("python", "testing", "git")
    assert bundle.hermes_toolsets == TASK_ROLE_HERMES_TOOLSETS["coder"]
    assert "git" not in bundle.hermes_toolsets


def test_domain_routing_selects_existing_specialized_role():
    assert resolve_capability("code-review", 80).task_role == "reviewer"
    assert resolve_capability("git-operations", 10).task_role == "executor"
    assert resolve_capability("web-automation", 10).task_role == "browser"


def test_every_task_role_has_a_native_hermes_toolset_policy():
    for role, toolsets in TASK_ROLE_HERMES_TOOLSETS.items():
        assert role in TASK_ROLE_PROFILES
        assert toolsets
        assert all(name.islower() and " " not in name for name in toolsets)


def test_lora_role_uses_coder_compatibility_contract():
    bundle = resolve_capability("python-coding", 10, task_role="lora-python-coding")
    assert bundle.task_role == "coder"
    assert bundle.profile == "student-coder"


def test_unknown_task_role_fails_closed_to_coder_contract():
    profile, tools = capability_for_task_role("new-role-not-yet-registered")
    assert profile == TASK_ROLE_PROFILES["coder"]
    assert tools == ("python", "testing", "git")


def test_rank_boundaries_match_role_loader():
    loader = RoleLoader(ROOT / "config" / "roles")
    assert resolve_capability("python-coding", 24, role_loader=loader).school_role == "Student"
    assert resolve_capability("python-coding", 25, role_loader=loader).school_role == "Teacher"
    assert resolve_capability("python-coding", 75, role_loader=loader).school_role == "Faculty"
