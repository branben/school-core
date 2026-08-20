"""The crew record must state whether its cited commit actually resolves.

WHY THIS EXISTS
---------------
``commit_is_reachable`` shipped in 19d5758 but NOTHING CALLED IT. That is the
"a guard nobody calls is a guard that does not guard" mistake, already made twice
tonight (db83c9f needed 7ab03e3; 066b383 needed its own wiring). These tests wire
it and pin the wiring.

The record being corrected: 54 crews emitted a terminal ``done:`` citing a real
SHA, and those objects do not exist anywhere — verified absent in both primary
clones AND in the crew's own shallow clone. The crew's disposable clone is reset
between runs and its worktree deleted, so the branch ref vanishes and the commit
is orphaned.

WHERE THE PROBE MUST HAPPEN — and this is the load-bearing design point:

``dispatch_crew`` writes the terminal record at the U10 checkpoint
(crew_dispatch.py:1038-1054) and only THEN calls ``teardown_worktree``. The probe
must run BEFORE teardown, while the worktree and its branch still exist —
probing after teardown would report every commit unreachable and prove nothing
about whether the work was real.

It must also NOT disturb U10 itself: ``terminal_status`` and ``fallback_reason``
are never mutated by the probe result. A crew that genuinely finished is still
``done``; the reachability answer is recorded as its own field. Conflating them
would let an unreachable-commit finding silently downgrade real work to
``failed``, which destroys the very signal we are trying to preserve.
"""

from pathlib import Path

import pytest

import crew_dispatch


class TestReachabilityIsRecorded:
    def test_record_carries_commit_reachable_field(self, monkeypatch, tmp_path):
        """The terminal record must include the probe result.

        Without it, a reader has a SHA and no way to know it resolves — the
        original integrity failure.
        """
        captured = {}

        def fake_update(crew_id, payload):
            captured.update(payload)

        monkeypatch.setattr(crew_dispatch, "_update_run", fake_update)
        crew_dispatch._record_artifact_reachability(
            crew_id="c1",
            artifact_identity={"branch": "fm/c1", "commit": "abc1234", "base": "def5678"},
            worktree_id=f"wt::{tmp_path}",
        )
        assert "commit_reachable" in captured, (
            "terminal record has no commit_reachable field, so the cited SHA "
            "remains unverifiable"
        )

    def test_unreachable_commit_records_false(self, monkeypatch, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        captured = {}
        monkeypatch.setattr(
            crew_dispatch, "_update_run", lambda cid, p: captured.update(p)
        )
        crew_dispatch._record_artifact_reachability(
            crew_id="c1",
            artifact_identity={
                "branch": "fm/c1",
                "commit": "0ff604b7841594535abfd2737237a2c9f14f2859",
                "base": "b0075d7",
            },
            worktree_id=f"wt::{tmp_path}",
        )
        assert captured["commit_reachable"] is False

    def test_missing_identity_records_none_not_false(self, monkeypatch):
        """No identity means UNKNOWN, not 'the commit is missing'.

        A failed/blocked crew never cites a commit. Recording False would
        assert something we never checked.
        """
        captured = {}
        monkeypatch.setattr(
            crew_dispatch, "_update_run", lambda cid, p: captured.update(p)
        )
        crew_dispatch._record_artifact_reachability(
            crew_id="c1", artifact_identity=None, worktree_id="wt::/tmp/x"
        )
        assert captured["commit_reachable"] is None

    def test_missing_worktree_records_none(self, monkeypatch):
        """Cannot look != not there. Tri-state must survive the wiring."""
        captured = {}
        monkeypatch.setattr(
            crew_dispatch, "_update_run", lambda cid, p: captured.update(p)
        )
        crew_dispatch._record_artifact_reachability(
            crew_id="c1",
            artifact_identity={"branch": "b", "commit": "abc1234", "base": "d"},
            worktree_id=None,
        )
        assert captured["commit_reachable"] is None


class TestProbeNeverCorruptsTheOutcome:
    """Guards on the U10 invariant. These are the ones that matter."""

    def test_probe_does_not_write_status_or_fallback_reason(self, monkeypatch, tmp_path):
        """An unreachable commit must NOT downgrade a genuine `done`.

        crew_dispatch.py:1038 documents U10: persist terminal state before
        teardown so cleanup failure cannot mask the outcome. The reachability
        probe must be additive only.
        """
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        captured = {}
        monkeypatch.setattr(
            crew_dispatch, "_update_run", lambda cid, p: captured.update(p)
        )
        crew_dispatch._record_artifact_reachability(
            crew_id="c1",
            artifact_identity={"branch": "b", "commit": "deadbee", "base": "d"},
            worktree_id=f"wt::{tmp_path}",
        )
        assert "status" not in captured, (
            "the probe mutated terminal_status — an unreachable commit would "
            "silently turn real completed work into a failure"
        )
        assert "fallback_reason" not in captured

    def test_probe_never_raises(self, monkeypatch):
        """A broken probe must not break dispatch.

        Recording the outcome is more important than probing it.
        """
        def boom(cid, p):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(crew_dispatch, "_update_run", boom)
        # Must not propagate.
        crew_dispatch._record_artifact_reachability(
            crew_id="c1",
            artifact_identity={"branch": "b", "commit": "abc1234", "base": "d"},
            worktree_id="wt::/nope",
        )
