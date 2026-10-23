"""The crew ledger must live in ONE place, not one per git worktree.

Regression (2026-10-23): ``CREW_RUNS_FILE`` defaulted to
``Path(__file__).parent.parent.parent / "data/crew_runs.json"``, which resolves
from the MODULE's own location. Every linked git worktree therefore carried its
own ``data/crew_runs.json``, so a crew that finished inside a worktree wrote a
record that never reached ``main``.

Measured: 5 ledger copies on disk (root + 4 worktrees). The 2026-10-12 ``done``
crew for issue 415 (``fm-loop-20261012-194417-415``, complete branch/commit/base
triple in the FM state dir) appears in NONE of them.

The fix anchors the path to the MAIN checkout root via
``git rev-parse --git-common-dir`` while keeping the file IN-REPO, so the
git-durability contract keeps working:
``persistence_contract.CHECKPOINT_PATHS``, the ``.gitignore`` whitelist
(``!data/crew_runs.json``), and ``school-loop.yml``'s ``git add -f`` step.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import src.crew.crew_dispatch as crew_dispatch
import src.crew.crew_ledger_reconcile as crew_ledger_reconcile


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(main: Path) -> None:
    main.mkdir(parents=True, exist_ok=True)
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "test")
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")


class TestMainRepoRoot:
    def test_worktree_resolves_to_the_main_checkout(self, tmp_path):
        """THE REGRESSION: from a linked worktree the ledger must resolve to
        the MAIN checkout, not the worktree — or every worktree strands its own
        copy of the ledger."""
        main = tmp_path / "main"
        _init_repo(main)
        worktree = tmp_path / "linked-worktree"
        _git(main, "worktree", "add", "-q", str(worktree))

        assert crew_dispatch._main_repo_root(cwd=worktree) == main.resolve()
        assert crew_dispatch._main_repo_root(cwd=main) == main.resolve()

    def test_falls_back_when_not_inside_git(self, tmp_path):
        """Never raise — a non-git context (CI sandbox, extracted copy) must
        still yield a usable path."""
        plain = tmp_path / "plain"
        plain.mkdir()
        assert crew_dispatch._main_repo_root(cwd=plain) == plain.resolve()

    def test_the_reconcile_audit_uses_the_same_anchor(self, tmp_path):
        """The audit must agree with the writer. ``crew_ledger_reconcile`` had
        its OWN duplicated path definition; fixing only ``crew_dispatch`` would
        leave the audit reading the worktree's stale copy forever."""
        main = tmp_path / "main"
        _init_repo(main)
        worktree = tmp_path / "linked-worktree"
        _git(main, "worktree", "add", "-q", str(worktree))

        assert crew_ledger_reconcile._main_repo_root(cwd=worktree) == main.resolve()


class TestLedgerPathStaysGitTracked:
    def test_default_is_under_data_not_fm_home(self):
        """The ledger must stay git-tracked: ``data/crew_runs.json`` is in
        CHECKPOINT_PATHS and whitelisted in ``.gitignore``. Moving it under
        FM_HOME would break the checkpoint chain and 3 guard tests."""
        default = crew_dispatch._main_repo_root() / "data" / "crew_runs.json"
        assert default.name == "crew_runs.json"
        assert default.parent.name == "data"
        assert "school-core-fm-config" not in str(default)
