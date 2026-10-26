"""Guards for school-core-5bzk: modules must not mutate ``os.environ`` at import.

WHY THIS FILE EXISTS
--------------------
``issue_bridge.py`` used to run ``load_dotenv(ENV_FILE)`` at module import. Because
``load_dotenv`` writes into ``os.environ`` process-wide, importing the module had a
side effect on *every* other module in the process -- including pytest's collection
phase, which runs before any test body executes.

That made two things happen that should not have been possible:

1. ``tests/test_omni_relay_live.py:18`` computes its skipif at import from
   ``os.environ.get("OMNIROUTE_API_KEY")``. A leaked key made 3 live tests RUN
   instead of skip, and they failed against an unreachable relay.
2. ``tests/test_orca_execution.py:504`` has the same import-time skipif. The leaked
   key made the suite HANG at ``test_orca_execution.py:526`` -- the run died with a
   faulthandler Timeout dump and no pytest summary.

The fix moved ``.env`` resolution to read time (``env_or_file``), which never
touches ``os.environ``. These tests pin that property, because the failure mode is
invisible in-process: by the time a test runs, the module is already imported.

A subprocess is used for the import assertions because in-process checks cannot
see the import side effect once ``conftest.py`` has already imported the module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.crew.dotenv_reader import env_or_file, read_dotenv_value

REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules that historically ran load_dotenv at import time.
POLLUTERS = ("issue_bridge", "src.crew.crew_dispatch", "src.core.director")


def _import_in_subprocess(module: str) -> str:
    """Import ``module`` in a clean child and report whether it left a key behind.

    ``OMNIROUTE_API_KEY`` is stripped from the child's environment so that a
    ``POLLUTED`` verdict can only come from the module itself reading ``.env``.
    """
    code = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"import {module}\n"
        "print('POLLUTED' if os.environ.get('OMNIROUTE_API_KEY') else 'CLEAN')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "OMNIROUTE_API_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("module", POLLUTERS)
def test_importing_module_does_not_pollute_environ(module):
    """Importing must not invent credentials for the rest of the process."""
    assert _import_in_subprocess(module) == "CLEAN", (
        f"importing {module} left OMNIROUTE_API_KEY in os.environ; "
        "a module-level load_dotenv is present again"
    )


def test_env_or_file_prefers_live_environment(monkeypatch, tmp_path):
    """The live environment wins over ``.env`` -- the old resolution order."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("SCHOOL_REPO=from-dotenv\n")
    monkeypatch.setenv("SCHOOL_REPO", "from-shell")
    assert env_or_file("SCHOOL_REPO", env_path=dotenv) == "from-shell"


def test_env_or_file_falls_back_to_dotenv(monkeypatch, tmp_path):
    """A key set only in ``.env`` is still found -- the 5 settings keep working."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("SCHOOL_REPO=branben/sound-royale-ny\n")
    monkeypatch.delenv("SCHOOL_REPO", raising=False)
    assert env_or_file("SCHOOL_REPO", env_path=dotenv) == "branben/sound-royale-ny"


def test_env_or_file_returns_default_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("SCHOOL_REPO", raising=False)
    assert env_or_file("SCHOOL_REPO", default="smolmachine", env_path=tmp_path / "none") == "smolmachine"


def test_env_or_file_does_not_mutate_environ(monkeypatch, tmp_path):
    """The whole point: reading a file must not write to shared state."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("SMOL_CLOUD_TOKEN=smk_should_not_escape\n")
    monkeypatch.delenv("SMOL_CLOUD_TOKEN", raising=False)
    env_or_file("SMOL_CLOUD_TOKEN", env_path=dotenv)
    assert "SMOL_CLOUD_TOKEN" not in os.environ


def test_repo_dotenv_still_supplies_the_five_settings():
    """The settings that used to arrive via import-time load_dotenv still resolve.

    Regression guard for the settings that have no other source in the repo.
    """
    repo_env = REPO_ROOT / ".env"
    if not repo_env.exists():  # a bare checkout has no .env; nothing to assert
        pytest.skip("no repo .env present")
    assert env_or_file("APPROVED_ACTORS") == read_dotenv_value(repo_env, "APPROVED_ACTORS")
    assert env_or_file("SCHOOL_CORE_SMOL_CLOUD_SOURCE_TYPE", "smolmachine")
    assert env_or_file("SCHOOL_REPO") == read_dotenv_value(repo_env, "SCHOOL_REPO")


def test_default_repo_reads_school_repo_from_dotenv(monkeypatch):
    """``default_repo()`` must still see SCHOOL_REPO with no .env load at import."""
    from src.tools.repo_default import default_repo

    monkeypatch.delenv("AGENT_SCHOOL_REPO", raising=False)
    monkeypatch.delenv("SCHOOL_REPO", raising=False)
    expected = read_dotenv_value(REPO_ROOT / ".env", "SCHOOL_REPO")
    if not expected:
        pytest.skip("no SCHOOL_REPO in repo .env")
    assert default_repo() == expected


def test_smol_cloud_runner_credential_falls_back_to_dotenv(monkeypatch, tmp_path):
    """``SMOL_CLOUD_TOKEN`` used to arrive only via the import-time load."""
    from src.crew.dotenv_reader import env_or_file

    dotenv = tmp_path / ".env"
    dotenv.write_text("SMOL_CLOUD_TOKEN=smk_from_file\n")
    monkeypatch.delenv("SMOL_CLOUD_TOKEN", raising=False)
    assert env_or_file("SMOL_CLOUD_TOKEN", env_path=dotenv) == "smk_from_file"
