"""Authoritative persistence/restore contract for school-core durable state.

This module defines ONE contract for all durable stores that must survive
fresh checkouts:

  - compound_learning.json   — CompoundLearningStore (bounded post-bead loop)
  - state.sqlite3           — StateJournal (approval CAS + operation journal)
  - candidate_bindings.json  — CandidateBindingStore (verification/approval posture)
  - data/recovery/          — recovery smoke evidence tree
  - data/trajectories/      — trajectory corpus (Layer 2 memory)

The contract specifies:
  1. Canonical paths for each store
  2. Which stores are checkpointed (committed to git)
  3. Which stores are seeded (restored from git)
  4. Allowlisted sanitization applied before checkpoint
  5. Validation that consumers can read what producers wrote

Sanitization rules (allowlisted):
  - Preserve required audit identity: bead_id, candidate_id, head_sha, actor,
    scope, approval_id, operation_id, issue_number, repository, branch
  - Redact credentials: api_key, access_token, refresh_token, authorization,
    auth, secret, password, credential, private_key
  - Redact home paths: /Users/<name>/... → ~, /home/<name>/... → ~
  - Redact tokens: sk-..., gh[pousr]_..., bearer ...

This module is the single source of truth for the school-loop checkpoint
and seed steps. The workflow file (.github/workflows/school-loop.yml) and
the recovery documentation (docs/setup/recovery.md) both reference this
contract.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

# ── Canonical store paths ─────────────────────────────────────────────────────

# These are the canonical paths relative to the repo root. All producers
# write to these paths; all consumers read from these paths. The school-loop
# checkpoint and seed steps use these exact paths.

COMPOUND_LEARNING_PATH = Path("data") / "compound_learning.json"
STATE_JOURNAL_PATH = Path("data") / "state.sqlite3"
CANDIDATE_BINDINGS_PATH = Path("data") / "candidate_bindings.json"
RECOVERY_EVIDENCE_PATH = Path("data") / "recovery"
TRAJECTORIES_PATH = Path("data") / "trajectories"

# ── Checkpoint and seed path lists ────────────────────────────────────────────

# These are the exact paths that the school-loop checkpoint step stages
# and the seed step restores. They MUST match the workflow file.

CHECKPOINT_PATHS: tuple[str, ...] = (
    # Core bridge state (already checkpointed)
    "data/last_run.json",
    "data/issues_cache.json",
    "data/processed_issues.json",
    "data/scores.json",
    "data/retry_issues.json",
    "data/crew_runs.json",
    "data/grading_queue.jsonl",
    # Compound learning (NEW — was missing)
    "data/compound_learning.json",
    # Candidate bindings (NEW — was missing)
    "data/candidate_bindings.json",
    # State journal (NEW — was missing)
    "data/state.sqlite3",
    # Recovery evidence tree (NEW — was missing)
    "data/recovery/",
    # Trajectories (already checkpointed via find)
    "data/trajectories/",
)

SEED_PATHS: tuple[str, ...] = (
    # Core bridge state (already seeded)
    "data/retry_issues.json",
    "data/processed_issues.json",
    "data/last_run.json",
    "data/grading_queue.jsonl",
    # Compound learning (NEW — was missing)
    "data/compound_learning.json",
    # Candidate bindings (NEW — was missing)
    "data/candidate_bindings.json",
    # State journal (NEW — was missing)
    "data/state.sqlite3",
    # Recovery evidence tree (NEW — was missing)
    "data/recovery/",
    # Trajectories (NEW — was missing from seed)
    "data/trajectories/",
)

# ── Allowlisted sanitization ─────────────────────────────────────────────────

# These patterns are used to redact unsafe content from durable stores
# before they are checkpointed. They are the SAME patterns used by
# scripts/sanitize_data.py — this module reuses them for consistency.

_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+")
_REPO_PREFIX_RE = re.compile(r"(?:/[A-Za-z0-9_.~-]+)+/school-core/data/")
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{12,})\b"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|auth|secret|password|credential|private[_-]?key)"
)

# Fields that MUST be preserved for audit identity. These are never redacted.
_AUDIT_IDENTITY_FIELDS: frozenset[str] = frozenset({
    "bead_id",
    "candidate_id",
    "head_sha",
    "base_sha",
    "diff_digest",
    "actor",
    "scope",
    "approval_id",
    "operation_id",
    "idempotency_key",
    "issue_number",
    "repository",
    "branch",
    "base_ref",
    "candidate_kind",
    "worktree",
    "owner",
    "created_at",
    "observed_at",
    "issued_at",
    "bound_at",
    "timestamp",
    "schema_version",
    "observation_id",
    "trigger",
    "phase",
    "stop_reason",
    "state",
    "status",
    "kind",
    "disposition",
    "domain",
    "difficulty",
    "agent",
    "student",
    "lens",
    "cto_verdict",
    "coo_verdict",
    "accepted",
    "cto_score",
    "coo_score",
    "has_critical",
    "parse_failed",
    "summary",
    "output",
    "task",
    "prompt",
    "response",
    "system_prompt",
    "evaluation",
    "error",
    "findings",
    "ac_met",
    "files_changed",
    "blockers",
    "verification",
    "change_id",
    "reason",
    "target",
    "evidence",
    "independent",
    "accepted",
    "promotion",
    "eligible",
    "validated_repetitions",
    "events",
    "result",
    "observed_at",
})


def _scrub_text(value: str) -> str:
    """Scrub a text value: redact home paths, repo prefixes, and tokens."""
    value = _REPO_PREFIX_RE.sub("data/", value)
    value = _HOME_PATH_RE.sub("~", value)
    value = _TOKEN_RE.sub("[REDACTED]", value)
    return value


def _scrub_value(value: Any) -> Any:
    """Recursively scrub a JSON-like value, preserving audit identity."""
    if isinstance(value, dict):
        result = {}
        for key, val in value.items():
            key_str = str(key).strip()
            if _SENSITIVE_KEY_RE.match(key_str):
                result[key] = "[REDACTED]"
            elif key_str in _AUDIT_IDENTITY_FIELDS:
                # Preserve audit identity fields as-is (but still scrub strings)
                if isinstance(val, str):
                    result[key] = _scrub_text(val)
                else:
                    result[key] = val
            else:
                result[key] = _scrub_value(val)
        return result
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def sanitize_store(path: str | Path) -> int:
    """Sanitize one store file in place. Returns replacement count.

    Applies allowlisted sanitization:
    - Redact sensitive keys (credentials, tokens)
    - Redact home paths and repo prefixes
    - Redact token-shaped strings
    - Preserve audit identity fields

    Returns the number of sensitive/path matches scrubbed.
    """
    path = Path(path)
    if not path.exists():
        return 0

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0

    hits_before = (
        len(_HOME_PATH_RE.findall(raw))
        + len(_REPO_PREFIX_RE.findall(raw))
        + len(_TOKEN_RE.findall(raw))
    )

    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml
        try:
            data = yaml.safe_load(raw)
            data = _scrub_value(data)
            path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        except (yaml.YAMLError, TypeError):
            pass
    elif path.suffix.lower() == ".json":
        try:
            data = json.loads(raw)
            data = _scrub_value(data)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except (json.JSONDecodeError, TypeError):
            pass
    elif path.suffix.lower() == ".sqlite3":
        # SQLite stores are sanitized by scrubbing the result_json column
        # in operation_events and any text columns in approvals/operations.
        # This is done in-place by the caller (see sanitize_state_journal).
        pass
    else:
        # Fallback: line-level scrubbing
        cleaned = _scrub_text(raw)
        if cleaned != raw:
            path.write_text(cleaned)

    return hits_before


def sanitize_state_journal(path: str | Path) -> int:
    """Sanitize a SQLite state journal in place.

    Scrubs the result_json column in operation_events and any text columns
    in approvals/operations that may contain sensitive data.

    Returns the number of rows scrubbed.
    """
    path = Path(path)
    if not path.exists():
        return 0

    scrubbed = 0
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Scrub operation_events.result_json
        rows = cursor.execute(
            "SELECT event_id, result_json FROM operation_events"
        ).fetchall()
        for row in rows:
            event_id = row["event_id"]
            result_json = row["result_json"]
            if not result_json:
                continue
            try:
                data = json.loads(result_json)
                scrubbed_data = _scrub_value(data)
                new_json = json.dumps(scrubbed_data, sort_keys=True, separators=(",", ":"))
                if new_json != result_json:
                    cursor.execute(
                        "UPDATE operation_events SET result_json = ? WHERE event_id = ?",
                        (new_json, event_id),
                    )
                    scrubbed += 1
            except (json.JSONDecodeError, TypeError):
                pass

        # Scrub approvals text columns (actor, scope are audit identity — preserve)
        # Only scrub if they contain sensitive patterns
        rows = cursor.execute(
            "SELECT approval_id, actor, scope FROM approvals"
        ).fetchall()
        for row in rows:
            approval_id = row["approval_id"]
            actor = row["actor"] or ""
            scope = row["scope"] or ""
            new_actor = _scrub_text(actor)
            new_scope = _scrub_text(scope)
            if new_actor != actor or new_scope != scope:
                cursor.execute(
                    "UPDATE approvals SET actor = ?, scope = ? WHERE approval_id = ?",
                    (new_actor, new_scope, approval_id),
                )
                scrubbed += 1

        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass

    return scrubbed


def sanitize_all_stores(data_dir: str | Path) -> dict[str, int]:
    """Sanitize all durable stores under data_dir.

    Returns a mapping of store path to replacement count.
    """
    data_dir = Path(data_dir)
    results: dict[str, int] = {}

    # Compound learning
    path = data_dir / COMPOUND_LEARNING_PATH
    if path.exists():
        results[str(COMPOUND_LEARNING_PATH)] = sanitize_store(path)

    # Candidate bindings
    path = data_dir / CANDIDATE_BINDINGS_PATH
    if path.exists():
        results[str(CANDIDATE_BINDINGS_PATH)] = sanitize_store(path)

    # State journal
    path = data_dir / STATE_JOURNAL_PATH
    if path.exists():
        results[str(STATE_JOURNAL_PATH)] = sanitize_state_journal(path)

    # Recovery evidence tree
    recovery_dir = data_dir / RECOVERY_EVIDENCE_PATH
    if recovery_dir.exists():
        for json_file in recovery_dir.rglob("*.json"):
            results[str(json_file.relative_to(data_dir))] = sanitize_store(json_file)
        for sqlite_file in recovery_dir.rglob("*.sqlite3"):
            results[str(sqlite_file.relative_to(data_dir))] = sanitize_state_journal(sqlite_file)

    # Trajectories
    traj_dir = data_dir / TRAJECTORIES_PATH
    if traj_dir.exists():
        for json_file in traj_dir.glob("*.json"):
            results[str(json_file.relative_to(data_dir))] = sanitize_store(json_file)

    return results


def validate_contract(data_dir: str | Path) -> list[str]:
    """Validate that all consumers can read what producers wrote.

    Returns a list of validation errors (empty if all valid).
    """
    data_dir = Path(data_dir)
    errors: list[str] = []

    # Compound learning
    path = data_dir / "compound_learning.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                errors.append(f"{COMPOUND_LEARNING_PATH}: expected list, got {type(data).__name__}")
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{COMPOUND_LEARNING_PATH}: {exc}")

    # Candidate bindings
    path = data_dir / "candidate_bindings.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append(f"{CANDIDATE_BINDINGS_PATH}: expected dict, got {type(data).__name__}")
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"{CANDIDATE_BINDINGS_PATH}: {exc}")

    # State journal
    path = data_dir / "state.sqlite3"
    if path.exists():
        try:
            conn = sqlite3.connect(str(path), timeout=10)
            conn.execute("PRAGMA quick_check")
            conn.close()
        except sqlite3.Error as exc:
            errors.append(f"{STATE_JOURNAL_PATH}: {exc}")

    # Recovery evidence tree
    recovery_dir = data_dir / "recovery"
    if recovery_dir.exists():
        for json_file in recovery_dir.rglob("*.json"):
            try:
                json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                errors.append(f"{json_file.relative_to(data_dir)}: {exc}")
        for sqlite_file in recovery_dir.rglob("*.sqlite3"):
            try:
                conn = sqlite3.connect(str(sqlite_file), timeout=10)
                conn.execute("PRAGMA quick_check")
                conn.close()
            except sqlite3.Error as exc:
                errors.append(f"{sqlite_file.relative_to(data_dir)}: {exc}")

    # Trajectories
    traj_dir = data_dir / "trajectories"
    if traj_dir.exists():
        for json_file in traj_dir.glob("*.json"):
            try:
                json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                errors.append(f"{json_file.relative_to(data_dir)}: {exc}")

    return errors


def checkpoint_stores(data_dir: str | Path) -> list[str]:
    """Sanitize and return the list of store paths to checkpoint.

    This is the single source of truth for the school-loop checkpoint step.
    The workflow file should call this function (or use CHECKPOINT_PATHS)
    to determine which files to stage.
    """
    sanitize_all_stores(data_dir)
    return list(CHECKPOINT_PATHS)


def seed_stores(data_dir: str | Path) -> list[str]:
    """Return the list of store paths to seed from the checkpoint branch.

    This is the single source of truth for the school-loop seed step.
    The workflow file should call this function (or use SEED_PATHS)
    to determine which files to restore.
    """
    return list(SEED_PATHS)


__all__ = [
    "AUDIT_IDENTITY_FIELDS",
    "CANDIDATE_BINDINGS_PATH",
    "CHECKPOINT_PATHS",
    "COMPOUND_LEARNING_PATH",
    "RECOVERY_EVIDENCE_PATH",
    "SEED_PATHS",
    "STATE_JOURNAL_PATH",
    "TRAJECTORIES_PATH",
    "checkpoint_stores",
    "sanitize_all_stores",
    "sanitize_state_journal",
    "sanitize_store",
    "seed_stores",
    "validate_contract",
]