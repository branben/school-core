"""Candidate-bound local verification evidence."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from candidate_manifest import CandidateManifest, CandidateStore


class GateEvidenceError(ValueError):
    """Local evidence cannot be safely bound to the candidate."""


@dataclass(frozen=True)
class GateEvidence:
    candidate_id: str
    head_sha: str
    disposition: str
    checks: tuple[dict[str, Any], ...]
    toolchain: dict[str, str]


Runner = Callable[..., dict[str, Any]]


def _bounded(value: Any, limit: int = 4096) -> str:
    text = str(value or "")
    return text[:limit]


def run_candidate_gate(
    *, repo_path: str | Path, store: CandidateStore, manifest: CandidateManifest,
    runner: Runner, commands: list[dict[str, Any]],
    project_verify: Path | None = None, flake_path: Path | None = None,
    timeout: int = 300,
) -> GateEvidence:
    try:
        stored = store.get(manifest.candidate_id)
    except Exception as exc:
        raise GateEvidenceError(f"candidate_id is not durable: {exc}") from exc
    if stored != manifest:
        raise GateEvidenceError("candidate_id is not bound to the supplied manifest")
    if store.branch_owner(manifest.branch) != manifest.candidate_id:
        raise GateEvidenceError("candidate does not own its branch")
    validation = store.validate(repo_path, manifest)
    if validation.status != "current":
        raise GateEvidenceError(f"candidate is {validation.status}: {validation.reason}")

    result = runner(
        Path(repo_path), project_verify=project_verify, flake_path=flake_path, timeout=timeout,
    )
    if not isinstance(result, dict):
        raise GateEvidenceError("gate runner returned an invalid result")
    failures = result.get("failures") or []
    if not isinstance(failures, list):
        raise GateEvidenceError("gate failures must be a list")
    checks: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        failure = next(
            (item for item in failures if isinstance(item, dict) and item.get("cmd") == command.get("cmd")),
            None,
        )
        checks.append({
            "name": command.get("name", command.get("cmd", f"check-{index}")),
            "command": command.get("cmd", ""),
            "cwd": command.get("cwd", "."),
            "exit": failure.get("exit") if failure else 0,
            "output": _bounded(failure.get("stderr", "") if failure else ""),
        })
    if result.get("skipped"):
        disposition = "skipped"
    elif result.get("passed") is True and not failures:
        disposition = "current"
    else:
        disposition = "failed"
    return GateEvidence(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        disposition=disposition,
        checks=tuple(checks),
        toolchain={
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "runner": getattr(runner, "__name__", type(runner).__name__),
        },
    )


def evidence_is_current(
    evidence: GateEvidence, manifest: CandidateManifest, *, repo_path: str | Path, store: CandidateStore,
) -> bool:
    if evidence.candidate_id != manifest.candidate_id or evidence.head_sha != manifest.head_sha:
        return False
    if evidence.disposition != "current" or store.get(manifest.candidate_id) != manifest:
        return False
    return store.validate(repo_path, manifest).status == "current"


__all__ = ["GateEvidence", "GateEvidenceError", "evidence_is_current", "run_candidate_gate"]
