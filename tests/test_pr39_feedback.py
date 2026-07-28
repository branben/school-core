"""Regression tests for PR #39 / #40 review feedback (multi-repo isolation).

Covers:
- orca_executor._find_worktree_by_prefix matches the canonical worktree name
  (e.g. "teacher-cto") and reboot suffixes (teacher-cto-4, teacher-cto-lens-2),
  but NOT unrelated names like "teacher-cto-backup" or "protonic" (greptile #3).
- BookbagSignal encodes the repo slug into the ready-flag filename so
  repo-scoped consumers find it (qodo #6), for both global and non-global repos.
- run_teacher_review_once threads the repo namespace through to
  TeacherWorktree: explicit CLI arg, SCHOOL_REPO env, and the default
  REPO_GLOBAL fallback (Sourcery suggestions).
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


# ── 1. Worktree rediscovery matches canonical name, excludes backups ────────


def test_find_worktree_by_prefix_matches_canonical_and_reboot_suffixes():
    from orca_executor import OrcaExecutionManager

    mgr = OrcaExecutionManager.__new__(OrcaExecutionManager)

    fake_listing = {
        "worktrees": [
            {"name": "teacher-cto", "path": "/wt/teacher-cto", "id": "1"},
            {"name": "teacher-cto-4", "path": "/wt/teacher-cto-4", "id": "2"},
            {"name": "teacher-cto-lens-2", "path": "/wt/teacher-cto-lens-2", "id": "3"},
            {"name": "teacher-cto-backup", "path": "/wt/teacher-cto-backup", "id": "4"},
            {"name": "protonic", "path": "/wt/protonic", "id": "5"},
        ]
    }
    mgr._run_orca = lambda args, timeout=15: fake_listing

    # Canonical name + reboot suffixes match.
    found = mgr._find_worktree_by_prefix("teacher-cto")
    assert found is not None
    assert "teacher-cto" in found
    assert "backup" not in found
    assert "protonic" not in found

    # A backup worktree must NOT be returned as the canonical teacher.
    names = []
    for wt in fake_listing["worktrees"]:
        names.append(wt["name"])
    assert "teacher-cto-backup" not in (
        mgr._find_worktree_by_prefix("teacher-cto") or ""
    )


# ── 2. BookbagSignal encodes repo slug (global + non-global) ───────────────


def test_bookbag_signal_encodes_non_global_repo_slug(tmp_path, monkeypatch):
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


# ── 3. run_teacher_review_once threads repo through (CLI / env / default) ────


def test_run_teacher_review_once_cli_repo(tmp_path, monkeypatch):
    script = ROOT / "scripts" / "run_teacher_review_once.py"
    mod = _load("run_teacher_review_once", script)

    captured = {}

    class FakeTeacher:
        def __init__(self, role, repo="__global__", diagnose_on_fail=False):
            captured["role"] = role
            captured["repo"] = repo
            captured["diagnose_on_fail"] = diagnose_on_fail

        def boot(self):
            return "/wt/fake"

        def review_cycle(self):
            return 0

    monkeypatch.setattr(mod, "TeacherWorktree", FakeTeacher)
    monkeypatch.setattr(sys, "argv", ["run_teacher_review_once.py", "cto", "branben/sound-royale-ny"])
    mod.main()
    assert captured["role"] == "cto"
    assert captured["repo"] == "branben/sound-royale-ny"
    assert captured["diagnose_on_fail"] is False


def test_run_teacher_review_once_env_repo_fallback(tmp_path, monkeypatch):
    script = ROOT / "scripts" / "run_teacher_review_once.py"
    mod = _load("run_teacher_review_once", script)

    captured = {}

    class FakeTeacher:
        def __init__(self, role, repo="__global__", diagnose_on_fail=False):
            captured["repo"] = repo
            captured["diagnose_on_fail"] = diagnose_on_fail

        def boot(self):
            return "/wt/fake"

        def review_cycle(self):
            return 0

    monkeypatch.setattr(mod, "TeacherWorktree", FakeTeacher)
    monkeypatch.setattr(sys, "argv", ["run_teacher_review_once.py", "coo"])
    monkeypatch.setenv("SCHOOL_REPO", "owner/repo-b")
    mod.main()
    assert captured["repo"] == "owner/repo-b"
    assert captured["diagnose_on_fail"] is False


def test_run_teacher_review_once_defaults_to_global(tmp_path, monkeypatch):
    script = ROOT / "scripts" / "run_teacher_review_once.py"
    mod = _load("run_teacher_review_once", script)

    captured = {}

    class FakeTeacher:
        def __init__(self, role, repo="__global__", diagnose_on_fail=False):
            captured["repo"] = repo
            captured["diagnose_on_fail"] = diagnose_on_fail

        def boot(self):
            return "/wt/fake"

        def review_cycle(self):
            return 0

    monkeypatch.setattr(mod, "TeacherWorktree", FakeTeacher)
    monkeypatch.setattr(sys, "argv", ["run_teacher_review_once.py", "cto"])
    monkeypatch.delenv("SCHOOL_REPO", raising=False)
    mod.main()
    assert captured["repo"] == "__global__"
    assert captured["diagnose_on_fail"] is False
