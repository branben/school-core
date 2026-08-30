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
  - If Nix is unavailable, this reusable library returns a loud SKIPPED
    verdict (`skipped: True`) instead of reporting fake compile failures — a
    missing toolchain must never masquerade as a failing build. The production
    GitHub Actions school-loop performs a separate hard preflight before issue
    execution, so it fails the execute job when Nix or `verifyShell` is absent.
    Under `VERIFY_GATE_STRICT=1`, an unrunnable library gate escalates to
    `skipped: False` + `strict_escalated: True` so the issue cannot pass
    unverified (compiler-before-critic enforced).
  - Timeouts bound every command; non-zero exit => failure finding.

Usage:
    from verify_gate import run_verify_gate
    result = run_verify_gate(repo_path=clone_path, project_verify="project_verify.yaml")
    # result == {"passed": False, "failures": [{"cmd": "...", "exit": 1, "stderr": "..."}], ...}
"""

from __future__ import annotations

import json
import os
import shlex
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

    Priority: explicit project_verify.yaml > the repo's own root
    project_verify.yaml (auto-probed when no explicit manifest is given) >
    inferred from package.json / pyproject.toml. Sub-projects (e.g. a
    `mobile/` dir with its own package.json) are discovered recursively so a
    repo that typechecks sub-projects separately is not missed (this is
    exactly the gap that bit the Orca mobile reconnect work: mobile
    typechecks independently of root).
    """
    commands: list[dict] = []

    if project_verify is None:
        default_manifest = repo_path / "project_verify.yaml"
        project_verify = default_manifest if default_manifest.exists() else None

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
        # Detect package manager from lockfile
        if (pkg.parent / "pnpm-lock.yaml").exists():
            runner = "pnpm"
        elif (pkg.parent / "yarn.lock").exists():
            runner = "yarn"
        else:
            runner = "npm"
        for key in ("typecheck", "lint", "test", "check"):
            if key in scripts:
                commands.append({
                    "name": f"{sub}/{runner}:{key}",
                    "cmd": f"{runner} run {key}" if runner != "pnpm" else f"pnpm {key}",
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


# Scratch-copy noise the verify commands never need (VCS metadata, venvs,
# caches). node_modules is included when present in the cache (installed by
# repo_reader.clone_repo for TypeScript projects) so the hermetic gate can
# run typecheck/test/lint without network access.
_VERIFY_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".svn", ".venv", "venv", "env",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".hypothesis", ".coverage", "htmlcov", ".DS_Store",
)

def _has_node_modules(repo_path: Path) -> bool:
    """Check if the repo has a node_modules directory (pre-installed by clone_repo)."""
    return (repo_path / "node_modules").is_dir()


def _find_nix() -> Optional[str]:
    """Locate a usable nix binary: PATH first, then the standard Determinate path.

    The school-loop runner uses Determinate Nix, whose binary lives at
    /nix/var/nix/profiles/default/bin/nix — not always on PATH for non-login
    shells — so we probe that location as a fallback.
    """
    which = shutil.which("nix")
    if which:
        return which
    determinate = Path("/nix/var/nix/profiles/default/bin/nix")
    return str(determinate) if determinate.exists() else None


def _skipped_verdict(cmd: str, reason: str) -> dict:
    """Build the loud non-pass verdict when the gate cannot run at all.

    Default library mode (soft-skip): `skipped: True` — the caller receives
    an explicit non-pass result without a fake compile failure. The scheduled
    school-loop does not rely on this soft-skip: its workflow preflight blocks
    issue execution before the bridge starts.

    ``VERIFY_GATE_STRICT=1`` escalates (campus.md #3: the compiler must
    ACTUALLY run before the critic speaks — if we cannot run it, we cannot
    pass): the verdict flips to `skipped: False` with `strict_escalated: True`,
    which the bridge treats as a real gate failure and forces the issue to FAIL.
    This strict flag covers direct/manual bridge callers and internal gate
    failures that occur after the workflow preflight.
    """
    failures = [{"cmd": cmd, "exit": None, "stderr": reason}]
    if os.environ.get("VERIFY_GATE_STRICT") == "1":
        failures[0]["stderr"] += (
            "\n[VERIFY_GATE_STRICT] Escalation: the verify gate could not run, "
            "so this issue cannot pass (compiler-before-critic is enforced)."
        )
        return {
            "passed": False,
            "skipped": False,
            "strict_escalated": True,
            "failures": failures,
            "ran": 0,
            "telemetry": {"shell_starts": 0, "commands": 0, "copied_bytes": 0},
        }
    return {
        "passed": False,
        "skipped": True,
        "failures": failures,
        "ran": 0,
        "telemetry": {"shell_starts": 0, "commands": 0, "copied_bytes": 0},
    }


def _flake_ref(flake_path: Path) -> Path:
    """Return the directory-form flake reference `nix develop` accepts cleanly.

    Passing the flake.nix *file* path works but prints a warning ("should point
    at the directory containing the flake.nix file"); the directory form is the
    clean reference. Falls back to the legacy file form only when neither the
    path itself nor path/flake.nix exists (caller error — keep the original
    shape so the error message still names the file).
    """
    flake_path = Path(flake_path)
    if flake_path.is_file():
        return flake_path.parent
    if (flake_path / "flake.nix").exists():
        return flake_path
    return flake_path / "flake.nix"


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


def _build_verify_script(
    commands: list[dict],
    work: Path,
    timeout: int,
) -> tuple[str, list[str], list[str]]:
    """Build the bounded shell wrapper and its per-command marker lists."""
    starts: list[str] = []
    ends: list[str] = []
    script_lines = ["set +e"]
    # Ensure local node_modules/.bin is in PATH for TS projects
    script_lines.append(f'export PATH="{work}/node_modules/.bin:$PATH"')
    for index, cmd in enumerate(commands):
        cwd = (work / cmd["cwd"]).resolve()
        start = f"__SCHOOL_VERIFY_START_{index}__"
        end = f"__SCHOOL_VERIFY_END_{index}__"
        starts.append(start)
        ends.append(end)
        script_lines.append(f"printf '%s\\n' {shlex.quote(start)}")
        script_lines.append(
            f"(cd -- {shlex.quote(str(cwd))} && "
            f"timeout {int(timeout)}s bash -c {shlex.quote(cmd['cmd'])}) 2>&1"
        )
        script_lines.append("status=$?")
        script_lines.append(f"printf '\\n%s%d\\n' {shlex.quote(end)} \"$status\"")
    # The wrapper reports command-level statuses through markers; its own
    # exit status must not hide later command diagnostics.
    script_lines.append("exit 0")
    return "\n".join(script_lines), starts, ends


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
        {"passed": bool, "failures": [...], "ran": int, "skipped": bool}
        `skipped` is True only when the reusable gate could not run at all (Nix
        missing, or no declared verify commands) — a loud non-pass, distinct
        from a real compile/test failure. The scheduled school-loop preflight
        blocks before this function when its required Nix infrastructure is
        absent. Under `VERIFY_GATE_STRICT=1` an unrunnable gate returns
        `skipped: False` + `strict_escalated: True` instead.
    """
    repo_path = Path(repo_path)
    flake_path = Path(flake_path) if flake_path else Path.cwd()

    commands = _discover_commands(repo_path, project_verify)
    if not commands:
        # No declared verify commands: we cannot prove correctness, but we
        # must not pretend success. Signal as a soft failure so the reviewer
        # knows verification was not possible. VERIFY_GATE_STRICT=1 escalates.
        return _skipped_verdict(
            "(discovery)",
            "No typecheck/test/lint commands discovered in repo.",
        )

    # Loudness: a missing Nix must NOT look like a compile failure. The gate
    # is only trustworthy when the hermetic shell actually runs, so when it
    # can't we return an explicit SKIPPED verdict the pipeline can distinguish
    # from a real failure — a school-failed issue because nix was absent would
    # be a silent lie.
    nix_bin = _find_nix()
    if nix_bin is None:
        return _skipped_verdict(
            "(nix)",
            "Nix not found — verify gate SKIPPED. "
            "Install Determinate Nix or add `nix` to PATH to run the "
            "hermetic verify layer.",
        )

    flake_ref = _flake_ref(flake_path)

    # Copy clone to a writable scratch dir so tests can emit artifacts.
    scratch = Path(tempfile.mkdtemp(prefix="school-verify-"))
    try:
        copied_bytes = 0

        def _copy_with_measurement(src, dst, *, follow_symlinks=True):
            nonlocal copied_bytes
            try:
                copied_bytes += max(0, Path(src).stat().st_size)
            except OSError:
                pass
            return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)

        # Only copy node_modules if it exists (pre-installed by clone_repo for TS projects)
        ignore_patterns = _VERIFY_COPY_IGNORE
        if not _has_node_modules(repo_path):
            ignore_patterns = shutil.ignore_patterns(
                *["node_modules", ".git", ".hg", ".svn", ".venv", "venv", "env",
                  "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
                  ".ruff_cache", ".hypothesis", ".coverage", "htmlcov", ".DS_Store"]
            )

        shutil.copytree(
            repo_path, scratch / "repo", dirs_exist_ok=True,
            ignore=ignore_patterns,
            copy_function=_copy_with_measurement,
        )
        work = scratch / "repo"

        failures: list[dict] = []
        script, starts, ends = _build_verify_script(commands, work, timeout)
        full = f"{nix_bin} develop {flake_ref}#verifyShell --command bash -c {shlex.quote(script)}"
        try:
            res = subprocess.run(
                full,
                shell=True,
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=(timeout * max(1, len(commands))) + 30,
            )
        except subprocess.TimeoutExpired:
            failures.append({
                "cmd": "(verify_shell)",
                "exit": None,
                "stderr": f"verify shell timed out after {(timeout * max(1, len(commands))) + 30}s",
            })
        else:
            output = (res.stdout or "")
            if res.stderr:
                output += f"\n{res.stderr}"
            found_markers = 0
            for index, cmd in enumerate(commands):
                start_pos = output.find(starts[index])
                end_pos = output.find(ends[index], start_pos + len(starts[index]))
                status_text = output[end_pos + len(ends[index]):].lstrip() if end_pos >= 0 else ""
                try:
                    exit_code = int(status_text.split()[0])
                except (IndexError, ValueError):
                    exit_code = None
                if start_pos >= 0 and end_pos >= 0 and exit_code is not None:
                    found_markers += 1
                    if exit_code != 0:
                        detail = output[start_pos + len(starts[index]):end_pos].strip()
                        failures.append({
                            "cmd": cmd["cmd"],
                            "exit": exit_code,
                            "stderr": (detail or "verify command failed")[-1500:],
                        })
            # A successful shell without every command marker is not proof
            # that the declared checks ran. Treat marker loss as a verify-shell
            # failure rather than allowing a silent false pass.
            if found_markers < len(commands) or (not commands and res.returncode != 0):
                missing = [
                    cmd["cmd"]
                    for index, cmd in enumerate(commands)
                    if output.find(starts[index]) < 0
                    or output.find(ends[index], output.find(starts[index]) + len(starts[index])) < 0
                ]
                marker_detail = (
                    f"verify shell emitted {found_markers}/{len(commands)} command markers; "
                    "execution evidence is incomplete"
                )
                if missing:
                    marker_detail += " missing: " + "; ".join(missing)
                if res.returncode != 0:
                    detail = (
                        marker_detail + "; "
                        + (res.stderr or res.stdout or "verify shell failed")[-1500:]
                    )
                    exit_code = res.returncode
                else:
                    detail = marker_detail
                    exit_code = res.returncode
                failures.append({
                    "cmd": "(verify_shell)",
                    "exit": exit_code,
                    "stderr": detail,
                })

        return {
            "passed": not failures,
            "failures": failures,
            "ran": len(commands),
            "telemetry": {
                "shell_starts": 1,
                "commands": len(commands),
                "copied_bytes": copied_bytes,
            },
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    r = run_verify_gate(p)
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["passed"] else 1)
