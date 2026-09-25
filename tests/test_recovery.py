"""Tests for recovery.py — the clean-device bootstrap + recovery doctor.

Real boundaries only: temporary project roots, real git repos (a local bare
remote for context materialization, an origin remote for target resolution),
real venv creation, real subprocess CLI invocations. Nothing about the
provisioning steps is mocked. Credentials are simulated via env vars / a .env
file and their VALUES must never appear in any output or persisted report.

Status contract under test:
  ok | missing | blocked | not_configured | unknown
Blocking (exit 1): required checks with status missing/blocked/not_configured.
Service checks run against a REAL local HTTP server (see local_service).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "recovery.py"

ALLOWED_STATUSES = {"ok", "missing", "blocked", "not_configured", "unknown"}


# Keys that must never leak in from the test runner's own environment
# (conftest imports load dotenv into os.environ in full-suite runs).
_SCRUBBED_KEYS = (
    "REQUIRED_KEY", "OPTIONAL_KEY",
    "OMNIROUTE_BASE", "OMNIROUTE_API_KEY",
    "AGENT_SCHOOL_REPO", "SCHOOL_REPO",
)


def run_cli(*args, root, env=None, timeout=55):
    """Invoke the recovery CLI as a real subprocess against a root.

    ``timeout`` is generous by default because the sandbox filesystem can be
    slow; each test keeps its own pytest-timeout marker as the hard bound.
    """
    full_env = dict(os.environ)
    for key in _SCRUBBED_KEYS:
        full_env.pop(key, None)
    for key, value in (env or {}).items():
        if value is None:
            full_env.pop(key, None)
        else:
            full_env[key] = value
    return subprocess.run(
        [sys.executable, str(CLI), *args, "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def local_service():
    """A real local HTTP service standing in for the external gateway."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=10)


@pytest.fixture
def project_root(tmp_path, local_service):
    """A minimal clean-device project root.

    Contains: a local bare git repo used as the pinned context source (with a
    declared skill), envit manifest+lock pinning that repo's commit, a soul
    profile, a .env.example with one required and one optional key, a .env
    pointing at the local test service, and a git checkout with an origin
    remote so target configuration self-resolves.
    """
    # --- pinned context source: real git repo with one commit + skill dir ---
    src = tmp_path / "ctx-src"
    (src / "skills" / "demo-skill").mkdir(parents=True)
    (src / "skills" / "demo-skill" / "SKILL.md").write_text("# demo-skill\n\nreal content\n")
    _git("init", "-q", str(src), cwd=tmp_path)
    _git("-C", str(src), "config", "user.email", "t@example.com", cwd=tmp_path)
    _git("-C", str(src), "config", "user.name", "T", cwd=tmp_path)
    _git("-C", str(src), "add", ".", cwd=tmp_path)
    _git("-C", str(src), "commit", "-qm", "seed", cwd=tmp_path)
    commit = _git("-C", str(src), "rev-parse", "HEAD", cwd=tmp_path).stdout.strip()
    bare = tmp_path / "ctx.git"
    _git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    source = f"file://{bare}"

    root = tmp_path / "project"
    root.mkdir()
    (root / "envit.json").write_text(json.dumps({
        "repos": [{"source": source, "ref": "HEAD", "update": "frozen"}],
        "skills": {source: {"path": "skills", "pick": ["demo-skill"], "modelInvocable": False}},
    }))
    (root / "envit.lock.json").write_text(json.dumps({
        "version": 1,
        "repos": [{"name": "demo", "source": source, "ref": "HEAD", "commit": commit}],
    }))

    soul_dir = root / "config" / "profiles" / "demo"
    soul_dir.mkdir(parents=True)
    (soul_dir / "SOUL.md").write_text("---\ntitle: demo soul\n---\n\nA real soul file.\n")

    (root / ".env.example").write_text(
        "# environment.example\n"
        "REQUIRED_KEY=\n"
        "# OPTIONAL_KEY=\n"
    )
    (root / ".env").write_text(
        f"OMNIROUTE_BASE={local_service}\n"
        "OMNIROUTE_API_KEY=test-key-not-a-secret\n"
    )
    (root / "requirements.txt").write_text("# empty on purpose — tests never hit the network\n")

    # The "checkout" has an origin remote, so target config self-resolves.
    _git("init", "-q", str(root), cwd=tmp_path)
    _git("-C", str(root), "config", "user.email", "t@example.com", cwd=tmp_path)
    _git("-C", str(root), "config", "user.name", "T", cwd=tmp_path)
    _git("-C", str(root), "remote", "add", "origin", "https://github.com/example/demo-target.git", cwd=tmp_path)

    return root


def _report(root, command):
    path = root / "data" / "recovery" / f"{command}-report.json"
    assert path.is_file(), f"expected durable report at {path}"
    return json.loads(path.read_text())


def _by_name(report):
    return {c["name"]: c for c in report["checks"]}


# --- bootstrap ---------------------------------------------------------------

@pytest.mark.timeout(180)
def test_bootstrap_provisions_venv_context_and_durable_evidence(project_root):
    result = run_cli(
        "bootstrap", root=project_root,
        env={"REQUIRED_KEY": "test-required-value"},
        timeout=170,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # real venv
    assert (project_root / ".venv" / "bin" / "python").is_file()
    # real context materialization from the pinned local remote
    assert (project_root / "data" / "context" / "skills" / "demo-skill" / "SKILL.md").is_file()
    assert (project_root / "data" / "context" / "repos" / "demo").is_dir()

    report = _report(project_root, "bootstrap")
    assert report["command"] == "bootstrap"
    assert report["root"] == str(project_root)
    assert report["blocking"] == []
    for check in report["checks"]:
        assert set(check) >= {"name", "status", "detail", "required"}
        assert check["status"] in ALLOWED_STATUSES
    names = _by_name(report)
    for required in ("python", "git", "venv", "deps", "context", "souls", "secrets", "target", "state"):
        assert names[required]["status"] == "ok", names[required]
    for required in ("python", "git", "venv", "context", "souls", "secrets", "target", "state"):
        assert names[required]["required"] is True

    # human-readable output exists and names the persisted report
    assert "bootstrap" in result.stdout
    assert "bootstrap-report.json" in result.stdout


def test_bootstrap_is_idempotent(project_root):
    env = {"REQUIRED_KEY": "v"}
    first = run_cli("bootstrap", "--no-venv", "--skip-deps", root=project_root, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    second = run_cli("bootstrap", "--no-venv", "--skip-deps", root=project_root, env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    report = _report(project_root, "bootstrap")
    assert report["blocking"] == []
    assert _by_name(report)["context"]["status"] == "ok"


def test_bootstrap_reports_missing_credentials_without_leaking_values(project_root):
    # A configured-but-different secret sits in .env; its value must never leak.
    (project_root / ".env").write_text("OPTIONAL_KEY=leak-canary-9f3a\n")
    result = run_cli(
        "bootstrap", "--no-venv", "--skip-deps", root=project_root,
        env={"REQUIRED_KEY": None},
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "leak-canary-9f3a" not in combined
    report = _report(project_root, "bootstrap")
    assert "leak-canary-9f3a" not in json.dumps(report)
    secrets = _by_name(report)["secrets"]
    assert secrets["status"] == "not_configured"
    assert "REQUIRED_KEY" in secrets["detail"]


def test_bootstrap_no_venv_flag_skips_venv_creation(project_root):
    result = run_cli(
        "bootstrap", "--no-venv", "--skip-deps", root=project_root,
        env={"REQUIRED_KEY": "v"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (project_root / ".venv").exists()
    assert _by_name(_report(project_root, "bootstrap"))["venv"]["status"] == "unknown"


def _make_wheel(directory: Path):
    """Build a minimal installable pure-python wheel (offline, no build backend)."""
    import zipfile
    wheel_path = directory / "demo_pkg-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as z:
        z.writestr("demo_pkg/__init__.py", "VALUE = 'installed-by-recovery-test'\n")
        z.writestr("demo_pkg-0.1.dist-info/METADATA",
                   "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 0.1\n")
        z.writestr("demo_pkg-0.1.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr("demo_pkg-0.1.dist-info/RECORD", "")
    return wheel_path


@pytest.mark.timeout(600)
def test_bootstrap_installs_declared_dependencies_for_real(project_root):
    # The one intentionally heavy test: bootstraps pip into the venv and
    # installs a real wheel from requirements.txt (local path — fully offline).
    wheel = _make_wheel(project_root)
    (project_root / "requirements.txt").write_text(f"./{wheel.name}\n")
    result = run_cli(
        "bootstrap", root=project_root,
        env={"REQUIRED_KEY": "test-required-value"},
        timeout=560,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    deps = _by_name(_report(project_root, "bootstrap"))["deps"]
    assert deps["status"] == "ok", deps
    probe = subprocess.run(
        [str(project_root / ".venv" / "bin" / "python"), "-c",
         "import demo_pkg; print(demo_pkg.VALUE)"],
        capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert "installed-by-recovery-test" in probe.stdout


def test_services_unreachable_is_explicit_blocker_without_leaking_url(project_root):
    (project_root / ".env").write_text(
        "OMNIROUTE_BASE=http://127.0.0.1:1\n"
        "OMNIROUTE_API_KEY=svc-canary-2b8d\n"
    )
    result = run_cli("doctor", root=project_root, env={"REQUIRED_KEY": "v"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "svc-canary-2b8d" not in combined
    assert "http://127.0.0.1:1" not in combined
    report = _report(project_root, "doctor")
    services = _by_name(report)["services"]
    assert services["status"] == "blocked"
    assert "omniroute" in services["detail"]
    assert "svc-canary-2b8d" not in json.dumps(report)
    assert "http://127.0.0.1:1" not in json.dumps(report)


def test_services_unconfigured_is_detected_not_silent(project_root):
    (project_root / ".env").unlink()
    result = run_cli("doctor", root=project_root, env={"REQUIRED_KEY": "v"})
    assert result.returncode != 0
    services = _by_name(_report(project_root, "doctor"))["services"]
    assert services["status"] == "not_configured"
    assert "OMNIROUTE_BASE" in services["remedy"]


# --- doctor ------------------------------------------------------------------

@pytest.mark.timeout(120)
def test_doctor_fully_provisioned_is_clean(project_root):
    env = {"REQUIRED_KEY": "test-required-value"}
    boot = run_cli("bootstrap", "--skip-deps", root=project_root, env=env, timeout=110)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    result = run_cli("doctor", root=project_root, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    report = _report(project_root, "doctor")
    assert report["command"] == "doctor"
    assert report["blocking"] == []
    assert _by_name(report)["context"]["status"] == "ok"


def test_doctor_flags_missing_souls_venv_and_context(project_root):
    import shutil
    shutil.rmtree(project_root / "config")
    result = run_cli(
        "doctor", root=project_root,
        env={"REQUIRED_KEY": "v"},
    )
    assert result.returncode != 0
    names = _by_name(_report(project_root, "doctor"))
    assert names["souls"]["status"] == "missing"
    assert names["venv"]["status"] == "missing"
    assert names["context"]["status"] == "missing"


def test_doctor_missing_git_is_reported_not_crashed(project_root):
    empty_bin = project_root / "empty-bin"
    empty_bin.mkdir()
    result = run_cli(
        "doctor", root=project_root,
        env={"PATH": str(empty_bin), "REQUIRED_KEY": "v"},
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    names = _by_name(_report(project_root, "doctor"))
    assert names["git"]["status"] == "missing"
    assert names["git"]["remedy"]


def test_corrupt_lock_is_precise_blocker(project_root):
    (project_root / "envit.lock.json").write_text("{not json")
    result = run_cli("doctor", root=project_root, env={"REQUIRED_KEY": "v"})
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    names = _by_name(_report(project_root, "doctor"))
    assert names["context"]["status"] == "blocked"
    assert "envit.lock.json" in names["context"]["detail"]


def test_doctor_json_output_matches_contract(project_root):
    env = {"REQUIRED_KEY": "v"}
    result = run_cli("doctor", "--json", root=project_root, env=env)
    assert result.returncode != 0  # nothing provisioned yet
    payload = json.loads(result.stdout)
    assert payload["command"] == "doctor"
    assert isinstance(payload["checks"], list) and payload["checks"]
    for check in payload["checks"]:
        assert check["status"] in ALLOWED_STATUSES
    assert "leak" not in result.stdout


def test_doctor_deps_probe_is_advisory_when_uninstalled(project_root):
    env = {"REQUIRED_KEY": "v"}
    boot = run_cli("bootstrap", "--skip-deps", root=project_root, env=env)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    (project_root / "requirements.txt").write_text("PyYAML>=6.0\n")
    result = run_cli("doctor", root=project_root, env=env)
    assert result.returncode == 0  # deps is advisory, never a hard blocker
    deps = _by_name(_report(project_root, "doctor"))["deps"]
    assert deps["status"] == "missing"
    assert deps["required"] is False


def test_secrets_values_never_printed_even_when_configured(project_root):
    secret = "sk-canary-do-not-print-7c1e"
    (project_root / ".env").write_text(f"REQUIRED_KEY={secret}\n")
    result = run_cli("bootstrap", "--no-venv", "--skip-deps", root=project_root)
    combined = result.stdout + result.stderr
    assert secret not in combined
    assert secret not in json.dumps(_report(project_root, "bootstrap"))
    assert _by_name(_report(project_root, "bootstrap"))["secrets"]["status"] == "ok"


def test_bootstrap_reports_missing_manifest_precisely(tmp_path):
    root = tmp_path / "empty-root"
    root.mkdir()
    result = run_cli("bootstrap", "--no-venv", "--skip-deps", root=root)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    names = _by_name(_report(root, "bootstrap"))
    assert names["context"]["status"] == "missing"
    assert "envit.json" in names["context"]["detail"]
