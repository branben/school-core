"""Test: setup.sh is idempotent, non-destructive, and works without Orca."""
import os
import sys
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "setup.sh"


def run_setup(env=None, cwd=None, extra_args=None):
    cmd = ["bash", str(SETUP_SH)]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or str(REPO_ROOT),
        env=env,
        timeout=60,
    )


class TestProfileTemplatesExist:
    EXPECTED_PERSONAS = [
        "student-coder", "student-reviewer", "student-searcher",
        "student-browser", "student-executor", "student-ci",
        "student-designer", "student-whymage",
    ]

    def test_all_8_templates_exist(self):
        templates_dir = REPO_ROOT / "config" / "profiles" / "_TEMPLATES"
        for persona in self.EXPECTED_PERSONAS:
            soul = templates_dir / persona / "SOUL.md"
            assert soul.exists(), f"Missing template: {soul}"
            assert len(soul.read_text()) > 100

    def test_templates_use_placeholders(self):
        templates_dir = REPO_ROOT / "config" / "profiles" / "_TEMPLATES"
        for persona in self.EXPECTED_PERSONAS:
            soul = templates_dir / persona / "SOUL.md"
            if soul.exists():
                content = soul.read_text()
                assert "/Users/brandonbennett" not in content
                assert "Brandon" not in content


class TestSetupCreatesProfiles:
    def test_creates_all_8_profiles(self):
        tmpdir = tempfile.mkdtemp()
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["SETUP_NONINTERACTIVE"] = "1"
        env["SETUP_SKIP_DEPS"] = "1"
        result = run_setup(env=env)
        profiles_dir = Path(tmpdir) / ".hermes" / "profiles"
        assert profiles_dir.exists(), "profiles dir should be created"
        assert len(list(profiles_dir.iterdir())) == 8


class TestSetupDryRun:
    def test_dry_run_skips_creation(self):
        tmpdir = tempfile.mkdtemp()
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["SETUP_SKIP_DEPS"] = "1"
        run_setup(env=env, extra_args=["--dry-run"])
        profiles_dir = Path(tmpdir) / ".hermes" / "profiles"
        if profiles_dir.exists():
            assert list(profiles_dir.iterdir()) == []


class TestExistingProfilePreservation:
    def test_existing_not_overwritten_noninteractive(self):
        tmpdir = tempfile.mkdtemp()
        profiles_dir = Path(tmpdir) / ".hermes" / "profiles"
        profiles_dir.mkdir(parents=True)
        existing = profiles_dir / "student-coder"
        existing.mkdir()
        (existing / "SOUL.md").write_text("# MY CUSTOM SOUL")

        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["SETUP_NONINTERACTIVE"] = "1"
        env["SETUP_SKIP_DEPS"] = "1"
        run_setup(env=env)

        assert "MY CUSTOM SOUL" in (existing / "SOUL.md").read_text()


class TestOrcaOptional:
    def test_no_orca_does_not_fail(self):
        tmpdir = tempfile.mkdtemp()
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["PATH"] = "/usr/bin:/bin"
        env["SETUP_SKIP_DEPS"] = "1"
        result = run_setup(env=env)
        assert result.returncode == 0, f"Failed: {result.stderr}"


class TestCleanSetup:
    def test_exits_zero(self):
        tmpdir = tempfile.mkdtemp()
        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["SETUP_NONINTERACTIVE"] = "1"
        env["SETUP_SKIP_DEPS"] = "1"
        result = run_setup(env=env)
        assert result.returncode == 0, f"Failed: {result.stderr}"
