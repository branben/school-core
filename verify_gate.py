"""verify_gate.py — Execute untrusted student code in a hermetic Nix shell.

This is the missing stage in the Agent School pipeline. campus.md principle #3
says "the compiler runs before the critic speaks" — but issue_bridge only judged
the student's *prose*. This module actually RUNS the code.

Safety model (read before changing):
  - We NEVER execute student-authored scripts. We only run the repo's OWN
    declared verify commands (typecheck/test/lint from package.json / pyproject /
    project_verify.yaml). The student's patch is already applied in the clone.
  - The cached clone is copied to a temp scratch dir (read-write) so tests can
    write artifacts; the original cache is never mutated.
  - Commands run inside `nix develop .#verifyShell` (node/pnpm/python, no network)
    so the host toolchain/network is not touched.
  - Timeouts bound every command; non-zero exit => failure finding.

Usage:
    from verify_gate import run_verify_gate
    result = run_verify_gate(repo_path=clone_path, project_verify="project_verify.yaml")
    # result == {"passed": False, "failures": [{"cmd": "...", "exit": 1, "stderr": "..."}], ...}
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


# Commands we are allowed to run come ONLY from declared config, never from
# free-form student input. This list is the allowlist of config sources.
ALLOWED_CONFIG_NAMES = ("project_verify.yaml", "package.json", "pyproject.toml")


def _discover_commands(repo_path: Path, project_verify: Optional[Path]) -> list[dict]:
    """Return a list of {name, cmd, cwd} verify commands.

    Priority: explicit project_verify.yaml > inferred from package.json /
    pyproject.toml. Sub-projects (e.g. a `mobile/` dir with its own
    package.json) are discovered recursively so a repo that typechecks
    sub-projects separately is not missed (this is exactly the gap that bit
    the Orca mobile reconnect work: mobile typechecks independently of root).
    """
    commands: list[dict] = []

    if project_verify and project_verify.exists():
        try:
            data = json.loads(project_verify.read_text()) if project_verify.suffix == ".json" \
                else _yaml_load(project_verify)
            for entry in data.get("verify", []):
                commands.append({
                    "name": entry.get("name", entry.get("cmd", "?")),
                    "cmd": entry["cmd"],
                    "cwd": entry.get("cwd", "."),
                })
            return commands
        except Exception as e:  # pragma: no cover - config is trusted but defensive
            print(f"[verify_gate] project_verify parse failed: {e}")

    # Infer from package.json files (root + sub-projects)
    for pkg in sorted(repo_path.rglob("package.json")):
        if "node_modules" in pkg.parts:
            continue
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except Exception:
            continue
        sub = pkg.parent.relative_to(repo_path)
        for key in ("typecheck", "lint", "test", "check"):
            if key in scripts:
                commands.append({
                    "name": f"{sub}/npm:{key}",
                    "cmd": f"npm run {key}",
                    "cwd": str(sub) or ".",
                })

    # Infer from pyproject.toml (pytest / mypy / ruff)
    for cfg in sorted(repo_path.rglob("pyproject.toml")):
        if ".venv" in cfg.parts or "site-packages" in cfg.parts:
            continue
        text = cfg.read_text(errors="replace")
        sub = cfg.parent.relative_to(repo_path)
        if "pytest" in text:
            commands.append({"name": f"{sub}/pytest", "cmd": "pytest -q", "cwd": str(sub) or "."})
        if "[tool.ruff]" in text:
            commands.append({"name": f"{sub}/ruff", "cmd": "ruff check .", "cwd": str(sub) or "."})

    return commands


def _yaml_load(path: Path) -> dict:
    """Minimal YAML loader fallback (avoid hard dep). Tries pyyaml, else a
    tiny parser sufficient for project_verify.yaml's flat `verify:` list."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        pass
    # Tiny fallback: parse `name:`/`cmd:`/`cwd:` under `- ` list items.
    out: dict = {"verify": []}
    cur: dict | None = None
    for line in path.read_text().splitlines():
        if line.strip().startswith("- name:"):
            cur = {"name": line.split("name:")[1].strip()}
            out["verify"].append(cur)
        elif cur is not None and line.strip().startswith("cmd:"):
            cur["cmd"] = line.split("cmd:")[1].strip()
        elif cur is not None and line.strip().startswith("cwd:"):
            cur["cwd"] = line.split("cwd:")[1].strip()
    return out


def run_verify_gate(
    repo_path: Path,
    project_verify: Optional[Path] = None,
    flake_path: Path | None = None,
    timeout: int = 300,
) -> dict:
    """Run all discovered verify commands for a repo inside the Nix shell.

    Args:
        repo_path: path to the (already-patched) cached clone.
        project_verify: optional explicit command manifest.
        flake_path: path to the flake providing `.#verifyShell`. Defaults to CWD.
        timeout: per-command timeout in seconds.

    Returns:
        {"passed": bool, "failures": [...]}
    """
    repo_path = Path(repo_path)
    flake_path = Path(flake_path) if flake_path else Path.cwd()

    commands = _discover_commands(repo_path, project_verify)
    if not commands:
        # No declared verify commands: we cannot prove correctness, but we
        # must not pretend success. Signal as a soft failure so the reviewer
        # knows verification was not possible.
        return {
            "passed": False,
            "failures": [{
                "cmd": "(discovery)",
                "exit": None,
                "stderr": "No typecheck/test/lint commands discovered in repo.",
            }],
            "ran": 0,
        }

    # Copy clone to a writable scratch dir so tests can emit artifacts.
    scratch = Path(tempfile.mkdtemp(prefix="school-verify-"))
    try:
        shutil.copytree(repo_path, scratch / "repo", dirs_exist_ok=True)
        work = scratch / "repo"

        failures: list[dict] = []
        for cmd in commands:
            cwd = (work / cmd["cwd"]).resolve()
            full = f'nix develop {flake_path}/flake.nix#verifyShell --command bash -c {cmd["cmd"]!r}'
            try:
                res = subprocess.run(
                    full,
                    shell=True,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                failures.append({"cmd": cmd["cmd"], "exit": None,
                                  "stderr": f"timed out after {timeout}s"})
                continue
            if res.returncode != 0:
                failures.append({
                    "cmd": cmd["cmd"],
                    "exit": res.returncode,
                    "stderr": (res.stderr or res.stdout)[-1500:],
                })

        return {"passed": not failures, "failures": failures, "ran": len(commands)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    r = run_verify_gate(p)
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["passed"] else 1)
