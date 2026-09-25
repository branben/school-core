#!/usr/bin/env python3
"""Clean-device bootstrap and recovery doctor for School Core.

One executable entrypoint that turns a fresh checkout into a working runtime —
or reports precise, machine-readable blockers when it cannot. Every step does
real work against real boundaries (filesystem, subprocesses, git); nothing
here prints placeholder guidance as a substitute for setup.

Commands
  bootstrap   Provision the project: create the virtual environment, install
              declared dependencies, materialize/verify the pinned envit
              context (scripts/verify_context_lock.py), and validate souls,
              secrets, target configuration, and state paths. Non-destructive:
              it only creates/updates project-scoped paths (``.venv/``,
              ``data/context/``, ``data/recovery/``).
  smoke       Disposable end-to-end recovery smoke against a real temporary
              (or --target) Git repository: candidate -> declared local
              verification -> exact identity -> durable evidence + state
              journal (see recovery_smoke.py). No GitHub/Beads mutation.
  doctor      Read-only inspection of the same surfaces plus tool
              availability (git, nix, flake platform). Detects missing tools,
              services, state, context, souls, and target configuration.

Service checks probe configured endpoints (currently the OmniRoute gateway)
for reachability and report them as explicit blockers when missing or down —
never as silent success. Endpoint URLs and credential values are never printed.

Status contract (every check carries one):
  ok             verified working
  missing        a required thing is absent (tool, file, environment)
  blocked       present but unusable (corrupt input, failed subprocess)
  not_configured  external configuration/credentials absent
  unknown       deliberately skipped or not inspectable here

Exit codes:
  0   no required check is missing/blocked/not_configured
  1   at least one required check is a blocker (details in the report)
  2   usage error (argparse)

Both commands persist their full report to
``data/recovery/<command>-report.json`` so evidence survives process exit.

Secret safety: credential checks inspect NAMES only (from ``.env.example``,
the environment, and ``.env``). Values are never stored in check details,
never printed, and never written to the persisted report.

Pure stdlib, mirrors scripts/verify_context_lock.py convention.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("ok", "missing", "blocked", "not_configured", "unknown")
BLOCKING_STATUSES = ("missing", "blocked", "not_configured")

MIN_PYTHON = (3, 9)

REPO_ROOT = Path(__file__).resolve().parent


# --- result model ------------------------------------------------------------

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
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
            "required": self.required,
        }


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
        return {
            "command": self.command,
            "root": self.root,
            "generated_at": self.generated_at,
            "ok": self.ok,
            "blocking": [c.to_dict() for c in self.blocking],
            "checks": [c.to_dict() for c in self.checks],
            "artifacts": self.artifacts,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    def to_text(self):
        lines = [f"school-core recovery — {self.command}", f"root: {self.root}", ""]
        for c in self.checks:
            marker = "" if c.required else "(advisory)"
            lines.append(f"  {c.status:<15}{marker:<11} {c.name}: {c.detail or '-'}")
            if c.remedy and c.status in BLOCKING_STATUSES:
                lines.append(f"      remedy: {c.remedy}")
        lines.append("")
        if self.ok:
            lines.append(f"result: OK — {len(self.checks)} checks, no required blockers")
        else:
            names = ", ".join(c.name for c in self.blocking)
            lines.append(f"result: BLOCKED — required checks not ok: {names}")
        return "\n".join(lines)

    def write(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n")
        return path


# --- low-level helpers -------------------------------------------------------

def _run(cmd, *, cwd=None, timeout=300, env=None):
    """Run a command, returning (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(str(c) for c in cmd)}"
    except OSError as e:
        return 126, "", f"cannot execute {cmd[0]}: {e}"


def _tail(text: str, n: int = 3) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
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
        ost = "darwin"
    elif sys.platform.startswith("linux"):
        ost = "linux"
    else:
        ost = sys.platform
    return f"{arch}-{ost}"


# --- individual checks -------------------------------------------------------

def check_python() -> Check:
    v = sys.version_info
    version = platform.python_version()
    if (v.major, v.minor) >= MIN_PYTHON:
        return Check("python", "ok", f"CPython {version} (>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})")
    return Check(
        "python", "missing",
        f"CPython {version} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        f"install Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def check_git() -> Check:
    rc, out, err = _run(["git", "--version"], timeout=15)
    if rc == 0:
        return Check("git", "ok", out.strip() or "git available")
    return Check("git", "missing", _tail(err) or "git not found on PATH",
                 "install git (xcode-select --install, or your package manager)")


def check_venv(root: Path, *, create: bool = False, skip: bool = False) -> Check:
    if skip:
        return Check("venv", "unknown", "skipped (--no-venv)")
    venv_dir = root / ".venv"
    py = _venv_python(root)
    if not py.is_file():
        if not create:
            return Check("venv", "missing", f"{venv_dir} absent",
                         "run: python3 recovery.py bootstrap")
        # --without-pip keeps venv creation fast and offline; pip itself is
        # bootstrapped on demand by check_deps when dependencies are installed.
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
    """Declared dependencies in the project venv (advisory: auto-installable)."""
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
        # The venv is created --without-pip; bootstrap pip first if needed.
        rc, _, _ = _run([py, "-m", "pip", "--version"], timeout=120)
        if rc != 0:
            rc, _, err = _run([py, "-m", "ensurepip", "--default-pip"], timeout=900)
            if rc != 0:
                return Check("deps", "blocked", f"pip bootstrap (ensurepip) failed: {_tail(err)}",
                             "run ensurepip manually in .venv, or check the python installation",
                             required=False)
        rc, _, err = _run(
            [py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
             "-r", str(requirements)],
            cwd=root,  # relative requirement paths resolve against the project root
            timeout=900,
            env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        if rc != 0:
            return Check("deps", "blocked", f"pip install failed: {_tail(err)}",
                         "re-run with network access, or inspect requirements.txt",
                         required=False)
        return Check("deps", "ok", "declared dependencies installed into .venv", required=False)
    # Read-only path (doctor): probe the runtime imports the framework actually
    # loads (issue_bridge/director: PyYAML + python-dotenv).
    rc, _, err = _run([py, "-c", "import yaml, dotenv"], timeout=60)
    if rc == 0:
        return Check("deps", "ok", "runtime dependencies importable in .venv", required=False)
    return Check("deps", "missing", f"declared dependencies not importable: {_tail(err)}",
                 "run: python3 recovery.py bootstrap", required=False)


def _has_requirements(requirements: Path) -> bool:
    if not requirements.is_file():
        return False
    for line in requirements.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return True
    return False


def check_nix(root: Path) -> Check:
    rc, out, err = _run(["nix", "--version"], timeout=15)
    if rc == 0:
        return Check("nix", "ok", out.strip(), required=False)
    return Check("nix", "missing", _tail(err) or "nix not found on PATH",
                 "install Determinate Nix (docs/setup.md Tier A) to run the verify-gate",
                 required=False)


def check_flake_platform(root: Path) -> Check:
    flake = root / "flake.nix"
    if not flake.is_file():
        return Check("flake-platform", "unknown", "no flake.nix to inspect", required=False)
    match = re.search(r'system\s*=\s*"([^"]+)"', flake.read_text())
    if not match:
        return Check("flake-platform", "unknown", "flake.nix declares no pinned system",
                     required=False)
    declared = match.group(1)
    host = _host_system()
    if declared == host:
        return Check("flake-platform", "ok", f"flake system {declared} matches host", required=False)
    return Check(
        "flake-platform", "not_configured",
        f"flake.nix pins system = \"{declared}\" but this host is {host}",
        f'adapt flake.nix (or pass --system) so verifyShell builds on {host}',
        required=False,
    )


def check_context(root: Path, *, materialize: bool = False) -> Check:
    manifest = root / "envit.json"
    lock = root / "envit.lock.json"
    for path in (manifest, lock):
        if not path.is_file():
            return Check("context", "missing", f"{path.name} not found at {path}",
                         "restore the envit manifest/lock from the checkout")
    for path in (manifest, lock):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            return Check("context", "blocked", f"{path.name}: invalid JSON: {e}",
                         "repair or restore the corrupt file from version control")
        if path is manifest:
            mdata = data
        else:
            ldata = data
    if not (mdata.get("repos") and ldata.get("repos")):
        return Check("context", "blocked",
                     "envit.json / envit.lock.json declare no pinned repos",
                     "restore a complete envit manifest + lockfile")

    dest = _context_dest(root)
    skills_root = dest / "skills"
    if materialize:
        rc, out, err = _run(
            [sys.executable, str(REPO_ROOT / "scripts" / "verify_context_lock.py"),
             "--dest", str(dest), "--manifest", str(manifest), "--lock", str(lock)],
            timeout=1800,
        )
        if rc != 0:
            return Check("context", "blocked",
                         f"context materialization failed: {_tail(err)}",
                         "inspect scripts/verify_context_lock.py output; fix drift or network")
        n_skills = len(list(skills_root.glob("*/SKILL.md"))) if skills_root.is_dir() else 0
        return Check("context", "ok",
                     f"{len(mdata['repos'])} pinned repo(s) + {n_skills} skill(s) materialized to {dest}")
    n_skills = len(list(skills_root.glob("*/SKILL.md"))) if skills_root.is_dir() else 0
    if n_skills:
        return Check("context", "ok", f"materialized: {n_skills} skill(s) under {dest}")
    return Check("context", "missing", f"context not materialized under {dest}",
                 "run: python3 recovery.py bootstrap")


def check_souls(root: Path) -> Check:
    profiles = root / "config" / "profiles"
    souls = sorted(profiles.glob("*/SOUL.md")) if profiles.is_dir() else []
    if not souls:
        return Check("souls", "missing",
                     f"no SOUL.md files under {profiles}",
                     "restore config/profiles/*/SOUL.md from the checkout")
    empty = [s.parent.name for s in souls if not s.read_text().strip()]
    if empty:
        return Check("souls", "blocked",
                     f"empty SOUL.md for profile(s): {', '.join(empty)}",
                     "populate the listed SOUL.md files")
    names = ", ".join(s.parent.name for s in souls)
    return Check("souls", "ok", f"{len(souls)} soul(s): {names}")


def _env_names(root: Path, env):
    """Names (never values) of configured environment keys from env + .env."""
    names = set(env)
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                names.add(match.group(1))
    return names


def check_secrets(root: Path, env) -> Check:
    """Required credentials from .env.example — names and set/missing only."""
    example = root / ".env.example"
    if not example.is_file():
        return Check("secrets", "missing", f".env.example not found at {example}",
                     "restore .env.example (the placeholder-only template)")
    required, optional = [], []
    for line in example.read_text().splitlines():
        match = re.match(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not match:
            continue
        key = match.group(1)
        if line.lstrip().startswith("#"):
            optional.append(key)
        else:
            required.append(key)
    present = _env_names(root, env)
    missing = [k for k in required if k not in present]
    req_state = ", ".join(f"{k}={'set' if k in present else 'missing'}" for k in required)
    opt_set = [k for k in optional if k in present]
    detail = f"required: {req_state or 'none declared'}"
    if optional:
        detail += f"; optional set: {len(opt_set)}/{len(optional)}"
    if missing:
        return Check("secrets", "not_configured", detail,
                     f"set {', '.join(missing)} in {root / '.env'} or the environment")
    return Check("secrets", "ok", detail)


def _env_values(root: Path, env):
    """Environment values from env + .env. Used ONLY for probing; values must
    never be formatted into check details, output, or reports."""
    values = {k: v for k, v in env.items() if isinstance(v, str)}
    dotenv = root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
            if match and match.group(1) not in values:
                values[match.group(1)] = match.group(2).strip().strip('"\'')
    return values


# External services the pipeline depends on: (service name, endpoint env key).
SERVICES = (("omniroute", "OMNIROUTE_BASE"),)


def _probe_service(url: str, timeout: float = 5.0):
    """Return (reachable, note). Any HTTP response counts as reachable.
    The note never includes the URL."""
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen
    try:
        with urlopen(url, timeout=timeout) as resp:
            return True, f"HTTP {getattr(resp, 'status', '?')}"
    except HTTPError as e:
        return True, f"HTTP {e.code}"
    except ValueError:
        return False, "invalid URL format"
    except URLError as e:
        return False, f"URLError: {getattr(e, 'reason', '')}"
    except OSError as e:
        return False, f"OSError: {e}"


def check_services(root: Path, env) -> Check:
    """External service reachability — explicit blockers, never silent success."""
    values = _env_values(root, env)
    unconfigured, notes = [], []
    for name, key in SERVICES:
        url = values.get(key)
        if not url:
            unconfigured.append(name)
            continue
        reachable, note = _probe_service(url)
        notes.append(f"{name}: {'reachable' if reachable else 'unreachable'} ({note})")
        if not reachable:
            return Check("services", "blocked", "; ".join(notes),
                         f"start or repair the {name} service (configure {key})")
    if unconfigured:
        missing = ", ".join(unconfigured)
        keys = ", ".join(k for n, k in SERVICES if n in unconfigured)
        return Check("services", "not_configured",
                     f"not configured: {missing}" + (f"; {'; '.join(notes)}" if notes else ""),
                     f"set {keys} in {root / '.env'} or the environment")
    return Check("services", "ok", "; ".join(notes))


def check_target(root: Path, env) -> Check:
    """Target repository: explicit env override, else the checkout's origin."""
    override = env.get("AGENT_SCHOOL_REPO") or env.get("SCHOOL_REPO")
    if override:
        return Check("target", "ok", f"target repo from environment: {override}")
    rc, out, _ = _run(["git", "-C", str(root), "remote", "get-url", "origin"], timeout=15)
    if rc == 0:
        url = out.strip()
        slug = url[:-4] if url.endswith(".git") else url
        if slug.startswith("git@"):
            slug = slug[4:].replace(":", "/")
        slug = slug.rsplit("github.com/", 1)[-1] if "github.com/" in slug else slug
        return Check("target", "ok", f"target repo self-configured from origin: {slug}")
    return Check("target", "not_configured",
                 "no target repository configured (no AGENT_SCHOOL_REPO/SCHOOL_REPO, no origin remote)",
                 "set AGENT_SCHOOL_REPO=owner/name or clone with an origin remote")


def check_state(root: Path) -> Check:
    data = root / "data"
    try:
        (data / "recovery").mkdir(parents=True, exist_ok=True)
        probe = data / "recovery" / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return Check("state", "blocked", f"data/ not writable: {e}",
                     "fix permissions on the project data directory")
    return Check("state", "ok", f"durable state dir writable: {data}")


# --- command flows -----------------------------------------------------------

def _collect_checks(root: Path, env, *, materialize: bool, create_venv: bool,
                    install_deps: bool, skip_venv: bool, skip_deps: bool):
    report_checks = [
        check_python(),
        check_git(),
        check_venv(root, create=create_venv, skip=skip_venv),
        check_deps(root, install=install_deps, skip=skip_deps),
        check_nix(root),
        check_flake_platform(root),
        check_context(root, materialize=materialize),
        check_souls(root),
        check_secrets(root, env),
        check_services(root, env),
        check_target(root, env),
        check_state(root),
    ]
    return report_checks


def run_bootstrap(root: Path, env=None, *, venv: bool = True, deps: bool = True) -> Report:
    root = Path(root).resolve()
    env = os.environ if env is None else env
    report = Report("bootstrap", str(root))
    for check in _collect_checks(
        root, env,
        materialize=True, create_venv=venv, install_deps=deps,
        skip_venv=not venv, skip_deps=not deps,
    ):
        report.add(check)
    report.write(_report_path(root, "bootstrap"))
    return report


def run_doctor(root: Path, env=None) -> Report:
    root = Path(root).resolve()
    env = os.environ if env is None else env
    report = Report("doctor", str(root))
    for check in _collect_checks(
        root, env,
        materialize=False, create_venv=False, install_deps=False,
        skip_venv=False, skip_deps=False,
    ):
        report.add(check)
    report.write(_report_path(root, "doctor"))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="recovery.py",
        description="Clean-device bootstrap and recovery doctor for School Core.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_boot = sub.add_parser("bootstrap", help="provision this checkout (venv, deps, context)")
    p_boot.add_argument("--root", default=str(REPO_ROOT), help="project root (default: this checkout)")
    p_boot.add_argument("--json", action="store_true", help="print the machine-readable report only")
    p_boot.add_argument("--no-venv", action="store_true", help="skip virtualenv creation")
    p_boot.add_argument("--skip-deps", action="store_true", help="skip dependency installation")

    p_doc = sub.add_parser("doctor", help="read-only recovery inspection")
    p_doc.add_argument("--root", default=str(REPO_ROOT), help="project root (default: this checkout)")
    p_doc.add_argument("--json", action="store_true", help="print the machine-readable report only")

    p_smoke = sub.add_parser(
        "smoke",
        help="disposable candidate -> declared verification -> exact identity -> durable evidence flow",
    )
    p_smoke.add_argument("--target", default=None,
                         help="existing target Git repository (default: create a disposable one)")
    p_smoke.add_argument("--evidence-dir", default=None,
                         help="where durable evidence is written (default: data/recovery/smoke-<ts>)")
    p_smoke.add_argument("--repository", default="local/recovery-smoke",
                         help="repository slug recorded in the candidate manifest")
    p_smoke.add_argument("--timeout", type=int, default=120,
                         help="per-command verification timeout in seconds")
    p_smoke.add_argument("--json", action="store_true", help="print the machine-readable report only")

    args = parser.parse_args(argv)
    root = Path(getattr(args, "root", REPO_ROOT))

    if args.command == "bootstrap":
        report = run_bootstrap(root, venv=not args.no_venv, deps=not args.skip_deps)
    elif args.command == "doctor":
        report = run_doctor(root)
    else:
        from recovery_smoke import run_smoke
        report = run_smoke(target=args.target, evidence_dir=args.evidence_dir,
                           repository=args.repository, timeout=args.timeout)

    if args.json:
        print(report.to_json())
    else:
        print(report.to_text())
        print("report: " + report.artifacts.get("report", str(_report_path(root.resolve(), args.command))))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
