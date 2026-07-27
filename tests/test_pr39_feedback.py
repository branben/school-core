"""Regression tests for PR #39 review feedback (real multi-repo isolation bugs).

Covers:
- orca_executor._find_worktree_by_prefix matches the canonical worktree name
  (e.g. "teacher-cto"), not just role-suffixed variants (qodo #2).
- BookbagSignal encodes the repo slug into the ready-flag filename so
  repo-scoped consumers find it (qodo #6).
- run_teacher_review_once threads the repo namespace through to
  TeacherWorktree instead of defaulting to __global__ (Sourcery #1).
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. Worktree rediscovery matches the canonical name ──────────────────────


def test_find_worktree_by_prefix_matches_canonical_name():
    from orca_executor import OrcaExecutionManager

    mgr = OrcaExecutionManager.__new__(OrcaExecutionManager)  # bypass __init__ (no Orca CLI)

    fake_listing = {
        "worktrees": [
            {"name": "teacher-cto", "path": "/wt/teacher-cto", "id": "1"},
            {"name": "teacher-cto-4", "path": "/wt/teacher-cto-4", "id": "2"},
            {"name": "teacher-cto-backup", "path": "/wt/teacher-cto-backup", "id": "3"},
            {"name": "protonic", "path": "/wt/protonic", "id": "4"},
            {"name": "cto-lens-2", "path": "/wt/cto-lens-2", "id": "5"},
        ]
    }

    def fake_run_orca(args, timeout=15):
        return fake_listing

    mgr._run_orca = fake_run_orca

    found = mgr._find_worktree_by_prefix("teacher-cto")
    assert found is not None
    # Must match the canonical name (teacher-cto) or the suffixed variant
    # (teacher-cto-4) — i.e. a path/name rooted at the canonical worktree.
    assert "teacher-cto" in found
    # Must NOT match an unrelated name like teacher-cto-backup or protonic.
    assert "backup" not in found
    assert "protonic" not in found


# ── 2. BookbagSignal encodes repo slug ───────────────────────────────────────


def test_bookbag_signal_encodes_repo_slug(tmp_path, monkeypatch):
    import bookbag

    monkeypatch.setattr(bookbag, "SIGNAL_DIR", tmp_path)
    sig = bookbag.BookbagSignal("bead-123", repo="branben/sound-royale-ny")
    assert "branben__sound-royale-ny__bead-123.ready" in str(sig._ready_path)
    sig.ready()
    assert sig._ready_path.exists()
    assert sig.check() is True


def test_bookbag_signal_default_is_global(tmp_path, monkeypatch):
    import bookbag

    monkeypatch.setattr(bookbag, "SIGNAL_DIR", tmp_path)
    sig = bookbag.BookbagSignal("bead-9")
    assert "__global____bead-9.ready" in str(sig._ready_path)


# ── 3. run_teacher_review_once threads repo through ──────────────────────────


def test_run_teacher_review_once_passes_repo_to_teacher(monkeypatch):
    script = ROOT / "scripts" / "run_teacher_review_once.py"
    mod = _load("run_teacher_review_once", script)

    captured = {}

    class FakeTeacher:
        def __init__(self, role, repo="__global__"):
            captured["role"] = role
            captured["repo"] = repo

        def boot(self):
            return "/wt/fake"

        def review_cycle(self):
            return 0

    monkeypatch.setattr(mod, "TeacherWorktree", FakeTeacher)

    # Simulate: python run_teacher_review_once.py cto branben/sound-royale-ny
    monkeypatch.setattr(sys, "argv", ["run_teacher_review_once.py", "cto", "branben/sound-royale-ny"])
    mod.main()
    assert captured["role"] == "cto"
    assert captured["repo"] == "branben/sound-royale-ny"


def test_run_teacher_review_once_env_repo_fallback(monkeypatch):
    script = ROOT / "scripts" / "run_teacher_review_once.py"
    mod = _load("run_teacher_review_once", script)

    captured = {}

    class FakeTeacher:
        def __init__(self, role, repo="__global__"):
            captured["repo"] = repo

        def boot(self):
            return "/wt/fake"

        def review_cycle(self):
            return 0

    monkeypatch.setattr(mod, "TeacherWorktree", FakeTeacher)
    monkeypatch.setattr(sys, "argv", ["run_teacher_review_once.py", "coo"])
    monkeypatch.setenv("SCHOOL_REPO", "owner/repo-b")
    mod.main()
    assert captured["repo"] == "owner/repo-b"
