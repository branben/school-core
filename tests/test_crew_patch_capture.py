"""Crew work must survive teardown as a patch (bead school-core-3um / B8).

WHY THIS EXISTS
---------------
54 crews emitted valid ``done: branch=... commit=... base=...`` lines and every
one of those commits is now unreachable. Verified: ``git cat-file -t`` absent in
both primary clones AND in the crew's own clone; ``git ls-remote`` shows zero
``fm/`` branches on origin. The disposable clone is reset between runs and the
worktree deleted, so the branch ref vanishes and the commit is orphaned.

The work was good. Two surviving branches hold ``0543d79`` — "extract MIN_ROUNDS
and MAX_ROUNDS constants into game.ts", 2 files, 5 insertions, a correct diff.

Meanwhile ``pr_creator`` builds the PR from the model's RESPONSE TEXT
(pr_creator.py:437-451), so judges grade prose while the real diff evaporates.

DESIGN — the SUPERVISOR captures, not the agent
-----------------------------------------------
phymora's reviewed option (a) was "have the crew write a patch file into the task
dir." This implements the same idea with one deliberate change: **the supervisor
runs ``git diff`` in the worktree**, rather than instructing the agent to write
the patch.

That change matters because agent compliance is precisely the failure mode this
system already has. The artifact handshake asks the agent to emit branch/commit/
base in a parseable shape and #342 produced nothing at all. A supervisor-side
capture has no compliance dependency: if the worktree has changes, the patch
exists.

Captured BEFORE teardown, into the task dir, which already survives teardown
alongside ``report.md``.

WHY A PATCH AND NOT A BRANCH HANDOFF
------------------------------------
Both of my earlier premises were refuted in review:
  * I believed the worktree shared an object store with a persistent clone, so a
    bare ``git branch`` would preserve the commit. FALSE — ``issue_bridge`` calls
    ``repo_reader.clone_repo``, a SEPARATE clone. You cannot name an object your
    repo does not have.
  * That clone is ``git clone --depth 1`` (repo_reader.py:102) and verified
    shallow, so fetching from it can graft history and the ``base=`` SHA may not
    exist there at all.
A patch is text. It has no object-store or shallow-history dependency.

THE EMPTINESS RULE — phymora's sharpest finding
-----------------------------------------------
``pr_creator.py:453-457``'s only guard catches BLOB CREATION FAILURE, not an
empty diff. Today exactly one blob is always written, so empty is impossible.
Introduce real crew content and a silently-failed capture yields a zero-entry
tree: **a PR with no diff that nothing vetoes.** An empty capture must therefore
be a hard, explicit failure — never a green PR. A patch file makes that
self-evident, which is why it beats a branch handoff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import crew_dispatch


def _worktree(tmp_path: Path, *, with_change: bool = True) -> Path:
    """A real git repo with a committed base and (optionally) a crew commit."""
    wt = tmp_path / "wt"
    wt.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True,
                                    capture_output=True)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("checkout", "-q", "-b", "main")
    (wt / "game.ts").write_text("export const x = 1;\n")
    run("add", "game.ts")
    run("commit", "-q", "-m", "base")
    if with_change:
        run("checkout", "-q", "-b", "fm/crew-1")
        (wt / "game.ts").write_text("export const MIN_ROUNDS = 1;\nexport const x = 1;\n")
        run("add", "game.ts")
        run("commit", "-q", "-m", "extract constant")
    return wt


class TestPatchIsCaptured:
    def test_capture_writes_a_patch_file(self, tmp_path):
        """REGRESSION: the crew's diff was destroyed with the worktree."""
        wt = _worktree(tmp_path)
        out = tmp_path / "task" / "changes.patch"
        result = crew_dispatch.capture_crew_patch(
            worktree_path=wt, base="main", destination=out
        )
        assert result is not None, "no patch captured from a worktree with changes"
        assert out.exists(), "capture reported success but wrote no file"

    def test_patch_contains_the_real_diff(self, tmp_path):
        """The patch must carry the actual change, not a placeholder."""
        wt = _worktree(tmp_path)
        out = tmp_path / "task" / "changes.patch"
        crew_dispatch.capture_crew_patch(worktree_path=wt, base="main", destination=out)
        text = out.read_text()
        assert "MIN_ROUNDS" in text, "the captured patch does not contain the change"
        assert "game.ts" in text
        assert text.startswith("diff --git") or "diff --git" in text

    def test_capture_is_supervisor_side_not_agent_dependent(self, tmp_path):
        """No agent cooperation required — only a worktree with changes.

        The artifact handshake already fails when agents do not comply (#342
        emitted nothing at all). This path must not inherit that dependency.
        """
        wt = _worktree(tmp_path)
        out = tmp_path / "p.patch"
        # Nothing was written by any agent: no report.md, no status file.
        assert crew_dispatch.capture_crew_patch(
            worktree_path=wt, base="main", destination=out
        ) is not None


class TestEmptyCaptureIsAFailure:
    """The load-bearing guards. An empty patch must never look like success."""

    def test_no_changes_returns_none(self, tmp_path):
        """A worktree with no crew commit has nothing to preserve.

        Returning a path here would let pr_creator build a zero-entry tree — a
        PR with no diff that nothing vetoes.
        """
        wt = _worktree(tmp_path, with_change=False)
        out = tmp_path / "p.patch"
        assert crew_dispatch.capture_crew_patch(
            worktree_path=wt, base="main", destination=out
        ) is None

    def test_empty_patch_file_is_not_written(self, tmp_path):
        """Do not leave a 0-byte patch on disk to be mistaken for output."""
        wt = _worktree(tmp_path, with_change=False)
        out = tmp_path / "p.patch"
        crew_dispatch.capture_crew_patch(worktree_path=wt, base="main", destination=out)
        assert not out.exists() or out.stat().st_size > 0, (
            "an empty patch file was written; a later reader would treat its "
            "existence as evidence of preserved work"
        )

    def test_missing_worktree_returns_none(self, tmp_path):
        assert crew_dispatch.capture_crew_patch(
            worktree_path=tmp_path / "gone", base="main", destination=tmp_path / "p.patch"
        ) is None

    def test_bad_base_returns_none_and_does_not_raise(self, tmp_path):
        """An unresolvable base must fail closed, not crash the dispatch."""
        wt = _worktree(tmp_path)
        assert crew_dispatch.capture_crew_patch(
            worktree_path=wt, base="no-such-ref", destination=tmp_path / "p.patch"
        ) is None


class TestCaptureNeverBreaksDispatch:
    def test_untrusted_base_is_validated(self, tmp_path):
        """`base` comes from model-authored status text — treat as untrusted."""
        wt = _worktree(tmp_path)
        for bad in ("", "--upload-pack=evil", "; rm -rf /", "--output=/tmp/x"):
            assert crew_dispatch.capture_crew_patch(
                worktree_path=wt, base=bad, destination=tmp_path / "p.patch"
            ) is None, f"base {bad!r} was not rejected"

    def test_oversized_patch_is_refused(self, tmp_path, monkeypatch):
        """A runaway diff must not be committed or held in memory unbounded."""
        wt = _worktree(tmp_path)
        monkeypatch.setattr(crew_dispatch, "MAX_PATCH_BYTES", 10)
        assert crew_dispatch.capture_crew_patch(
            worktree_path=wt, base="main", destination=tmp_path / "p.patch"
        ) is None
