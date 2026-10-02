"""Shared fixtures for school-core tests."""

import json
import os
from pathlib import Path

import pytest

from scoring import ScoreStore


def _is_git_broker_wrapper(entry: str) -> bool:
    """True when ``entry/git`` is a script shim, not the real git binary.

    The Paperclip runtime prepends a Node credential-broker wrapper (a
    ``#!/usr/bin/env node`` script named ``git``) to PATH. It strips
    ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` from the child env and re-sets them to
    the empty string, repopulating them only when its GitHub broker answers.
    A real git is a compiled binary (ELF/Mach-O), never a ``#!`` script.
    """
    candidate = Path(entry) / "git"
    try:
        if not candidate.is_file():
            return False
        with candidate.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return False
    return head.startswith(b"#!") and b"node" in head


@pytest.fixture(autouse=True)
def hermetic_git_identity(monkeypatch):
    """Make every git subprocess independent of the ambient runtime identity.

    Two independent hazards in the Paperclip agent environment make the seam
    suite red in a clean shell's absence:

    1. ``GIT_AUTHOR_NAME``/``GIT_AUTHOR_EMAIL``/``GIT_COMMITTER_NAME``/
       ``GIT_COMMITTER_EMAIL`` (and ``GIT_CONFIG_COUNT``) are exported
       **empty-but-set**. Git honours an empty (set) identity and refuses to
       commit: ``fatal: empty ident name (for <>) not allowed`` (exit 128).
    2. A Node git credential-broker wrapper sits first on PATH and re-zeroes
       the identity whenever its broker is unavailable — even when the caller
       passes an explicit ``GIT_AUTHOR_*`` or a ``git -c user.name`` override.

    Tests must not depend on either: this fixture clears the empty identity
    vars and drops the broker wrapper from PATH so ``git`` resolves to the real
    binary. The suite never needs brokered GitHub credentials.
    """
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(var, raising=False)

    raw_path = os.environ.get("PATH", "")
    entries = [entry for entry in raw_path.split(os.pathsep) if entry]
    kept = [entry for entry in entries if not _is_git_broker_wrapper(entry)]
    if len(kept) != len(entries):
        monkeypatch.setenv("PATH", os.pathsep.join(kept))


@pytest.fixture
def store(tmp_path):
    """Temp ScoreStore so production code never writes the live data/scores.json.

    Any test that exercises a path which persists scores MUST inject this
    (e.g. bridge_issues(..., store=store)) to avoid polluting real data.
    """
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(json.dumps({
        "foundry-coder-7b": {"_default": 25.0, "debugging": 40.0, "code-implementation": 30.0},
        "foundry-coder-1.5b": {"_default": 20.0, "debugging": 40.0},
    }))
    return ScoreStore(file_path=str(scores_file))


@pytest.fixture(autouse=True)
def isolate_data_dirs(tmp_path, monkeypatch):
    """Redirect every live data/*.json writer to a temp dir.

    Production modules default their persistence paths to the repo's
    data/ directory (scores.json, activity_log.json, decision_log.json,
    escalation_log.json). Without this, importing/using those modules in a
    test silently writes test noise into the real data files. Point all of
    them (and the cached singletons) at tmp_path so local test runs never
    pollute the live dashboard data.
    """
    import activity_log
    import bookbag
    import decision_log
    import escalation_log
    import scoring
    import sleep_state
    import trajectory

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(activity_log, "ACTIVITY_LOG_PATH", data_dir / "activity_log.json")
    monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", data_dir / "decision_log.json")
    monkeypatch.setattr(escalation_log, "LOG_PATH", data_dir / "escalation_log.json")
    monkeypatch.setattr(sleep_state, "SCORES_PATH", data_dir / "scores.json")

    # Redirect trajectory and bookbag writes to temp so tests that
    # exercise real director.run_task() paths don't leak persistent
    # artifacts into the repo or the developer's home directory.
    traj_dir = tmp_path / "trajectories"
    traj_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(trajectory, "TRAJECTORY_DIR", traj_dir)
    monkeypatch.setattr(bookbag, "BOOKBAG_DIR", Path(tmp_path / "bookbag"))

    # Reset cached singletons so they pick up the redirected paths.
    activity_log._default_log = None
    decision_log._default_log = None

    # Isolate crew_runs.json so tests that exercise dispatch_crew /
    # issue_bridge paths never pollute the real data/crew_runs.json.
    # Tests that need a specific path still pass it explicitly via monkeypatch.
    import crew_dispatch
    import issue_bridge

    # Bridge tests must never use pr_creator's independent `gh` subprocess
    # boundary. Tests that exercise publication failures override this seam.
    monkeypatch.setattr(
        issue_bridge,
        "create_pr_for_issue",
        lambda **kwargs: "https://github.com/test/repo/pull/1",
    )
    monkeypatch.setattr(crew_dispatch, "CREW_RUNS_FILE", data_dir / "crew_runs.json")
    monkeypatch.setattr(issue_bridge, "CREW_RUNS_FILE", data_dir / "crew_runs.json")

    monkeypatch.setattr(crew_dispatch, "FM_HOME", tmp_path / "fm-home")
    monkeypatch.setattr(crew_dispatch, "STATE_DIR", tmp_path / "fm-home" / "state")
    monkeypatch.setattr(crew_dispatch, "DATA_DIR", tmp_path / "fm-home" / "data")

    # Bulletproof scores isolation: any ScoreStore() created WITHOUT an
    # explicit file_path (e.g. mcp_server's module-level store, director
    # fallbacks, autonomous_loop) is redirected to tmp. An explicitly passed
    # file_path is always preserved, so production behaviour is untouched.
    tmp_scores = data_dir / "scores.json"
    orig_init = scoring.ScoreStore.__init__

    def _isolated_init(self, file_path=None, *a, **kw):
        if file_path is None:
            file_path = str(tmp_scores)
        return orig_init(self, file_path, *a, **kw)

    monkeypatch.setattr(scoring.ScoreStore, "__init__", _isolated_init)


@pytest.fixture(autouse=True)
def disable_live_vault(monkeypatch):
    """Disable live Obsidian vault reads during tests.

    context_orchestrator adds an Obsidian (live vault) probe whenever
    OBSIDIAN_API_KEY is set in the environment. Tests that assert "all
    sources down → empty" would become non-deterministic if the developer's
    shell has that key exported. Unset it (and its friends) by default so the
    suite is hermetic; tests that exercise live vault reads set the key via
    monkeypatch themselves.
    """
    monkeypatch.delenv("OBSIDIAN_API_KEY", raising=False)
    monkeypatch.delenv("OBSIDIAN_BASE_URL", raising=False)
    monkeypatch.delenv("OBSIDIAN_SOCKS5", raising=False)


@pytest.fixture(autouse=True)
def verify_lens_module_loads():
    """Verify the lenses module loads correctly after the load-order fix.

    The constants are now defined before the LENS_PROMPTS dict, so a simple
    import should work. This fixture acts as a smoke test.
    """
    from lenses import get_lens_prompt
    from adversarial_reviewer import LensType

    assert get_lens_prompt(LensType.CORRECTNESS)
    assert get_lens_prompt(LensType.SECURITY)
    assert get_lens_prompt(LensType.COMPLETENESS)
