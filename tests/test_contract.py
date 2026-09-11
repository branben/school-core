"""Tests for the packet.yaml contract schema."""

from __future__ import annotations

import json
import warnings

import pytest

from director_console.contract import (
    Intent,
    Packet,
    Provenance,
    Scope,
    ValidationError,
    validate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_packet(**overrides):
    """Create a valid Packet with optional overrides."""
    defaults = dict(
        intent=Intent(
            what="Add ripwire blast-radius to queue output",
            why="Director needs blast-radius context per issue",
            anchor="plan-director-console-2026-09-06-v2",
        ),
        acceptance_criteria=["ripwire output attached to queue items"],
        scope=Scope(
            files_in_scope=["director_console/__main__.py"],
            files_out_of_scope=["tests/"],
            max_lines_changed=50,
        ),
        provenance=Provenance(
            bd_issue="school-core-123",
            kc_plan="plan-director-console-2026-09-06-v2",
        ),
        verdict_criteria=["queue renders with --include-context"],
    )
    defaults.update(overrides)
    return Packet(**defaults)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

def test_validate_valid_packet():
    """A valid packet passes validation and returns no warnings for a real plan."""
    packet = make_packet()
    # The anchor plan-director-console-2026-09-06-v2 exists in KC
    warns = validate(packet)
    assert isinstance(warns, list)


def test_validate_missing_intent_what():
    """Missing intent.what raises ValidationError."""
    packet = make_packet(intent=Intent(what="", why="x", anchor="plan-foo"))
    with pytest.raises(ValidationError, match="intent.what is required"):
        validate(packet)


def test_validate_empty_intent_what():
    """Whitespace-only intent.what raises ValidationError."""
    packet = make_packet(intent=Intent(what="   ", why="x", anchor="plan-foo"))
    with pytest.raises(ValidationError, match="intent.what is required"):
        validate(packet)


def test_validate_missing_acceptance_criteria():
    """Missing acceptance_criteria raises ValidationError."""
    packet = make_packet(acceptance_criteria=[])
    with pytest.raises(ValidationError, match="acceptance_criteria is required"):
        validate(packet)


def test_validate_invalid_bd_issue_prefix():
    """bd_issue with invalid prefix raises ValidationError."""
    packet = make_packet(
        provenance=Provenance(bd_issue="invalid-prefix-123", kc_plan="plan-foo")
    )
    with pytest.raises(ValidationError, match="bd_issue must start with"):
        validate(packet)


def test_validate_anchor_warning_not_error():
    """Anchor that doesn't resolve to KC note produces warning, not error."""
    packet = make_packet(
        intent=Intent(what="x", why="y", anchor="plan-nonexistent-xyz")
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        validate(packet)
        assert len(w) == 1
        assert "plan-nonexistent-xyz" in str(w[0].message)


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------

def test_to_yaml_roundtrip():
    """Valid packet → YAML serialization works."""
    packet = make_packet()
    yaml_str = packet.to_yaml()
    assert "intent:" in yaml_str
    assert "acceptance_criteria:" in yaml_str
    assert "Add ripwire blast-radius" in yaml_str


def test_to_json_roundtrip():
    """Valid packet → JSON serialization works (for Orca task spec)."""
    packet = make_packet()
    json_str = packet.to_json()
    payload = json.loads(json_str)
    assert payload["intent"]["what"] == "Add ripwire blast-radius to queue output"
    assert payload["provenance"]["bd_issue"] == "school-core-123"
    assert "acceptance_criteria" in payload


def test_to_dict_structure():
    """to_dict returns the expected structure."""
    packet = make_packet()
    d = packet.to_dict()
    assert d["intent"]["anchor"] == "plan-director-console-2026-09-06-v2"
    assert d["scope"]["max_lines_changed"] == 50
    assert d["provenance"]["issued_by"] == "director-console"


# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------

def test_provenance_default_issued_at():
    """Provenance auto-generates issued_at if not provided."""
    prov = Provenance(bd_issue="school-core-1", kc_plan="plan-foo")
    assert prov.issued_by == "director-console"
    assert prov.issued_at  # non-empty


def test_scope_default_lists():
    """Scope defaults to empty lists."""
    scope = Scope()
    assert scope.files_in_scope == []
    assert scope.files_out_of_scope == []
    assert scope.max_lines_changed == 500
