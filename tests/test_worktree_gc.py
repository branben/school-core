"""Worktree GC tests — pin the fail-closed janitor boundary.

The janitor's one job: remove ONLY provably-dead worktrees. Every test here
encodes a way a careless GC could destroy work. The None/fail-closed contract
is the thing under test.
"""

import subprocess

import pytest

from worktree_gc import (
    ACTION_KEEP,
    ACTION_PRUNE,
    ACTION_REMOVE,
    WorktreeEntry,
    decide,
    parse_worktree_list,
)


def _e(**kw) -> WorktreeEntry:
    return WorktreeEntry(path="/w/x", **kw)


class TestParseWorktreeList:
    def test_parses_porcelain_v1(self):
        text = (
            "worktree /repo\nHEAD abc1111\nbranch refs/heads/main\n\n"
            "worktree /w/a\nHEAD abc2222\nbranch refs/heads/feat/a\n\n"
            "worktree /w/b\nHEAD abc3333\nprunable\n\n"
            "worktree /w/c\nbranch refs/heads/x\nlocked\n\n"
            "worktree /w/d\nbare\n\n"
        )
        es = parse_worktree_list(text)
        assert [e.path for e in es] == ["/repo", "/w/a", "/w/b", "/w/c", "/w/d"]
        by_path = {e.path: e for e in es}
        assert by_path["/repo"].branch == "main"
        assert by_path["/w/b"].prunable is True
        assert by_path["/w/c"].locked is True
        assert by_path["/w/d"].bare is True

    def test_empty_text(self):
        assert parse_worktree_list("") == []


class TestDecide:
    """decide() with all facts explicit — the pure contract."""

    BASE = dict(is_main=False, dirty=False, merged=True, age_hours=100.0,
                active=False, git_ok=True)

    def test_removes_provably_dead(self):
        action, reason = decide(_e(), **self.BASE)
        assert action == ACTION_REMOVE
        assert "clean" in reason

    def test_prunable_wins_over_everything(self):
        action, _ = decide(_e(prunable=True), **self.BASE)
        assert action == ACTION_PRUNE

    def test_never_touches_main_bare_locked(self):
        cases = [(_e(bare=True), {}), (_e(), dict(is_main=True)),
                 (_e(locked=True), {})]
        for e, kw in cases:
            action, reason = decide(e, **{**self.BASE, **kw})
            assert action == ACTION_KEEP
            assert "never touched" in reason or "bare" in reason

    def _decide(self, **overrides):
        return decide(_e(), **{**self.BASE, **overrides})

    def test_dirty_keeps(self):
        action, reason = self._decide(dirty=True)
        assert action == ACTION_KEEP
        assert "uncommitted" in reason

    def test_unmerged_keeps(self):
        action, reason = self._decide(merged=False)
        assert action == ACTION_KEEP
        assert "not fully merged" in reason

    def test_young_keeps(self):
        action, reason = self._decide(age_hours=1.0)
        assert action == ACTION_KEEP
        assert "grace" in reason

    def test_active_keeps(self):
        action, reason = self._decide(active=True)
        assert action == ACTION_KEEP
        assert "live process" in reason

    def test_none_facts_fail_closed(self):
        # every unverifiable fact → KEEP, the fail-closed contract
        for kw in [dict(dirty=None), dict(merged=None), dict(age_hours=None),
                   dict(git_ok=False)]:
            action, _ = self._decide(**kw)
            assert action == ACTION_KEEP, kw

    def test_boundary_exactly_at_grace_is_removable(self):
        action, _ = self._decide(age_hours=48.0)
        assert action == ACTION_REMOVE

    def test_dirty_beats_merged(self):
        """Order matters: dirty worktree that is also merged must KEEP."""
        action, reason = self._decide(dirty=True, merged=True)
        assert action == ACTION_KEEP
        assert "uncommitted" in reason


class TestDecideOrdering:
    def test_locked_prunable_priority(self):
        # prunable beats locked (prune is safe even if locked flag stale)
        action, _ = decide(_e(prunable=True, locked=True),
                           **TestDecide.BASE)
        assert action == ACTION_PRUNE


# ── gather_facts / sweep integration (light, with fakes) ────────────────────

class FakeRunner:
    """Routes canned responses by command prefix."""

    def __init__(self, responses: dict[tuple, subprocess.CompletedProcess]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        key = tuple(cmd[:4])
        return self.responses.get(key, subprocess.CompletedProcess(cmd, 1, "", "no fake"))


class TestGatherFacts:
    def _wt(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /elsewhere\n")
        return wt

    def test_clean_merged_old_inactive(self, tmp_path):
        import time as _t
        wt = self._wt(tmp_path)
        old = _t.time() - 100 * 3600
        import os
        os.utime(wt, (old, old))
        os.utime(wt / ".git", (old, old))
        fake = FakeRunner({
            ("git", "-C", str(wt), "status"): subprocess.CompletedProcess(
                [], 0, "", ""),
            ("git", "-C", str(wt), "merge-base"): subprocess.CompletedProcess(
                [], 0, "", ""),
        })
        from worktree_gc import gather_facts
        facts = gather_facts(
            tmp_path / "repo", WorktreeEntry(path=str(wt)),
            now=_t.time(), live_paths=set(), run=fake)
        assert facts["dirty"] is False
        assert facts["merged"] is True
        assert facts["age_hours"] >= 48
        assert facts["active"] is False
        assert facts["git_ok"] is True

    def test_status_failure_fails_closed(self, tmp_path):
        wt = self._wt(tmp_path)
        fake = FakeRunner({
            ("git", "-C", str(wt), "status"): subprocess.CompletedProcess(
                [], 128, "", "fatal: not a working tree"),
        })
        from worktree_gc import gather_facts
        facts = gather_facts(
            tmp_path / "repo", WorktreeEntry(path=str(wt)),
            now=0, live_paths=set(), run=fake)
        assert facts["git_ok"] is False

    def test_active_cwd_inside_keeps(self, tmp_path):
        import time as _t, os
        wt = self._wt(tmp_path)
        old = _t.time() - 100 * 3600
        os.utime(wt, (old, old))
        os.utime(wt / ".git", (old, old))
        fake = FakeRunner({
            ("git", "-C", str(wt), "status"): subprocess.CompletedProcess(
                [], 0, "", ""),
            ("git", "-C", str(wt), "merge-base"): subprocess.CompletedProcess(
                [], 0, "", ""),
        })
        from worktree_gc import gather_facts
        facts = gather_facts(
            tmp_path / "repo", WorktreeEntry(path=str(wt)),
            now=_t.time(), live_paths={str(wt.resolve())}, run=fake)
        assert facts["active"] is True
