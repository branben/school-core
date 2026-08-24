"""A `commit=` in the record must resolve, or must not be emitted.

WHY THIS EXISTS
---------------
54 crews reached a terminal ``done:`` across 6 real issues, each citing a real
SHA::

    done: branch=fm/fm-loop-20260819-190209-342 commit=0ff604b784... base=b0075d74...

Those commits do not exist. Verified with ``git cat-file -t``: absent in
``~/sound-royale-ny``, absent in ``~/orca/sound-royale-ny``, and absent in the
crew's OWN clone at
``~/.cache/branben__sound-royale-ny/repos/branben__sound-royale-ny``. ``git
ls-remote origin`` shows zero ``fm/`` branches.

The mechanism, from that clone's reflog::

    b0075d7 HEAD@{0}: reset: moving to b0075d74f24a2bdfbb6394a3095930828e9b6f1e
    b0075d7 HEAD@{1}: reset: moving to b0075d74f24a2bdfbb6394a3095930828e9b6f1e
    b0075d7 HEAD@{2}: clone: from https://github.com/branben/sound-royale-ny.git

The crew's disposable clone is reset between runs and its worktree deleted, so
the branch ref disappears and the commit is orphaned. The SHA was real when
written and unreachable minutes later.

That is precisely the integrity failure Brandon flagged earlier in this project:
a status record asserting ``done: commit=<hash>`` with no object behind it. A
record that cites unverifiable evidence is worse than one that admits it has
none, because a reader (human or agent) will trust the hash.

SCOPE — this is the HONEST-RECORD fix, deliberately not the preservation fix.
Preserving the crew's actual diff is a larger change with real hazards that
phymora identified in review:
  * the crew's clone is ``git clone --depth 1`` (repo_reader.py:102) and verified
    shallow, so fetching from it can yield grafted history and the ``base=`` in
    the done: line may not even exist there;
  * pr_creator.py:436-440's only emptiness guard catches BLOB CREATION FAILURE,
    not an empty diff — today exactly one blob is always written so empty is
    impossible, but swapping in a real branch makes a silent handoff failure
    produce a zero-entry tree, i.e. a PR with no diff that nothing vetoes.
Shipping the honest record first costs nothing and removes the misleading
artifact while that design is settled.
"""

import re

import pytest

import crew_dispatch


class TestUnresolvableCommitIsNotAsserted:
    def test_artifact_identity_records_reachability(self):
        """A recorded artifact identity must say whether its commit resolves.

        REGRESSION: the identity was persisted verbatim from the agent's status
        line with no check that the object existed, so every crew record carried
        an unverifiable hash.
        """
        assert hasattr(crew_dispatch, "commit_is_reachable"), (
            "no way to tell a resolvable commit from an orphaned one; the "
            "record asserts a SHA it never verified"
        )

    def test_missing_commit_reports_false(self, tmp_path):
        """An absent object must report unreachable, not raise and not pass."""
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        assert crew_dispatch.commit_is_reachable(
            "0ff604b7841594535abfd2737237a2c9f14f2859", repo_path=tmp_path
        ) is False

    def test_present_commit_reports_true(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "f.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True
        )
        sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert crew_dispatch.commit_is_reachable(sha, repo_path=tmp_path) is True

    def test_unreachable_repo_reports_none_not_false(self, tmp_path):
        """Tri-state: 'cannot tell' must not masquerade as 'not there'.

        Reporting False for a missing repo would let a tooling failure read as
        evidence the crew's work was lost — the same collapse-UNKNOWN-into-a-
        verdict trap fixed in the review gates tonight.
        """
        assert crew_dispatch.commit_is_reachable(
            "0ff604b7841594535abfd2737237a2c9f14f2859",
            repo_path=tmp_path / "does-not-exist",
        ) is None

    def test_garbage_sha_does_not_raise(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for bad in ("", "zzzz", "../../etc/passwd", "HEAD; rm -rf /"):
            assert crew_dispatch.commit_is_reachable(bad, repo_path=tmp_path) in (
                False, None,
            ), f"garbage sha {bad!r} was treated as reachable"
