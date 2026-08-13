"""Canonical evidence packet shared by the director and issue bridge.

The packet is an internal, additive contract. Existing ``review`` fields stay
available for compatibility; new callers can use this object to avoid rerunning
an authoritative verify or semantic review stage.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


SCHEMA_VERSION = 1
_MAX_TEXT = 500
_MAX_FINDINGS = 50
_ALLOWED_ARTIFACT_KEYS = {
    "bead", "repo", "worktree", "branch", "commit", "base", "crew_id",
}


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    value = "" if value is None else str(value)
    value = re.sub(r"/(?:Users|home)/[^\s/]+", "~", value)
    return value[:limit]


def _findings(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for finding in value[:_MAX_FINDINGS]:
        if isinstance(finding, dict):
            result.append({
                key: _text(finding.get(key))
                for key in ("section", "issue_class", "severity", "citation", "description", "suggestion")
                if finding.get(key) is not None
            })
        elif finding:
            result.append({"description": _text(finding)})
    return result


def _judge(value: Any) -> dict:
    value = value if isinstance(value, dict) else {}
    result = {
        "verdict": _text(value.get("verdict", ""), 32),
        "score": max(0.0, min(100.0, float(value.get("score", 0) or 0))),
        "findings": _findings(value.get("findings")),
    }
    if value.get("confidence") is not None:
        result["confidence"] = max(0.0, min(1.0, float(value.get("confidence") or 0)))
    return result


def _verification_was_attempted(value: Any) -> bool:
    """Return whether the packet contains evidence from an actual gate run."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(value, dict):
        return False
    try:
        ran = max(0, int(value.get("ran", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        ran = 0
    return bool(
        value.get("passed")
        or value.get("skipped")
        or value.get("strict_escalated")
        or value.get("gate_error")
        or ran > 0
    )


def _verification(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = {}
    value = value if isinstance(value, dict) else {}
    return {
        "passed": bool(value.get("passed")),
        "skipped": bool(value.get("skipped")),
        "strict_escalated": bool(value.get("strict_escalated")),
        "gate_error": bool(value.get("gate_error")),
        "ran": max(0, int(value.get("ran", 0) or 0)),
        "failures": [
            {
                "cmd": _text(failure.get("cmd", ""), 180),
                "exit": failure.get("exit"),
                "stderr": _text(failure.get("stderr", "")),
            }
            for failure in (value.get("failures") or [])[:_MAX_FINDINGS]
            if isinstance(failure, dict)
        ],
        "telemetry": dict(value.get("telemetry") or {}),
    }


class ReviewPacket:
    """Validated canonical evidence and verdict for one review run."""

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def create(
        cls,
        *,
        artifact: Optional[dict] = None,
        execution: Optional[dict] = None,
        verification: Optional[dict] = None,
        entire: Optional[dict] = None,
        cto: Optional[dict] = None,
        coo: Optional[dict] = None,
        accepted: bool = False,
        verification_authoritative: Optional[bool] = None,
    ) -> "ReviewPacket":
        artifact = artifact if isinstance(artifact, dict) else {}
        safe_artifact = {
            key: _text(artifact[key], 240)
            for key in _ALLOWED_ARTIFACT_KEYS
            if artifact.get(key) is not None
        }
        cto_data = _judge(cto)
        coo_data = _judge(coo)
        if verification_authoritative is None:
            verification_authoritative = _verification_was_attempted(verification)
        return cls({
            "schema_version": SCHEMA_VERSION,
            "authority": "director",
            "artifact": safe_artifact,
            "execution": {"findings": _findings((execution or {}).get("findings"))},
            "verification": _verification(verification),
            "verification_authoritative": bool(verification_authoritative),
            "entire": cls._sensor(entire),
            "output_verification": None,
            "judges": {"cto": cto_data, "coo": coo_data},
            "accepted": bool(accepted),
            "verdict": "ACCEPTED" if accepted else "REJECTED",
        })

    @staticmethod
    def _sensor(value: Optional[dict]) -> Optional[dict]:
        if not isinstance(value, dict):
            return None
        return {
            "status": _text(value.get("status", ""), 32),
            "findings": max(0, int(value.get("findings", 0) or 0)),
        }

    @classmethod
    def from_dict(cls, value: Any) -> Optional["ReviewPacket"]:
        if not isinstance(value, dict):
            return None
        if value.get("schema_version") != SCHEMA_VERSION or value.get("authority") != "director":
            return None
        try:
            judges = value.get("judges") or {}
            packet = cls.create(
                artifact=value.get("artifact"),
                execution=value.get("execution"),
                verification=value.get("verification"),
                entire=value.get("entire"),
                cto=judges.get("cto"),
                coo=judges.get("coo"),
                accepted=bool(value.get("accepted")),
                verification_authoritative=bool(value.get("verification_authoritative", False)),
            )
            packet._data["output_verification"] = value.get("output_verification")
            return packet
        except (TypeError, ValueError):
            return None

    @property
    def is_authoritative(self) -> bool:
        return self._data.get("authority") == "director"

    @property
    def is_verification_authoritative(self) -> bool:
        return bool(self._data.get("verification_authoritative"))

    @property
    def accepted(self) -> bool:
        return bool(self._data.get("accepted"))

    @property
    def verification(self) -> dict:
        return self._data["verification"]

    def attach_entire(self, entire: Optional[dict]) -> None:
        self._data["entire"] = self._sensor(entire)

    def attach_output_verification(self, verification: Optional[dict]) -> None:
        if not isinstance(verification, dict):
            return
        self._data["output_verification"] = {
            "score": max(0, min(100, int(verification.get("score", 0) or 0))),
            "verdict": _text(verification.get("verdict", ""), 32),
        }

    def adversarial_summary(self) -> dict:
        judges = self._data["judges"]
        findings = judges["cto"]["findings"] + judges["coo"]["findings"]
        score = (judges["cto"]["score"] + judges["coo"]["score"]) / 2.0
        return {
            "verdict": "PASS" if self.accepted else "FAIL",
            "score": score,
            "findings": findings,
            "lens_used": "canonical-cto-coo",
            "confidence": 1.0,
            "canonical": True,
        }

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self._data))
