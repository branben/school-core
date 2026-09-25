"""Transactional local state for the School Core merge spine.

SQLite provides the compare-and-set boundary required by the merge contract.
Each operation opens its own connection and uses BEGIN IMMEDIATE for writes,
so approval consumption and its operation record commit atomically.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_RESULT = 4096


class StateJournalError(RuntimeError):
    """Base class for fail-closed state journal errors."""


class InvalidStateError(StateJournalError, ValueError):
    """Input or persisted state is invalid."""


class ConflictError(StateJournalError):
    """A state transition lost its compare-and-set race."""


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    candidate_id: str
    head_sha: str
    actor: str
    scope: str
    state: str
    issued_at: str
    operation_id: str | None = None


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    idempotency_key: str
    candidate_id: str
    head_sha: str
    kind: str
    status: str
    events: tuple[dict[str, Any], ...] = ()


class StateJournal:
    """SQLite-backed approval CAS and append-only operation journal."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('issued', 'consumed', 'revoked', 'expired')),
                    issued_at TEXT NOT NULL,
                    operation_id TEXT
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    candidate_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    kind TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operation_events_operation
                    ON operation_events(operation_id, event_id);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_sha(value: str, field: str) -> str:
        if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
            raise InvalidStateError(f"{field} must be a full lowercase Git SHA")
        return value

    @staticmethod
    def _require_text(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidStateError(f"{field} must not be blank")
        return value.strip()

    def issue_approval(
        self, *, approval_id: str, candidate_id: str, head_sha: str,
        actor: str, scope: str,
    ) -> ApprovalRecord:
        approval_id = self._require_text(approval_id, "approval_id")
        candidate_id = self._require_text(candidate_id, "candidate_id")
        head_sha = self._validate_sha(head_sha, "head_sha")
        actor = self._require_text(actor, "actor")
        scope = self._require_text(scope, "scope")
        record = ApprovalRecord(
            approval_id=approval_id, candidate_id=candidate_id, head_sha=head_sha,
            actor=actor, scope=scope, state="issued", issued_at=self._now(),
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO approvals (approval_id, candidate_id, head_sha, actor, scope, state, issued_at) "
                    "VALUES (?, ?, ?, ?, ?, 'issued', ?)",
                    (approval_id, candidate_id, head_sha, actor, scope, record.issued_at),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ConflictError(f"approval already exists: {approval_id}") from exc
        return record

    def approval(self, approval_id: str) -> ApprovalRecord:
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None:
            raise InvalidStateError(f"unknown approval: {approval_id}")
        return ApprovalRecord(
            approval_id=row["approval_id"], candidate_id=row["candidate_id"],
            head_sha=row["head_sha"], actor=row["actor"], scope=row["scope"],
            state=row["state"], issued_at=row["issued_at"], operation_id=row["operation_id"],
        )

    def consume_approval(
        self, *, approval_id: str, candidate_id: str, head_sha: str,
        operation_id: str, idempotency_key: str,
    ) -> OperationRecord:
        operation_id = self._require_text(operation_id, "operation_id")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
                if row is None:
                    raise InvalidStateError(f"unknown approval: {approval_id}")
                if row["candidate_id"] != candidate_id:
                    raise InvalidStateError("candidate_id does not match approval")
                if row["head_sha"] != head_sha:
                    raise InvalidStateError("head_sha does not match approval")
                if row["state"] == "consumed":
                    if row["operation_id"] != operation_id:
                        raise ConflictError("approval already consumed by another operation")
                    db.commit()
                    return self._operation_in_connection(db, operation_id)
                if row["state"] != "issued":
                    raise ConflictError(f"approval is {row['state']}")
                existing = db.execute(
                    "SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None and existing["operation_id"] != operation_id:
                    raise ConflictError("idempotency key belongs to another operation")
                if existing is None:
                    db.execute(
                        "INSERT INTO operations (operation_id, idempotency_key, candidate_id, head_sha, kind, status, created_at) "
                        "VALUES (?, ?, ?, ?, 'consume_approval', 'confirmed', ?)",
                        (operation_id, idempotency_key, candidate_id, head_sha, self._now()),
                    )
                db.execute(
                    "UPDATE approvals SET state = 'consumed', operation_id = ? WHERE approval_id = ? AND state = 'issued'",
                    (operation_id, approval_id),
                )
                db.commit()
                return self._operation_in_connection(db, operation_id)
            except Exception:
                db.rollback()
                raise

    def start_operation(
        self, *, operation_id: str, idempotency_key: str, candidate_id: str,
        head_sha: str, kind: str,
    ) -> OperationRecord:
        operation_id = self._require_text(operation_id, "operation_id")
        idempotency_key = self._require_text(idempotency_key, "idempotency_key")
        candidate_id = self._require_text(candidate_id, "candidate_id")
        head_sha = self._validate_sha(head_sha, "head_sha")
        kind = self._require_text(kind, "kind")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                if row is not None:
                    db.commit()
                    return self._operation_in_connection(db, row["operation_id"])
                db.execute(
                    "INSERT INTO operations (operation_id, idempotency_key, candidate_id, head_sha, kind, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'started', ?)",
                    (operation_id, idempotency_key, candidate_id, head_sha, kind, self._now()),
                )
                db.commit()
                return self._operation_in_connection(db, operation_id)
            except Exception:
                db.rollback()
                raise

    def record_event(self, operation_id: str, kind: str, result: dict[str, Any] | None = None) -> None:
        kind = self._require_text(kind, "kind")
        payload = json.dumps(result or {}, sort_keys=True, separators=(",", ":"))[:_MAX_RESULT]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO operation_events (operation_id, kind, result_json, observed_at) VALUES (?, ?, ?, ?)",
                    (operation_id, kind, payload, self._now()),
                )
                status_by_kind = {"confirmed": "confirmed", "failed": "failed", "closed": "closed"}
                status = status_by_kind.get(kind, "started")
                db.execute("UPDATE operations SET status = ? WHERE operation_id = ?", (status, operation_id))
                db.commit()
            except Exception:
                db.rollback()
                raise

    def operation(self, operation_id: str) -> OperationRecord:
        with self._connect() as db:
            return self._operation_in_connection(db, operation_id)

    def operations(self) -> list[OperationRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT operation_id FROM operations ORDER BY created_at, operation_id").fetchall()
            return [self._operation_in_connection(db, row["operation_id"]) for row in rows]

    @staticmethod
    def _operation_in_connection(db: sqlite3.Connection, operation_id: str) -> OperationRecord:
        row = db.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            raise InvalidStateError(f"unknown operation: {operation_id}")
        events = db.execute(
            "SELECT kind, result_json, observed_at FROM operation_events WHERE operation_id = ? ORDER BY event_id",
            (operation_id,),
        ).fetchall()
        return OperationRecord(
            operation_id=row["operation_id"], idempotency_key=row["idempotency_key"],
            candidate_id=row["candidate_id"], head_sha=row["head_sha"], kind=row["kind"],
            status=row["status"],
            events=tuple({"kind": e["kind"], "result": json.loads(e["result_json"]), "observed_at": e["observed_at"]} for e in events),
        )


__all__ = [
    "ApprovalRecord", "ConflictError", "InvalidStateError", "OperationRecord",
    "StateJournal", "StateJournalError",
]
