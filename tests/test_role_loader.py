from __future__ import annotations

import pytest
from pathlib import Path

from role_loader import (
    Role,
    RoleLoader,
    get_role,
    get_role_definition,
    _parse_yaml_frontmatter,
)


ROLE_DIR = Path(__file__).parent.parent / "config" / "roles"


class TestParseYamlFrontmatter:
    def test_splits_frontmatter_and_body(self):
        text = "---\nname: Test\n---\nrole:\n  name: Test\n"
        front, body = _parse_yaml_frontmatter(text)
        assert front == {"name": "Test"}
        assert body == {"role": {"name": "Test"}}

    def test_no_frontmatter_returns_empty_front(self):
        text = "role:\n  name: Test\n"
        front, body = _parse_yaml_frontmatter(text)
        assert front == {}
        assert body == {"role": {"name": "Test"}}


class TestRoleLoaderHappyPath:
    def setup_method(self):
        self.loader = RoleLoader()

    def test_score_15_returns_student(self):
        role = self.loader.get_role(15)
        assert role.name == "Student"
        assert role.gate_min == 0
        assert role.gate_max == 24

    def test_score_0_returns_student(self):
        role = self.loader.get_role(0)
        assert role.name == "Student"

    def test_score_24_returns_student(self):
        role = self.loader.get_role(24)
        assert role.name == "Student"

    def test_score_25_returns_teacher(self):
        role = self.loader.get_role(25)
        assert role.name == "Teacher"
        assert role.gate_min == 25
        assert role.gate_max == 74

    def test_score_50_returns_teacher(self):
        role = self.loader.get_role(50)
        assert role.name == "Teacher"

    def test_score_74_returns_teacher(self):
        role = self.loader.get_role(74)
        assert role.name == "Teacher"

    def test_score_75_returns_faculty(self):
        role = self.loader.get_role(75)
        assert role.name == "Faculty"
        assert role.gate_min == 75
        assert role.gate_max == 100

    def test_score_100_returns_faculty(self):
        role = self.loader.get_role(100)
        assert role.name == "Faculty"


class TestRoleLoaderGateBoundaries:
    def setup_method(self):
        self.loader = RoleLoader()

    def test_boundary_24_not_teacher(self):
        role = self.loader.get_role(24)
        assert role.name != "Teacher"
        assert role.name == "Student"

    def test_boundary_25_not_student(self):
        role = self.loader.get_role(25)
        assert role.name != "Student"
        assert role.name == "Teacher"

    def test_boundary_74_not_faculty(self):
        role = self.loader.get_role(74)
        assert role.name != "Faculty"
        assert role.name == "Teacher"

    def test_boundary_75_not_teacher(self):
        role = self.loader.get_role(75)
        assert role.name != "Teacher"
        assert role.name == "Faculty"


class TestRoleLoaderDomainParameter:
    def setup_method(self):
        self.loader = RoleLoader()

    def test_domain_does_not_change_role(self):
        role_no_domain = self.loader.get_role(15)
        role_with_domain = self.loader.get_role(15, domain="python-testing")
        assert role_no_domain.name == role_with_domain.name

    def test_domain_accepted_for_all_roles(self):
        for score, expected in [(10, "Student"), (40, "Teacher"), (80, "Faculty")]:
            role = self.loader.get_role(score, domain="code-review")
            assert role.name == expected


class TestRoleMetadata:
    def setup_method(self):
        self.loader = RoleLoader()

    def test_student_has_version(self):
        role = self.loader.load_role("student")
        assert role.version == "1.0.0"

    def test_teacher_has_version(self):
        role = self.loader.load_role("teacher")
        assert role.version == "1.0.0"

    def test_faculty_has_version(self):
        role = self.loader.load_role("faculty")
        assert role.version == "1.0.0"

    def test_student_has_updated(self):
        role = self.loader.load_role("student")
        assert role.updated == "2026-06-14"

    def test_teacher_has_updated(self):
        role = self.loader.load_role("teacher")
        assert role.updated == "2026-06-14"

    def test_faculty_has_updated(self):
        role = self.loader.load_role("faculty")
        assert role.updated == "2026-06-14"


class TestRoleDefinition:
    def setup_method(self):
        self.loader = RoleLoader()

    def test_get_role_definition_returns_dict(self):
        defn = self.loader.get_role_definition("student")
        assert isinstance(defn, dict)
        assert defn["name"] == "Student"

    def test_definition_has_all_fields(self):
        for name in ["student", "teacher", "faculty"]:
            defn = self.loader.get_role_definition(name)
            assert "name" in defn
            assert "version" in defn
            assert "updated" in defn
            assert "gate_min" in defn
            assert "gate_max" in defn
            assert "domain_expertise" in defn
            assert "rules" in defn
            assert "criteria" in defn
            assert "escalation" in defn


class TestRoleDataclass:
    def setup_method(self):
        self.loader = RoleLoader()

    def test_role_is_dataclass(self):
        role = self.loader.load_role("student")
        assert hasattr(role, "__dataclass_fields__")

    def test_role_fields(self):
        role = RoleLoader().load_role("student")
        assert role.name == "Student"
        assert isinstance(role.gate_min, int)
        assert isinstance(role.gate_max, int)
        assert isinstance(role.domain_expertise, list)
        assert isinstance(role.rules, list)
        assert isinstance(role.criteria, list)
        assert isinstance(role.escalation, list)

    def test_student_domain_expertise(self):
        role = RoleLoader().load_role("student")
        assert "basic_python" in role.domain_expertise
        assert "testing_fundamentals" in role.domain_expertise

    def test_teacher_domain_expertise(self):
        role = RoleLoader().load_role("teacher")
        assert "clean_architecture" in role.domain_expertise
        assert "test_driven_development" in role.domain_expertise

    def test_faculty_domain_expertise(self):
        role = RoleLoader().load_role("faculty")
        assert "system_design" in role.domain_expertise
        assert "architecture_review" in role.domain_expertise


class TestMissingRoleFile:
    def test_missing_file_raises_file_not_found(self):
        loader = RoleLoader(role_dir="/tmp/nonexistent_roles")
        with pytest.raises(FileNotFoundError, match="Role file not found"):
            loader.load_role("ghost")

    def test_missing_file_on_get_role_raises(self):
        loader = RoleLoader(role_dir="/tmp/nonexistent_roles")
        with pytest.raises(FileNotFoundError):
            loader.get_role(50)


class TestModuleLevelFunctions:
    def test_get_role_module_level(self):
        role = get_role(15)
        assert role.name == "Student"

    def test_get_role_definition_module_level(self):
        defn = get_role_definition("teacher")
        assert defn["name"] == "Teacher"


class TestListRoles:
    def test_list_roles_returns_three(self):
        loader = RoleLoader()
        roles = loader.list_roles()
        assert len(roles) == 3

    def test_list_roles_names(self):
        loader = RoleLoader()
        names = [r.name for r in loader.list_roles()]
        assert names == ["Student", "Teacher", "Faculty"]


class TestBackwardCompatibility:
    def test_role_loader_failure_falls_back_gracefully(self):
        loader = RoleLoader(role_dir="/tmp/nonexistent_roles")
        with pytest.raises(FileNotFoundError):
            loader.get_role(50)

    def test_existing_role_anchors_unchanged(self):
        from prompt_composer import ROLE_ANCHORS
        assert "student" in ROLE_ANCHORS
        assert "teacher" in ROLE_ANCHORS
        assert "faculty" in ROLE_ANCHORS
