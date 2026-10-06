#!/usr/bin/env python3
"""Clean-device bootstrap, doctor, smoke, and durable-state restore for School Core."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

STATUSES = ("ok", "missing", "blocked", "not_configured", "unknown")
BLOCKING_STATUSES = ("missing", "blocked", "not_configured")
MIN_PYTHON = (3, 9)
REPO_ROOT = Path(__file__).resolve().parent


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    remedy: str = ""
    required: bool = True

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"invalid status {self.status!r} for check {self.name!r}")

    def to_dict(self):
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "remedy": self.remedy, "required": self.required}


@dataclass
class Report:
    command: str
    root: str
    checks: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def add(self, check: Check):
        self.checks.append(check)
        return check

    @property
    def blocking(self):
        return [c for c in self.checks if c.required and c.status in BLOCKING_STATUSES]

    @property
    def ok(self):
        return not self.blocking

    def to_dict(self):
        return {"command": self.command, "root": self.root,
                "generated_at": self.generated_at, "ok": self.ok,
                "blocking": [c.to_dict() for c in self.blocking],
                "checks": [c.to_dict() for c in self.checks], "artifacts": self.artifacts}

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    def to_text(self):
        lines = [f"school-core recovery — {self.command}", f"root: {self.root}", ""]
        for check in self.checks:
            marker = "(advisory)" if not check.required else ""
            lines.append(f"  {check.status:<15}{marker:<11} {check.name}: {check.detail or '-'}")
            if check.remedy and check.status in BLOCKING_STATUSES:
                lines.append(f"      remedy: {check.remedy}")
        lines.append("")
        if self.ok:
            lines.append(f"result: OK — {len(self.checks)} checks, no required blockers")
        else:
            names = ", ".join(check.name for check in self.blocking)
            lines.append(f"result: BLOCKED — required checks not ok: {names}")
        return "\n".join(lines)

    def write(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n")
        return path


def _run(cmd, *, cwd=None, timeout=300, env=None):
    try:
        result = subprocess.run([str(part) for part in cmd], cwd=str(cwd) if cwd else None,
                                env=env, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return 127, "", f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(str(part) for part in cmd)}"
    except OSError as exc:
        return 126, "", f"cannot execute {cmd[0]}: {exc}"


def _tail(text: str, n: int = 3) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " | ".join(lines[-n:])


def _venv_python(root: Path) -> Path:
    return root / ".venv" / "bin" / "python"


def _context_dest(root: Path) -> Path:
    return root / "data" / "context"


def _report_path(root: Path, command: str) -> Path:
    return root / "data" / "recovery" / f"{command}-report.json"


def _host_system() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, machine)
    if sys.platform == "darwin":
        os_name = "darwin"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    else:
        os_name = sys.platform
    return f"{arch}-{os_name}"


def check_python() -> Check:
    version = platform.python_version()
    if sys.version_info[:2] >= MIN_PYTHON:
        return Check("python", "ok", f"CPython {version} (>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})")
    return Check("python", "missing", f"CPython {version} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
                 f"install Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")


def check_git() -> Check:
    rc, out, err = _run(["git", "--version"], timeout=15)
    if rc == 0:
        return Check("git", "ok", out.strip() or "git available")
    return Check("git", "missing", _tail(err) or "git not found on PATH",
                 "install git (xcode-select --install, or your package manager)")


def check_venv(root: Path, *, create: bool = False, skip: bool = False) -> Check:
    if skip:
        return Check("venv", "unknown", "skipped (--no-venv)")
    venv_dir, py = root / ".venv", _venv_python(root)
    if not py.is_file():
        if not create:
            return Check("venv", "missing", f"{venv_dir} absent", "run: python3 recovery.py bootstrap")
        rc, _, err = _run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], timeout=600)
        if rc != 0:
            return Check("venv", "blocked", f"venv creation failed: {_tail(err)}",
                         "ensure the python venv module is available (python3 -m venv)")
    rc, out, err = _run([py, "--version"], timeout=30)
    if rc == 0:
        return Check("venv", "ok", f"{out.strip()} at {venv_dir}")
    return Check("venv", "blocked", f"{py} exists but does not run: {_tail(err)}",
                 "delete .venv/ and re-run: python3 recovery.py bootstrap")


def check_deps(root: Path, *, install: bool = False, skip: bool = False) -> Check:
    py = _venv_python(root)
    if skip:
        return Check("deps", "unknown", "skipped (--skip-deps)", required=False)
    if not py.is_file():
        return Check("deps", "unknown", "venv absent — cannot inspect dependencies",
                     "run: python3 recovery.py bootstrap", required=False)
    requirements = root / "requirements.txt"
    if not _has_requirements(requirements):
        return Check("deps", "ok", "no third-party requirements declared", required=False)
    if install:
        rc, _, _ = _run([py, "-m", "pip", "--version"], timeout=120)
        if rc != 0:
            rc, _, err = _run([py, "-m", "ensurepip", "--default-pip"], timeout=900)
            if rc != 0:
                return Check("deps", "blocked", f"pip bootstrap (ensurepip) failed: {_tail(err)}",
                             "run ensurepip manually in .venv, or check the python installation", required=False)
        rc, _, err = _run([py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                           "-r", str(requirements)], cwd=root, timeout=900,
                         env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        if rc != 0:
            return Check("deps", "blocked", f"pip install failed: {_tail(err)}",
                         "re-run with network access, or inspect requirements.txt", required=False)
        return Check("deps", "ok", "declared dependencies installed into .venv", required=False)
    rc, _, err = _run([py, "-c", "import yaml, dotenv"], timeout=60)
    if rc == 0:
        return Check("deps", "ok", "runtime dependencies importable in .venv", required=False)
    return Check("deps", "missing", f"declared dependencies not importable: {_tail(err)}",
                 "run: python3 recovery.py bootstrap", required=False)


def _has_requirements(requirements: Path) -> bool:
    if not requirements.is_file():
        return False
    return any(line.strip() and not line.strip().startswith("#") for line in requirements.read_text().splitlines())


def check_nix(root: Path) -> Check:
    rc, out, err = _run(["nix", "--version"], timeout=15)
    if rc == 0:
        return Check("nix", "ok", out.strip(), required=False)
    return Check("nix", "missing", _tail(err) or "nix not found on PATH",
                 "install Determinate Nix (docs/setup.md Tier A) to run the verify-gate", required=False)


def check_flake_platform(root: Path) -> Check:
    flake = root / "flake.nix"
    if not flake.is_file():
        return Check("flake-platform", "unknown", "no flake.nix to inspect", required=False)
    match = re.search(r'system\s*=\s*"([^"]+)"', flake.read_text())
    if not match:
        return Check("flake-platform", "unknown", "flake.nix declares no pinned system", required=False)
    declared, host = match.group(1), _host_system()
    if declared == host:
        return Check("flake-platform", "ok", f"flake system {declared} matches host", required=False)
    return Check("flake-platform", "not_configured", f"flake.nix pins system = \"{declared}\" but this host is {host}",
                 f"adapt flake.nix (or pass --system) so verifyShell builds on {host}", required=False)


def _context_dest_for(root: Path) -> Path:
    return _context_dest(root)


def check_context(root: Path, *, materialize: bool = False) -> Check:
    manifest, lock = root / "envit.json", root / "envit.lock.json"
    for path in (manifest, lock):
        if not path.is_file():
            return Check("context", "missing", f"{path.name} not found at {path}",
                         "restore the envit manifest/lock from the checkout")
    mdata = None
    try:
        mdata = json.loads(manifest.read_text())
    except json.JSONDecodeError as exc:
        return Check("context", "blocked", f"{manifest.name}: invalid JSON: {exc}", "repair or restore the corrupt manifest")
    try:
        ldata = json.loads(lock.read_text())
    except json.JSONDecodeError as exc:
        return Check("context", "blocked", f"{lock.name}: invalid JSON: {exc}", "repair or restore the corrupt lockfile")
    if not (mdata.get("repos") and ldata.get("repos")):
        return Check("context", "blocked", "envit.json / envit.lock.json declare no pinned repos",
                     "restore a complete envit manifest + lockfile")
    dest = _context_dest(root)
    skills_root = dest / "skills"
    if materialize:
        rc, _, err = _run([sys.executable, str(REPO_ROOT / "scripts" / "verify_context_lock.py"),
                           "--dest", str(dest), "--manifest", str(manifest), "--lock", str(lock)], timeout=1800)
        if rc != 0:
            return Check("context", "blocked", f"context materialization failed: {_tail(err)}",
                         "inspect scripts/verify_context_lock.py output; fix drift or network")
        n_skills = len(list(skills_root.glob("*/SKILL.md"))) if skills_root.is_dir() else 0
        return Check("context", "ok", f"{len(mdata['repos'])} pinned repo(s) + {n_skills} skill(s) materialized to {dest}")
    n_skills = len(list(skills_root.glob("*/SKILL.md"))) if skills_root.is_dir() else 0
    if n_skills:
        return Check("context", "ok", f"materialized: {n_skills} skill(s) under {dest}")
    return Check("context", "missing", f"context not materialized under {dest}", "run: python3 recovery.py bootstrap")


def check_souls(root: Path) -> Check:
    profiles = root / "config" / "profiles"
    souls = sorted(profiles.glob("*/SOUL.md")) if profiles.is_dir() else []
    if not souls:
        return Check("souls", "missing", f"no SOUL.md files under {profiles}", "restore config/profiles/*/SOUL.md from the checkout")
    empty = [s.parent.name for s in souls if not s.read_text().strip()]
    if empty:
        return Check("souls", "blocked", f"empty SOUL.md for profile(s): {', '.join(empty)}", "populate the listed SOUL.md files")
    return Check("souls", "ok", f"{len(souls)} soul(s): {', '.join(s.parent.name for s in souls)}")


def _env_names(root: Path, env):
    names = set(env)
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                names.add(match.group(1))
    return names


def check_secrets(root: Path, env) -> Check:
    example = root / ".env.example"
    if not example.is_file():
        return Check("secrets", "missing", f".env.example not found at {example}", "restore .env.example (the placeholder-only template)")
    required, optional = [], []
    for line in example.read_text().splitlines():
        match = re.match(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not match:
            continue
        key = match.group(1)
        (optional if line.lstrip().startswith("#") else required).append(key)
    present = _env_names(root, env)
    missing = [key for key in required if key not in present]
    detail = ", ".join(f"{key}={'set' if key in present else 'missing'}" for key in required)
    optional_set = sum(key in present for key in optional)
    if missing:
        detail += f"; optional set: {optional_set}/{len(optional)}"
        return Check("secrets", "not_configured", f"required: {detail or 'none declared'}",
                     f"set {', '.join(missing)} in {root / '.env'} or the environment")
    detail = f"required: {detail or 'none declared'}"
    if optional:
        detail += f"; optional set: {optional_set}/{len(optional)}"
    return Check("secrets", "ok", detail)


def _env_values(root: Path, env):
    values = {key: value for key, value in env.items() if isinstance(value, str)}
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
            if match and match.group(1) not in values:
                values[match.group(1)] = match.group(2).strip().strip('"\\\'')
    return values


def _probe_service(url: str, timeout: float = 5.0):
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen
    try:
        with urlopen(url, timeout=timeout) as response:
            return True, f"HTTP {getattr(response, 'status', '?')}"
    except HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except ValueError:
        return False, "invalid URL format"
    except URLError:
        return False, "service unreachable"
    except OSError as exc:
        return False, f"service unavailable ({type(exc).__name__})"


SERVICES = (("omniroute", "OMNIROUTE_BASE"),)


def check_services(root: Path, env) -> Check:
    values = _env_values(root, env)
    missing = []
    notes = []
    for name, key in SERVICES:
        url = values.get(key)
        if not url:
            missing.append(key)
            continue
        reachable, note = _probe_service(url)
        notes.append(f"{name}: {'reachable' if reachable else 'unreachable'} ({note})")
        if not reachable:
            return Check("services", "blocked", "; ".join(notes), f"start or repair {name} (configure {key})")
    if missing:
        return Check("services", "not_configured", f"not configured: {', '.join(missing)}", f"set {', '.join(missing)} locally")
    return Check("services", "ok", "; ".join(notes))


def check_target(root: Path, env) -> Check:
    override = env.get("AGENT_SCHOOL_REPO") or env.get("SCHOOL_REPO")
    if override:
        return Check("target", "ok", f"target repo from environment: {override}")
    rc, out, _ = _run(["git", "-C", str(root), "remote", "get-url", "origin"], timeout=15)
    if rc != 0:
        return Check("target", "not_configured", "no target repository configured",
                     "set AGENT_SCHOOL_REPO=owner/name or clone with an origin remote")
    url = out.strip()
    slug = url[:-4] if url.endswith(".git") else url
    if slug.startswith("git@"):
        slug = slug[4:].replace(":", "/")
    if "github.com/" in slug:
        slug = slug.rsplit("github.com/", 1)[-1]
    return Check("target", "ok", f"target repo self-configured from origin: {slug}")


def check_state(root: Path) -> Check:
    path = root / "data" / "recovery"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("state", "blocked", f"data/recovery not writable: {type(exc).__name__}", "fix permissions on data/")
    return Check("state", "ok", f"durable state dir writable: {root / 'data'}")


def _collect_checks(root: Path, env, *, materialize: bool, create_venv: bool,
                    install_deps: bool, skip_venv: bool, skip_deps: bool):
    return [check_python(), check_git(), check_venv(root, create=create_venv, skip=skip_venv),
            check_deps(root, install=install_deps, skip=skip_deps), check_nix(root),
            check_flake_platform(root), check_context(root, materialize=materialize),
            check_souls(root), check_secrets(root, env), check_services(root, env),
            check_target(root, env), check_state(root)]


def run_bootstrap(root: Path, env=None, *, venv: bool = True, deps: bool = True) -> Report:
    root = Path(root).resolve()
    env = os.environ if env is None else env
    report = Report("bootstrap", str(root))
    for check in _collect_checks(root, env, materialize=True, create_venv=venv,
                                 install_deps=deps, skip_venv=not venv, skip_deps=not deps):
        report.add(check)
    report.write(_report_path(root, "bootstrap"))
    return report


def run_doctor(root: Path, env=None) -> Report:
    root = Path(root).resolve()
    env = os.environ if env is None else env
    report = Report("doctor", str(root))
    for check in _collect_checks(root, env, materialize=False, create_venv=False,
                                 install_deps=False, skip_venv=False, skip_deps=False):
        report.add(check)
    report.write(_report_path(root, "doctor"))
    return report


def _beads_remote(root: Path) -> str | None:
    config = root / ".beads" / "config.yaml"
    if not config.is_file():
        return None
    for line in config.read_text(errors="replace").splitlines():
        match = re.match(r"^\s*sync\.remote:\s*(.*?)\s*(?:#.*)?$", line)
        if match:
            value = match.group(1).strip().strip("\\\"'")
            return value or None
    return None


def _beads_database(root: Path, metadata: dict) -> Path:
    beads = root / ".beads"
    if metadata.get("dolt_mode") == "embedded":
        return beads / "embeddeddolt" / (metadata.get("dolt_database") or "beads")
    return beads / "dolt" / (metadata.get("dolt_database") or "beads")


def _beads_state(root: Path, metadata: dict) -> tuple[str, str]:
    beads = root / ".beads"
    if not beads.is_dir():
        return "missing", ".beads directory absent"
    if metadata.get("backend") != "dolt" or metadata.get("database") not in (None, "dolt"):
        return "blocked", "configured Beads backend is not supported by restore"
    if metadata.get("dolt_mode") not in (None, "embedded", "server"):
        return "blocked", "configured Dolt mode is invalid"
    database = _beads_database(root, metadata)
    repo_state_path = database / ".dolt" / "repo_state.json"
    if not database.exists():
        return "missing", f"Dolt database absent at {database}"
    if not repo_state_path.is_file():
        return "blocked", f"Dolt database directory exists but has no repo state: {repo_state_path}"
    try:
        state = json.loads(repo_state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "blocked", f"Dolt repository state is unreadable or corrupt: {repo_state_path}"
    if not isinstance(state, dict) or not isinstance(state.get("head"), str) or not state.get("head"):
        return "blocked", "Dolt repository state has no valid head"
    remotes = state.get("remotes", {})
    if not isinstance(remotes, dict):
        return "blocked", "Dolt repository remotes are corrupt"
    return "present", str(database)


def _run_bd(root: Path, *args: str, timeout: int = 120):
    return _run(["bd", *args], cwd=root, timeout=timeout)


def _valid_issue_list(root: Path) -> tuple[bool, int, str]:
    rc, out, _ = _run_bd(root, "list", "--json")
    if rc != 0:
        return False, 0, "Beads issue listing failed"
    try:
        issues = json.loads(out)
    except (TypeError, json.JSONDecodeError):
        return False, 0, "Beads issue listing returned invalid JSON"
    if not isinstance(issues, list):
        return False, 0, "Beads issue listing has an invalid shape"
    if not issues:
        return False, 0, "Beads issue database is empty; refusing to claim state recovery"
    return True, len(issues), ""


def _local_restore_check(root: Path, name: str, directory: Path, required_files: tuple[str, ...]) -> Check:
    if not directory.exists():
        return Check(name, "missing", f"durable path absent: {directory}", "restore the directory from its authoritative backup")
    if not directory.is_dir():
        return Check(name, "blocked", f"durable path is not a directory: {directory}")
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    base = directory.resolve()
    escaped = []
    for path in files:
        try:
            path.resolve().relative_to(base)
        except (OSError, ValueError):
            escaped.append(path.name)
    if escaped:
        return Check(name, "blocked", f"symlinked files escape {directory}: {', '.join(escaped[:10])}")
    if not files:
        return Check(name, "missing", f"no durable files found under {directory}", "restore the directory from its authoritative backup")
    missing = [relative for relative in required_files if not any(path.name == relative for path in files)]
    if missing:
        return Check(name, "blocked", f"required evidence missing under {directory}: {', '.join(missing)}")
    unreadable = []
    for path in files:
        try:
            path.stat()
            if path.suffix == ".json":
                json.loads(path.read_text())
            elif path.suffix == ".sqlite3":
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                    row = db.execute("PRAGMA quick_check").fetchone()
                    if not row or row[0] != "ok":
                        raise sqlite3.DatabaseError("quick_check failed")
        except (OSError, json.JSONDecodeError, sqlite3.DatabaseError):
            unreadable.append(path.name)
    if unreadable:
        return Check(name, "blocked", f"unreadable or corrupt files under {directory}: {', '.join(unreadable[:10])}")
    digest = sha256()
    try:
        for path in files:
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    except OSError as exc:
        return Check(name, "blocked", f"could not hash durable files under {directory}: {type(exc).__name__}")
    return Check(name, "ok", f"{len(files)} readable file(s), sha256 {digest.hexdigest()}")


def _check_local_credentials(root: Path, env) -> Check:
    example = root / ".env.example"
    if not example.is_file():
        return Check("secrets", "missing", ".env.example not found")
    values = _env_values(root, env)
    missing = []
    endpoint_keys = {key for _, key in SERVICES}
    for line in example.read_text(errors="replace").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not match:
            continue
        key = match.group(1)
        if key in endpoint_keys:
            continue
        value = values.get(key, "").strip()
        if not value or value.lower().startswith(("your_", "changeme", "placeholder")):
            missing.append(key)
    if missing:
        return Check("secrets", "not_configured", f"missing local credentials: {', '.join(missing)}",
                     f"configure {', '.join(missing)} locally; values are not read into reports")
    return Check("secrets", "ok", "all required credential values are configured")


def _check_local_services(root: Path, env) -> Check:
    values = _env_values(root, env)
    unconfigured = []
    for service, key in SERVICES:
        url = values.get(key, "").strip()
        if not url:
            unconfigured.append(key)
            continue
        reachable, note = _probe_service(url)
        if not reachable:
            return Check("services", "blocked", f"{service} is unreachable ({note}); endpoint omitted")
    if unconfigured:
        return Check("services", "not_configured", f"missing local endpoint configuration: {', '.join(unconfigured)}")
    return Check("services", "ok", "configured local services responded")


def run_restore(root: Path, env=None) -> Report:
    root = Path(root).resolve()
    env = os.environ if env is None else env
    report = Report("restore", str(root))
    remote = _beads_remote(root)
    if not remote:
        report.add(Check("beads", "not_configured", "no sync.remote configured in .beads/config.yaml",
                         "configure sync.remote for the authoritative Beads repository"))
    elif not remote.startswith("git+"):
        report.add(Check("beads", "blocked", "unsupported Beads remote scheme; expected git+ URL"))
    else:
        metadata_path = root / ".beads" / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text())
        except FileNotFoundError:
            metadata = {"backend": "dolt", "dolt_mode": "embedded", "dolt_database": "school_core"}
        except (OSError, json.JSONDecodeError):
            metadata = None
        if metadata is None:
            report.add(Check("beads", "blocked", "Beads metadata is unreadable or corrupt; refusing bootstrap"))
        else:
            state, detail = _beads_state(root, metadata)
            if state == "blocked":
                report.add(Check("beads", "blocked", detail, "repair or recover the local Beads database before syncing"))
            else:
                if state == "missing":
                    rc, out, _ = _run_bd(root, "bootstrap", "--yes", "--json")
                    if rc != 0:
                        report.add(Check("beads", "blocked", "Beads bootstrap from configured remote failed; no local state was accepted"))
                    else:
                        try:
                            bootstrap = json.loads(out)
                        except (TypeError, json.JSONDecodeError):
                            bootstrap = None
                        if not isinstance(bootstrap, dict) or bootstrap.get("action") not in ("sync", "clone", "restore"):
                            report.add(Check("beads", "blocked", "Beads bootstrap did not confirm a remote restore"))
                        else:
                            state, detail = _beads_state(root, metadata)
                            if state != "present":
                                report.add(Check("beads", "blocked", f"bootstrap did not produce a valid local Beads database: {detail}"))
                            else:
                                database = _beads_database(root, metadata)
                                try:
                                    db_state = json.loads((database / ".dolt" / "repo_state.json").read_text())
                                except (OSError, json.JSONDecodeError):
                                    db_state = {}
                                origin = db_state.get("remotes", {}).get("origin", {}) if isinstance(db_state, dict) else {}
                                if origin.get("url") != remote:
                                    report.add(Check("beads", "blocked", "bootstrapped Beads origin does not match configured sync.remote"))
                if not any(check.name == "beads" for check in report.checks):
                    database = _beads_database(root, metadata)
                    try:
                        db_state = json.loads((database / ".dolt" / "repo_state.json").read_text())
                    except (OSError, json.JSONDecodeError):
                        db_state = {}
                    origin = db_state.get("remotes", {}).get("origin", {}) if isinstance(db_state, dict) else {}
                    if origin.get("url") != remote:
                        report.add(Check("beads", "blocked", "local Beads origin does not match configured sync.remote; refusing overwrite"))
                    else:
                        valid, _, error = _valid_issue_list(root)
                        if not valid:
                            report.add(Check("beads", "blocked", error + "; refusing remote sync"))
                if not any(check.name == "beads" for check in report.checks):
                    rc, _, _ = _run_bd(root, "dolt", "pull", "--remote", "origin")
                    if rc != 0:
                        report.add(Check("beads", "blocked", "Beads remote pull failed; local records were not declared recovered"))
                    else:
                        valid, count, error = _valid_issue_list(root)
                        if valid:
                            report.add(Check("beads", "ok", f"configured Beads remote read; {count} local issue(s) validated"))
                        else:
                            report.add(Check("beads", "blocked", error + "; Beads state could not be validated"))

    traj = root / "data" / "trajectories"
    report.add(_local_restore_check(root, "trajectories", traj, ()))
    evidence = root / "data" / "recovery"
    report.add(_local_restore_check(root, "evidence", evidence, ("report.json", "candidate.json", "gate-evidence.json", "state.sqlite3")))
    report.add(_check_local_services(root, env))
    report.add(_check_local_credentials(root, env))
    report.add(check_target(root, env))
    report.artifacts["trajectories"] = {"path": str(traj), "read_only": True}
    report.artifacts["evidence"] = {"path": str(evidence), "read_only": True}
    report.artifacts["beads_remote"] = "configured" if remote else ""
    report.write(_report_path(root, "restore"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="recovery.py", description="Clean-device bootstrap and recovery doctor for School Core.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_boot = sub.add_parser("bootstrap", help="provision this checkout (venv, deps, context)")
    p_boot.add_argument("--root", default=str(REPO_ROOT))
    p_boot.add_argument("--json", action="store_true")
    p_boot.add_argument("--no-venv", action="store_true")
    p_boot.add_argument("--skip-deps", action="store_true")
    p_doc = sub.add_parser("doctor", help="read-only recovery inspection")
    p_doc.add_argument("--root", default=str(REPO_ROOT))
    p_doc.add_argument("--json", action="store_true")
    p_smoke = sub.add_parser("smoke", help="disposable candidate -> declared verification -> evidence flow")
    p_smoke.add_argument("--target", default=None)
    p_smoke.add_argument("--evidence-dir", default=None)
    p_smoke.add_argument("--repository", default="local/recovery-smoke")
    p_smoke.add_argument("--timeout", type=int, default=120)
    p_smoke.add_argument("--json", action="store_true")
    p_restore = sub.add_parser("restore", help="restore Beads and verify local durable state")
    p_restore.add_argument("--root", default=str(REPO_ROOT))
    p_restore.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(getattr(args, "root", REPO_ROOT))
    if args.command == "bootstrap":
        report = run_bootstrap(root, venv=not args.no_venv, deps=not args.skip_deps)
    elif args.command == "doctor":
        report = run_doctor(root)
    elif args.command == "restore":
        report = run_restore(root)
    else:
        from recovery_smoke import run_smoke
        report = run_smoke(target=args.target, evidence_dir=args.evidence_dir,
                           repository=args.repository, timeout=args.timeout)
    if args.json:
        print(report.to_json())
    else:
        print(report.to_text())
        print("report: " + str(report.artifacts.get("report", _report_path(root.resolve(), args.command))))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
