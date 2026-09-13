"""packet.yaml contract schema — typed workflow contracts.

Defines the ``Packet`` dataclass hierarchy that carries intent from
Design → Orchestrator → Implementation → Review. Packets serialize
to both YAML (human-readable) and JSON (Orca task spec).

Validation rules (see ``validate``):
  - ``intent.what`` required, non-empty
  - ``acceptance_criteria`` required, at least 1 entry
  - ``anchor`` must resolve to a KC note (warning if not, not error)
  - ``bd_issue`` must start with a valid prefix for the repo
"""

from __future__ import annotations

import dataclasses
import json
import re
import warnings
from datetime import datetime, timezone
from typing import Any, List, Optional

import yaml

KC_ROOT = "/Users/brandonbennett/Documents/KnowledgeCore"
PLAN_ANCHOR_RE = re.compile(r"anchor:\s*(plan-[a-z0-9-]+)")
VALID_BD_PREFIXES = ("school-core-", "brandonbennett-", "branben-")


@dataclasses.dataclass
class Intent:
    what: str
    why: str
    anchor: str  # KC plan anchor, e.g. "plan-omniroute-admission-lanes"


@dataclasses.dataclass
class Scope:
    files_in_scope: List[str] = dataclasses.field(default_factory=list)
    files_out_of_scope: List[str] = dataclasses.field(default_factory=list)
    max_lines_changed: int = 500


@dataclasses.dataclass
class Provenance:
    bd_issue: str
    kc_plan: str
    issued_by: str = "director-console"
    issued_at: str = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclasses.dataclass
class Packet:
    intent: Intent
    acceptance_criteria: List[str]
    scope: Scope
    provenance: Provenance
    verdict_criteria: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for YAML or JSON output."""
        return {
            "intent": dataclasses.asdict(self.intent),
            "acceptance_criteria": self.acceptance_criteria,
            "scope": dataclasses.asdict(self.scope),
            "provenance": dataclasses.asdict(self.provenance),
            "verdict_criteria": self.verdict_criteria,
        }

    def to_yaml(self) -> str:
        """Serialize to YAML."""
        return yaml.dump(self.to_dict(), default_flow_style=False, sort_keys=False)

    def to_json(self) -> str:
        """Serialize to JSON (for Orca task spec)."""
        return json.dumps(self.to_dict(), indent=2, default=str)


class ValidationError(Exception):
    """Raised when a packet fails validation."""


def validate(packet: Packet) -> list[str]:
    """Validate a packet against the contract rules.

    Returns a list of warning strings. Raises ``ValidationError`` on
    hard failures (missing required fields, invalid bd_issue prefix).
    """
    errors: list[str] = []
    warnings_list: list[str] = []

    # intent.what required, non-empty
    if not packet.intent.what or not packet.intent.what.strip():
        errors.append("intent.what is required and must be non-empty")

    # acceptance_criteria required, at least 1 entry
    if not packet.acceptance_criteria or len(packet.acceptance_criteria) < 1:
        errors.append("acceptance_criteria is required (at least 1 entry)")

    # bd_issue must start with valid prefix
    if not any(packet.provenance.bd_issue.startswith(p) for p in VALID_BD_PREFIXES):
        errors.append(
            f"bd_issue must start with one of {VALID_BD_PREFIXES}, "
            f"got: {packet.provenance.bd_issue!r}"
        )

    # anchor must resolve to a KC note (warning if not, not error)
    if packet.intent.anchor:
        if not _kc_plan_exists(packet.intent.anchor):
            warnings_list.append(
                f"anchor {packet.intent.anchor!r} does not resolve to a KC plan"
            )

    # Emit warnings
    for w in warnings_list:
        warnings.warn(w, UserWarning, stacklevel=2)

    if errors:
        raise ValidationError("; ".join(errors))

    return warnings_list


def _kc_plan_exists(anchor: str) -> bool:
    """Check whether a KC plan with the given anchor exists in the vault."""
    import os

    plans_dir = os.path.join(KC_ROOT, "docs", "plans")
    if not os.path.isdir(plans_dir):
        return False

    for fname in os.listdir(plans_dir):
        if anchor in fname and fname.endswith(".md"):
            fpath = os.path.join(plans_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    head = f.read(2000)
            except OSError:
                return False
            if re.search(r"^status:\s*active", head, re.MULTILINE):
                return True
    return False
