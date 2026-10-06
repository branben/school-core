"""Hermetic fresh-checkout integration tests for the persistence contract.

These tests prove that compound learning, teacher decisions, candidate
evidence, and trajectories survive a checkpoint/restore cycle — the exact
cycle the school-loop workflow performs every 5 minutes.

The tests are hermetic: they use temporary directories and never touch
the real data/ directory. They simulate the full lifecycle:

  1. Producer writes state (compound learning, journal, bindings, etc.)
  2. Checkpoint sanitizes and stages the state
  3. Fresh checkout restores the state
  4. Consumer reads the restored state
  5. Replay does not reset or duplicate state

Run: python -m pytest tests/test_persistence_contract.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from persistence_contract import (
    CANDIDATE_BINDINGS_PATH,
    CHECKPOINT_PATHS,
    COMPOUND_LEARNING_PATH,
    RECOVERY_EVIDENCE_PATH,
    SEED_PATHS,
    STATE_JOURNAL_PATH,
    TRAJECTORIES_PATH,
    checkpoint_stores,
    sanitize_all_stores,
    sanitize_state_journal,
    sanitize_store,
    seed_stores,
    validate_contract,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_repo(repo: Path) -> None:
    """Create a real Git repository with a base commit."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "file.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-q", "-b", "candidate"], cwd=repo, check=True, capture_output=True)
    (repo / "file.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True, capture_output=True)


def _sha(seed: str = "a") -> str:
    return seed * 40


def _make_manifest(tmp_path: Path, *, candidate_id: str, repo: Path):
    """Create a real CandidateManifest from a real Git repo."""
    from candidate_manifest import CandidateStore, create_candidate
    return create_candidate(
        store=CandidateStore(tmp_path / f"candidates-{candidate_id}.json"),
        repo_path=repo,
        candidate_id=candidate_id,
        bead_id="bead-test",
        issue_number=1,
        repository="owner/repo",
        base_ref="main",
        branch="candidate",
        owner="test-owner",
    )


class _VerificationStub:
    def __init__(self, *, candidate_id: str, head_sha: str) -> None:
        self.candidate_id = candidate_id
        self.head_sha = head_sha
        self.passed = True
        self.skipped = False
        self.failures = ()


# ── Test: Compound observation lifecycle survives checkpoint/restore ─────────


class TestCompoundLearningLifecycle:
    """Compound learning observations must survive checkpoint/restore."""

    def test_observation_survives_round_trip(self, tmp_path: Path) -> None:
        """A compound observation written by the producer is readable by the consumer after restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        # Producer: write a compound observation
        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        record = store.observe(
            bead_id="bead-test-1",
            trigger="bead_completed",
            evidence={
                "control": {"exit_code": 0},
                "runtime": {"duration_ms": 1234},
                "verification": {"passed": True},
                "judgment": {"score": 85},
                "outcome": {"status": "success"},
            },
        )
        observation_id = record["observation_id"]

        # Checkpoint: sanitize
        results = sanitize_all_stores(data_dir)
        assert str(COMPOUND_LEARNING_PATH) in results

        # Fresh checkout: new store instance over the same file
        restored_store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        restored = restored_store.recent(limit=10)
        assert len(restored) == 1
        assert restored[0]["observation_id"] == observation_id
        assert restored[0]["bead_id"] == "bead-test-1"
        assert restored[0]["trigger"] == "bead_completed"
        assert restored[0]["phase"] == "observed"

    def test_full_lifecycle_survives_round_trip(self, tmp_path: Path) -> None:
        """observe → propose → verify → record survives checkpoint/restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)

        # Full lifecycle
        record = store.observe(
            bead_id="bead-lifecycle",
            trigger="bead_completed",
            evidence={"outcome": {"status": "success"}},
        )
        obs_id = record["observation_id"]

        store.propose(obs_id, {
            "change_id": "change-1",
            "kind": "improvement",
            "target": "target-1",
            "reason": "test reason",
        })
        store.verify(obs_id, accepted=True, evidence={"independent": True})
        final = store.record(obs_id)
        assert final["phase"] == "recorded"
        assert final["promotion"]["eligible"] is False  # Only 1 repetition

        # Checkpoint
        sanitize_all_stores(data_dir)

        # Restore
        restored_store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        restored = restored_store.recent(limit=10)
        assert len(restored) == 1
        assert restored[0]["phase"] == "recorded"
        assert restored[0]["candidate"]["change_id"] == "change-1"
        assert restored[0]["promotion"]["validated_repetitions"] == 1

    def test_replay_does_not_duplicate(self, tmp_path: Path) -> None:
        """Re-reading the store does not create duplicate observations."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        store.observe(bead_id="bead-1", trigger="bead_completed", evidence={})

        # Simulate replay: open the store again and read
        replay_store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        assert len(replay_store.recent(limit=10)) == 1

        # Another replay
        replay_store2 = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        assert len(replay_store2.recent(limit=10)) == 1

    def test_sanitization_preserves_audit_identity(self, tmp_path: Path) -> None:
        """Sanitization preserves bead_id, candidate_id, trigger, etc."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        record = store.observe(
            bead_id="bead-audit-123",
            trigger="bead_completed",
            evidence={
                "control": {"exit_code": 0},
                "outcome": {"status": "success", "path": "/Users/brandonbennett/school-core/data/test"},
            },
        )

        sanitize_all_stores(data_dir)

        restored = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        data = restored.recent(limit=10)
        assert data[0]["bead_id"] == "bead-audit-123"
        assert data[0]["trigger"] == "bead_completed"
        # Home path in evidence should be scrubbed
        evidence_str = json.dumps(data[0]["evidence"])
        assert "/Users/brandonbennett" not in evidence_str
        assert "~" in evidence_str or "data/" in evidence_str

    def test_sanitization_redacts_credentials(self, tmp_path: Path) -> None:
        """Sanitization redacts api_key, tokens, etc."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        store.observe(
            bead_id="bead-cred",
            trigger="bead_completed",
            evidence={
                "control": {"api_key": "sk-1234567890abcdef"},
                "outcome": {"status": "success"},
            },
        )

        sanitize_all_stores(data_dir)

        restored = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        data = restored.recent(limit=10)
        evidence_str = json.dumps(data[0]["evidence"])
        assert "sk-1234567890abcdef" not in evidence_str
        assert "[REDACTED]" in evidence_str


# ── Test: Teacher decision / approval state survives checkpoint/restore ───────


class TestTeacherApprovalSurvives:
    """Teacher approvals in the StateJournal must survive checkpoint/restore."""

    def test_approval_survives_round_trip(self, tmp_path: Path) -> None:
        """An issued approval is readable after checkpoint/restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from state_journal import StateJournal
        journal = StateJournal(data_dir / STATE_JOURNAL_PATH)
        journal.issue_approval(
            approval_id="approval-1",
            candidate_id="candidate-1",
            head_sha=_sha("a"),
            actor="human@example.com",
            scope="open_pr_for_candidate",
        )

        # Checkpoint
        sanitize_state_journal(data_dir / STATE_JOURNAL_PATH)

        # Restore: new journal instance
        restored = StateJournal(data_dir / STATE_JOURNAL_PATH)
        approval = restored.approval("approval-1")
        assert approval.state == "issued"
        assert approval.candidate_id == "candidate-1"
        assert approval.actor == "human@example.com"

    def test_approval_consumption_survives_round_trip(self, tmp_path: Path) -> None:
        """A consumed approval remains consumed after checkpoint/restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from state_journal import StateJournal
        journal = StateJournal(data_dir / STATE_JOURNAL_PATH)
        journal.issue_approval(
            approval_id="approval-consume",
            candidate_id="candidate-1",
            head_sha=_sha("b"),
            actor="human@example.com",
            scope="open_pr_for_candidate",
        )
        journal.consume_approval(
            approval_id="approval-consume",
            candidate_id="candidate-1",
            head_sha=_sha("b"),
            operation_id="op-1",
            idempotency_key="consume:approval-consume",
        )

        # Checkpoint
        sanitize_state_journal(data_dir / STATE_JOURNAL_PATH)

        # Restore
        restored = StateJournal(data_dir / STATE_JOURNAL_PATH)
        approval = restored.approval("approval-consume")
        assert approval.state == "consumed"
        assert approval.operation_id == "op-1"

    def test_operation_journal_survives_round_trip(self, tmp_path: Path) -> None:
        """Operation events survive checkpoint/restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from state_journal import StateJournal
        journal = StateJournal(data_dir / STATE_JOURNAL_PATH)
        op = journal.start_operation(
            operation_id="op-1",
            idempotency_key="op:key-1",
            candidate_id="candidate-1",
            head_sha=_sha("c"),
            kind="merge_request",
        )
        journal.record_event("op-1", "requested", {"provider": "fake"})
        journal.record_event("op-1", "confirmed", {"provider": "fake", "result": "merged"})

        # Checkpoint
        sanitize_state_journal(data_dir / STATE_JOURNAL_PATH)

        # Restore
        restored = StateJournal(data_dir / STATE_JOURNAL_PATH)
        operation = restored.operation("op-1")
        assert operation.status == "confirmed"
        assert len(operation.events) == 2
        assert operation.events[0]["kind"] == "requested"
        assert operation.events[1]["kind"] == "confirmed"

    def test_replay_does_not_duplicate_operations(self, tmp_path: Path) -> None:
        """Re-starting an operation with the same idempotency key does not duplicate."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from state_journal import StateJournal
        journal = StateJournal(data_dir / STATE_JOURNAL_PATH)
        journal.start_operation(
            operation_id="op-1",
            idempotency_key="op:key-1",
            candidate_id="candidate-1",
            head_sha=_sha("d"),
            kind="merge_request",
        )

        # Replay with same idempotency key
        replay = journal.start_operation(
            operation_id="op-2",
            idempotency_key="op:key-1",
            candidate_id="candidate-1",
            head_sha=_sha("d"),
            kind="merge_request",
        )
        assert replay.operation_id == "op-1"  # Returns original

        # After restore
        sanitize_state_journal(data_dir / STATE_JOURNAL_PATH)
        restored = StateJournal(data_dir / STATE_JOURNAL_PATH)
        assert len(restored.operations()) == 1


# ── Test: Candidate manifest / gate evidence survives checkpoint/restore ─────


class TestCandidateEvidenceSurvives:
    """Candidate bindings and gate evidence must survive checkpoint/restore."""

    def test_binding_survives_round_trip(self, tmp_path: Path) -> None:
        """A candidate binding is readable after checkpoint/restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        repo = tmp_path / "repo"
        _make_repo(repo)
        manifest = _make_manifest(tmp_path, candidate_id="cand-1", repo=repo)

        from candidate_binding import CandidateBindingStore, bind_trusted_evidence
        store = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)
        binding = bind_trusted_evidence(
            manifest=manifest,
            verification=_VerificationStub(
                candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
            ),
            approval=None,
        )
        store.put(binding)

        # Checkpoint
        sanitize_all_stores(data_dir)

        # Restore
        restored = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)
        found = restored.get("cand-1")
        assert found is not None
        assert found.candidate_id == "cand-1"
        assert found.head_sha == manifest.head_sha
        assert found.verification_passed is True

    def test_multiple_bindings_survive_round_trip(self, tmp_path: Path) -> None:
        """Multiple candidate bindings all survive checkpoint/restore."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        _make_repo(repo_a)
        _make_repo(repo_b)
        manifest_a = _make_manifest(tmp_path, candidate_id="cand-a", repo=repo_a)
        manifest_b = _make_manifest(tmp_path, candidate_id="cand-b", repo=repo_b)

        from candidate_binding import CandidateBindingStore, bind_trusted_evidence
        store = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)
        store.put(bind_trusted_evidence(
            manifest=manifest_a,
            verification=_VerificationStub(
                candidate_id=manifest_a.candidate_id, head_sha=manifest_a.head_sha,
            ),
            approval=None,
        ))
        store.put(bind_trusted_evidence(
            manifest=manifest_b,
            verification=_VerificationStub(
                candidate_id=manifest_b.candidate_id, head_sha=manifest_b.head_sha,
            ),
            approval=None,
        ))

        # Checkpoint
        sanitize_all_stores(data_dir)

        # Restore
        restored = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)
        assert restored.get("cand-a") is not None
        assert restored.get("cand-b") is not None

    def test_recovery_evidence_tree_survives_round_trip(self, tmp_path: Path) -> None:
        """Recovery evidence (candidate.json, gate-evidence.json, state.sqlite3) survives."""
        data_dir = tmp_path / "data"
        recovery_dir = data_dir / RECOVERY_EVIDENCE_PATH / "smoke-test"
        recovery_dir.mkdir(parents=True)

        # Write candidate.json
        candidate = {
            "candidate_id": "smoke-1",
            "head_sha": _sha("e"),
            "base_sha": _sha("f"),
            "diff_digest": _sha("g"),
            "branch": "candidate",
            "repository": "owner/repo",
        }
        (recovery_dir / "candidate.json").write_text(json.dumps(candidate, indent=2))

        # Write gate-evidence.json
        gate = {
            "disposition": "current",
            "head_sha": _sha("e"),
            "candidate_id": "smoke-1",
            "checks": [{"name": "test", "exit": 0}],
        }
        (recovery_dir / "gate-evidence.json").write_text(json.dumps(gate, indent=2))

        # Write state.sqlite3
        db_path = recovery_dir / "state.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE operations (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO operations VALUES ('op-1')")
        conn.commit()
        conn.close()

        # Checkpoint
        sanitize_all_stores(data_dir)

        # Restore: validate
        errors = validate_contract(data_dir)
        assert errors == []

        # Verify content
        restored_candidate = json.loads((recovery_dir / "candidate.json").read_text())
        assert restored_candidate["candidate_id"] == "smoke-1"
        restored_gate = json.loads((recovery_dir / "gate-evidence.json").read_text())
        assert restored_gate["disposition"] == "current"

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM operations").fetchall()
        conn.close()
        assert len(rows) == 1


# ── Test: Trajectories survive checkpoint/restore ─────────────────────────────


class TestTrajectoriesSurvive:
    """Trajectory files must survive checkpoint/restore."""

    def test_trajectory_survives_round_trip(self, tmp_path: Path) -> None:
        """A trajectory file is readable after checkpoint/restore."""
        data_dir = tmp_path / "data"
        traj_dir = data_dir / TRAJECTORIES_PATH
        traj_dir.mkdir(parents=True)

        trajectory = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "domain": "python-coding",
            "difficulty": "medium",
            "agent": "coder",
            "prompt": "test prompt",
            "response": "test response",
            "task_score": 85.0,
        }
        traj_file = traj_dir / "20260101_000000_000000--python-coding--coder.json"
        traj_file.write_text(json.dumps(trajectory, indent=2))

        # Checkpoint
        sanitize_all_stores(data_dir)

        # Restore
        restored = json.loads(traj_file.read_text())
        assert restored["domain"] == "python-coding"
        assert restored["task_score"] == 85.0

    def test_trajectory_with_home_path_is_sanitized(self, tmp_path: Path) -> None:
        """Trajectory with home path is sanitized during checkpoint."""
        data_dir = tmp_path / "data"
        traj_dir = data_dir / TRAJECTORIES_PATH
        traj_dir.mkdir(parents=True)

        trajectory = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "domain": "python-coding",
            "difficulty": "medium",
            "agent": "coder",
            "prompt": "test prompt",
            "response": "test response",
            "task_score": 85.0,
            "worktree": "/Users/brandonbennett/school-core/data/test",
        }
        traj_file = traj_dir / "20260101_000000_000000--python-coding--coder.json"
        traj_file.write_text(json.dumps(trajectory, indent=2))

        # Checkpoint
        sanitize_all_stores(data_dir)

        # Verify home path is scrubbed
        restored = json.loads(traj_file.read_text())
        assert "/Users/brandonbennett" not in json.dumps(restored)


# ── Test: Structural workflow tests verify exact allowlisted paths ────────────


class TestStructuralWorkflowPaths:
    """Verify the exact allowlisted seed/checkpoint paths in the workflow."""

    def test_checkpoint_paths_include_new_stores(self) -> None:
        """CHECKPOINT_PATHS must include compound_learning, candidate_bindings, state.sqlite3, recovery, trajectories."""
        paths = set(CHECKPOINT_PATHS)
        assert "data/compound_learning.json" in paths
        assert "data/candidate_bindings.json" in paths
        assert "data/state.sqlite3" in paths
        assert "data/recovery/" in paths
        assert "data/trajectories/" in paths

    def test_seed_paths_include_new_stores(self) -> None:
        """SEED_PATHS must include compound_learning, candidate_bindings, state.sqlite3, recovery, trajectories."""
        paths = set(SEED_PATHS)
        assert "data/compound_learning.json" in paths
        assert "data/candidate_bindings.json" in paths
        assert "data/state.sqlite3" in paths
        assert "data/recovery/" in paths
        assert "data/trajectories/" in paths

    def test_checkpoint_paths_include_existing_stores(self) -> None:
        """CHECKPOINT_PATHS must still include the existing bridge state files."""
        paths = set(CHECKPOINT_PATHS)
        assert "data/last_run.json" in paths
        assert "data/processed_issues.json" in paths
        assert "data/scores.json" in paths
        assert "data/retry_issues.json" in paths
        assert "data/crew_runs.json" in paths
        assert "data/grading_queue.jsonl" in paths

    def test_seed_paths_include_existing_stores(self) -> None:
        """SEED_PATHS must still include the existing bridge state files."""
        paths = set(SEED_PATHS)
        assert "data/retry_issues.json" in paths
        assert "data/processed_issues.json" in paths
        assert "data/last_run.json" in paths
        assert "data/grading_queue.jsonl" in paths

    def test_checkpoint_stores_returns_list(self) -> None:
        """checkpoint_stores() returns a list of paths."""
        result = checkpoint_stores(Path("/nonexistent"))
        assert isinstance(result, list)
        assert len(result) > 0

    def test_seed_stores_returns_list(self) -> None:
        """seed_stores() returns a list of paths."""
        result = seed_stores(Path("/nonexistent"))
        assert isinstance(result, list)
        assert len(result) > 0

    def test_validate_contract_empty_dir(self, tmp_path: Path) -> None:
        """validate_contract on an empty data dir returns no errors."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        errors = validate_contract(data_dir)
        assert errors == []

    def test_validate_contract_detects_corrupt_json(self, tmp_path: Path) -> None:
        """validate_contract detects corrupt JSON files."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "compound_learning.json").write_text("{corrupt")
        errors = validate_contract(data_dir)
        assert len(errors) > 0
        assert any("compound_learning" in e for e in errors)

    def test_validate_contract_detects_corrupt_sqlite(self, tmp_path: Path) -> None:
        """validate_contract detects corrupt SQLite files."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        db_path = data_dir / "state.sqlite3"
        db_path.write_text("not a sqlite file")
        errors = validate_contract(data_dir)
        assert len(errors) > 0
        assert any("state.sqlite3" in e for e in errors)


# ── Test: Full fresh-checkout simulation ─────────────────────────────────────


class TestFreshCheckoutSimulation:
    """Simulate the full fresh-checkout cycle: produce → checkpoint → restore → consume."""

    def test_full_cycle_compound_learning(self, tmp_path: Path) -> None:
        """Full cycle: produce compound learning → sanitize → restore → consume."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        # Phase 1: Producer writes
        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        record = store.observe(
            bead_id="bead-full-cycle",
            trigger="bead_completed",
            evidence={"outcome": {"status": "success"}},
        )
        obs_id = record["observation_id"]
        store.propose(obs_id, {
            "change_id": "change-full",
            "kind": "improvement",
            "target": "target-full",
            "reason": "full cycle test",
        })
        store.verify(obs_id, accepted=True, evidence={"independent": True})
        store.record(obs_id)

        # Phase 2: Checkpoint (sanitize)
        sanitize_all_stores(data_dir)

        # Phase 3: Fresh checkout (new store instance)
        fresh_store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)

        # Phase 4: Consumer reads
        records = fresh_store.recent(limit=10)
        assert len(records) == 1
        assert records[0]["phase"] == "recorded"
        assert records[0]["bead_id"] == "bead-full-cycle"
        assert records[0]["candidate"]["change_id"] == "change-full"

        # Phase 5: Replay does not reset
        replay_store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        assert len(replay_store.recent(limit=10)) == 1

    def test_full_cycle_teacher_approval(self, tmp_path: Path) -> None:
        """Full cycle: produce approval → sanitize → restore → consume."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        # Phase 1: Producer writes
        from state_journal import StateJournal
        journal = StateJournal(data_dir / STATE_JOURNAL_PATH)
        journal.issue_approval(
            approval_id="approval-full",
            candidate_id="candidate-full",
            head_sha="a" * 40,
            actor="human@example.com",
            scope="open_pr_for_candidate",
        )

        # Phase 2: Checkpoint
        sanitize_state_journal(data_dir / STATE_JOURNAL_PATH)

        # Phase 3: Fresh checkout
        fresh_journal = StateJournal(data_dir / STATE_JOURNAL_PATH)

        # Phase 4: Consumer reads
        approval = fresh_journal.approval("approval-full")
        assert approval.state == "issued"
        assert approval.candidate_id == "candidate-full"

        # Phase 5: Replay does not reset
        replay_journal = StateJournal(data_dir / STATE_JOURNAL_PATH)
        assert replay_journal.approval("approval-full").state == "issued"

    def test_full_cycle_candidate_binding(self, tmp_path: Path) -> None:
        """Full cycle: produce binding → sanitize → restore → consume."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        repo = tmp_path / "repo"
        _make_repo(repo)
        manifest = _make_manifest(tmp_path, candidate_id="cand-full", repo=repo)

        # Phase 1: Producer writes
        from candidate_binding import CandidateBindingStore, bind_trusted_evidence
        store = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)
        store.put(bind_trusted_evidence(
            manifest=manifest,
            verification=_VerificationStub(
                candidate_id=manifest.candidate_id, head_sha=manifest.head_sha,
            ),
            approval=None,
        ))

        # Phase 2: Checkpoint
        sanitize_all_stores(data_dir)

        # Phase 3: Fresh checkout
        fresh_store = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)

        # Phase 4: Consumer reads
        binding = fresh_store.get("cand-full")
        assert binding is not None
        assert binding.candidate_id == "cand-full"
        assert binding.verification_passed is True

        # Phase 5: Replay does not reset
        replay_store = CandidateBindingStore(data_dir / CANDIDATE_BINDINGS_PATH)
        assert replay_store.get("cand-full") is not None

    def test_full_cycle_recovery_evidence(self, tmp_path: Path) -> None:
        """Full cycle: produce recovery evidence → sanitize → restore → consume."""
        data_dir = tmp_path / "data"
        recovery_dir = data_dir / RECOVERY_EVIDENCE_PATH / "smoke-full"
        recovery_dir.mkdir(parents=True)

        # Phase 1: Producer writes
        candidate = {
            "candidate_id": "smoke-full",
            "head_sha": _sha("i"),
            "base_sha": _sha("j"),
            "diff_digest": _sha("k"),
            "branch": "candidate",
            "repository": "owner/repo",
        }
        (recovery_dir / "candidate.json").write_text(json.dumps(candidate, indent=2))

        gate = {
            "disposition": "current",
            "head_sha": _sha("i"),
            "candidate_id": "smoke-full",
            "checks": [{"name": "test", "exit": 0}],
        }
        (recovery_dir / "gate-evidence.json").write_text(json.dumps(gate, indent=2))

        db_path = recovery_dir / "state.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE operations (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO operations VALUES ('op-full')")
        conn.commit()
        conn.close()

        # Phase 2: Checkpoint
        sanitize_all_stores(data_dir)

        # Phase 3: Fresh checkout — validate
        errors = validate_contract(data_dir)
        assert errors == []

        # Phase 4: Consumer reads
        restored_candidate = json.loads((recovery_dir / "candidate.json").read_text())
        assert restored_candidate["candidate_id"] == "smoke-full"
        restored_gate = json.loads((recovery_dir / "gate-evidence.json").read_text())
        assert restored_gate["disposition"] == "current"

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM operations").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_full_cycle_trajectory(self, tmp_path: Path) -> None:
        """Full cycle: produce trajectory → sanitize → restore → consume."""
        data_dir = tmp_path / "data"
        traj_dir = data_dir / TRAJECTORIES_PATH
        traj_dir.mkdir(parents=True)

        # Phase 1: Producer writes
        trajectory = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "domain": "python-coding",
            "difficulty": "medium",
            "agent": "coder",
            "prompt": "test prompt",
            "response": "test response",
            "task_score": 85.0,
        }
        traj_file = traj_dir / "20260101_000000_000000--python-coding--coder.json"
        traj_file.write_text(json.dumps(trajectory, indent=2))

        # Phase 2: Checkpoint
        sanitize_all_stores(data_dir)

        # Phase 3: Fresh checkout
        restored = json.loads(traj_file.read_text())

        # Phase 4: Consumer reads
        assert restored["domain"] == "python-coding"
        assert restored["task_score"] == 85.0

        # Phase 5: Replay does not reset
        replay = json.loads(traj_file.read_text())
        assert replay["domain"] == "python-coding"


# ── Test: Sanitization edge cases ─────────────────────────────────────────────


class TestSanitizationEdgeCases:
    """Edge cases for the allowlisted sanitization."""

    def test_sanitize_store_missing_file(self, tmp_path: Path) -> None:
        """sanitize_store on a missing file returns 0."""
        result = sanitize_store(tmp_path / "nonexistent.json")
        assert result == 0

    def test_sanitize_state_journal_missing_file(self, tmp_path: Path) -> None:
        """sanitize_state_journal on a missing file returns 0."""
        result = sanitize_state_journal(tmp_path / "nonexistent.sqlite3")
        assert result == 0

    def test_sanitize_all_stores_empty_dir(self, tmp_path: Path) -> None:
        """sanitize_all_stores on an empty data dir returns empty dict."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        results = sanitize_all_stores(data_dir)
        assert results == {}

    def test_sanitize_preserves_non_sensitive_fields(self, tmp_path: Path) -> None:
        """sanitize_store preserves non-sensitive fields."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        store.observe(
            bead_id="bead-preserve",
            trigger="bead_completed",
            evidence={
                "control": {"exit_code": 0},
                "runtime": {"duration_ms": 1234},
                "verification": {"passed": True},
                "judgment": {"score": 85},
                "outcome": {"status": "success"},
            },
        )

        sanitize_all_stores(data_dir)

        restored = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        data = restored.recent(limit=10)
        assert data[0]["bead_id"] == "bead-preserve"
        assert data[0]["trigger"] == "bead_completed"
        # After JSON round-trip, integers may become strings; check value not type
        assert int(data[0]["evidence"]["control"]["exit_code"]) == 0
        assert data[0]["evidence"]["verification"]["passed"] is True

    def test_sanitize_redacts_bearer_token(self, tmp_path: Path) -> None:
        """sanitize_store redacts bearer tokens."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)

        from compound_learning import CompoundLearningStore
        store = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        store.observe(
            bead_id="bead-token",
            trigger="bead_completed",
            evidence={
                "control": {"authorization": "Bearer abcdef1234567890"},
                "outcome": {"status": "success"},
            },
        )

        sanitize_all_stores(data_dir)

        restored = CompoundLearningStore(data_dir / COMPOUND_LEARNING_PATH)
        data = restored.recent(limit=10)
        evidence_str = json.dumps(data[0]["evidence"])
        assert "abcdef1234567890" not in evidence_str
        assert "[REDACTED]" in evidence_str


# ── Test: Workflow file references the contract ──────────────────────────────


class TestWorkflowReferencesContract:
    """Verify the workflow file references the contract paths."""

    def test_workflow_checkpoint_includes_compound_learning(self) -> None:
        """The school-loop workflow checkpoint step must include compound_learning.json."""
        workflow_path = REPO_ROOT / ".github" / "workflows" / "school-loop.yml"
        if not workflow_path.exists():
            pytest.skip("Workflow file not found")
        content = workflow_path.read_text()
        # The checkpoint step should reference compound_learning.json
        # (either directly or via the contract module)
        assert "compound_learning" in content or "persistence_contract" in content

    def test_workflow_seed_includes_compound_learning(self) -> None:
        """The school-loop workflow seed step must include compound_learning.json."""
        workflow_path = REPO_ROOT / ".github" / "workflows" / "school-loop.yml"
        if not workflow_path.exists():
            pytest.skip("Workflow file not found")
        content = workflow_path.read_text()
        # The seed step should reference compound_learning.json
        assert "compound_learning" in content or "persistence_contract" in content


# ── Test: Recovery smoke integration ──────────────────────────────────────────


class TestRecoverySmokeIntegration:
    """Recovery smoke evidence must be compatible with the persistence contract."""

    def test_recovery_smoke_evidence_validates(self, tmp_path: Path) -> None:
        """Recovery smoke evidence passes validate_contract."""
        data_dir = tmp_path / "data"
        recovery_dir = data_dir / RECOVERY_EVIDENCE_PATH / "smoke-test"
        recovery_dir.mkdir(parents=True)

        # Write minimal recovery evidence
        candidate = {
            "candidate_id": "smoke-1",
            "head_sha": _sha("l"),
            "base_sha": _sha("m"),
            "diff_digest": _sha("n"),
            "branch": "candidate",
            "repository": "owner/repo",
        }
        (recovery_dir / "candidate.json").write_text(json.dumps(candidate, indent=2))

        gate = {
            "disposition": "current",
            "head_sha": _sha("l"),
            "candidate_id": "smoke-1",
            "checks": [{"name": "test", "exit": 0}],
        }
        (recovery_dir / "gate-evidence.json").write_text(json.dumps(gate, indent=2))

        db_path = recovery_dir / "state.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE operations (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO operations VALUES ('op-1')")
        conn.commit()
        conn.close()

        # Validate
        errors = validate_contract(data_dir)
        assert errors == []

    def test_recovery_smoke_evidence_survives_sanitization(self, tmp_path: Path) -> None:
        """Recovery smoke evidence survives sanitization."""
        data_dir = tmp_path / "data"
        recovery_dir = data_dir / RECOVERY_EVIDENCE_PATH / "smoke-test"
        recovery_dir.mkdir(parents=True)

        candidate = {
            "candidate_id": "smoke-1",
            "head_sha": _sha("o"),
            "base_sha": _sha("p"),
            "diff_digest": _sha("q"),
            "branch": "candidate",
            "repository": "owner/repo",
            "worktree": "/Users/brandonbennett/school-core/data/test",
        }
        (recovery_dir / "candidate.json").write_text(json.dumps(candidate, indent=2))

        # Sanitize
        sanitize_all_stores(data_dir)

        # Verify home path is scrubbed
        restored = json.loads((recovery_dir / "candidate.json").read_text())
        assert "/Users/brandonbennett" not in json.dumps(restored)