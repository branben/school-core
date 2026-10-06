"""verify_gate.py — Execute untrusted student code inside an OS sandbox and Nix shell.

This is the missing stage in the Agent School pipeline. campus.md principle #3
says "the compiler runs before the critic speaks" — but issue_bridge only judged
the student's *prose*. This module actually RUNS the code.

Safety model (read before changing):
  - We only run declared repository verification commands.
  - Repository symlinks are checked before manifests are read; targets outside
    the repo or inside excluded paths are rejected before the scratch copy.
  - The cached clone is copied to a writable temp scratch dir; dependency files
    are copied, never hardlinked, so verification cannot mutate the shared clone.
  - Nix initializes the tool environment first; each declared command then
    runs inside macOS Seatbelt with writes confined to scratch and network
    denied. Nix is only the tool environment.
  - Unsupported hosts fail closed and report a skipped verification.
  - Timeouts bound every command; non-zero exit => failure finding.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from execution_sandbox import sandbox_exec_path, write_sandbox_profile

ALLOWED_CONFIG_NAMES = ("project_verify.yaml", "package.json", "pyproject.toml")


_EXT_TO_LANG = {
    ".py": "python", ".pyi": "python",
    ".rs": "rust",
    ".go": "go",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash", ".bash": "bash",
    ".toml": "toml",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
}


def _language_for_path(path: str) -> Optional[str]:
    """Map a single file path to its language tag, or None if unrecognised."""
    lowered = path.strip().lower()
    for ext, lang in _EXT_TO_LANG.items():
        if lowered.endswith(ext):
            return lang
    return None


def _detect_languages_from_diff(diff_text: str) -> set[str]:
    """Detect programming languages from a diff/code block.

    Returns a set of language tags: python, rust, go, typescript, javascript,
    yaml, json, markdown, bash, etc.
    """
    languages: set[str] = set()
    # Look for file paths in the diff (e.g., "diff --git a/file.py b/file.py" or "# file.py")
    import re
    for match in re.finditer(r'(?:diff --git a/(\S+)|# ([^\s]+\.\w+))', diff_text):
        path = match.group(1) or match.group(2) or ""
        lang = _language_for_path(path)
        if lang:
            languages.add(lang)
    # Also detect from code block language tags (```python, ```rust, etc.)
    for match in re.finditer(r'```(\w+)', diff_text):
        lang = match.group(1).lower()
        if lang in ("python", "rust", "go", "typescript", "javascript", "yaml", "json", "bash", "toml", "markdown", "md"):
            languages.add(lang)
    return languages


def _discover_commands(
    repo_path: Path,
    project_verify: Optional[Path],
    languages: Optional[set[str]] = None,
) -> list[dict]:
    """Discover declared verify commands from explicit manifest or project files.

    When *languages* is provided, only returns commands whose ``languages``
    list intersects with the detected languages. Commands with no ``languages``
    key are always included (they are language-agnostic).
    """
    commands: list[dict] = []
    if project_verify is None:
        default_manifest = repo_path / "project_verify.yaml"
        project_verify = default_manifest if default_manifest.exists() else None

    if project_verify and project_verify.exists():
        try:
            data = json.loads(project_verify.read_text()) if project_verify.suffix == ".json" else _yaml_load(project_verify)
            for entry in data.get("verify", []):
                cmd_languages = set(entry.get("languages", []))
                # If languages are specified, filter; otherwise include all
                if languages is not None and cmd_languages:
                    if not (cmd_languages & languages):
                        continue
                commands.append({
                    "name": entry.get("name", entry.get("cmd", "?")),
                    "cmd": entry["cmd"],
                    "cwd": entry.get("cwd", "."),
                })
            return commands
        except Exception as exc:  # pragma: no cover - defensive manifest parsing
            print(f"[verify_gate] project_verify parse failed: {exc}")

    if (repo_path / "package.json").exists():
        packages = sorted(repo_path.rglob("package.json"))
    else:
        packages = []
    for package in packages:
        if "node_modules" in package.parts:
            continue
        try:
            scripts = json.loads(package.read_text()).get("scripts", {})
        except Exception:
            continue
        sub = package.parent.relative_to(repo_path)
        if (package.parent / "pnpm-lock.yaml").exists():
            runner = "pnpm"
        elif (package.parent / "yarn.lock").exists():
            runner = "yarn"
        else:
            runner = "npm"
        for name in ("typecheck", "lint", "test", "check"):
            if name in scripts:
                cmd = f"{runner} run {name}" if runner != "pnpm" else f"pnpm {name}"
                commands.append({"name": f"{sub}/{runner}:{name}", "cmd": cmd, "cwd": str(sub) or "."})

    if (repo_path / "pyproject.toml").exists():
        configs = sorted(repo_path.rglob("pyproject.toml"))
    else:
        configs = []
    for config in configs:
        if ".venv" in config.parts or "site-packages" in config.parts:
            continue
        text = config.read_text(errors="replace")
        sub = config.parent.relative_to(repo_path)
        if "pytest" in text:
            commands.append({"name": f"{sub}/pytest", "cmd": "pytest -q", "cwd": str(sub) or "."})
        if "[tool.ruff]" in text:
            commands.append({"name": f"{sub}/ruff", "cmd": "ruff check .", "cwd": str(sub) or "."})
    return commands


_VERIFY_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".svn", ".venv", "venv", "env",
    ".env", ".env.*", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc", ".netrc",
    ".ssh", ".aws", ".kube", ".docker",
    "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".hypothesis", ".coverage", "htmlcov", ".DS_Store",
    ".codegraph",
    # .envit/repos/* are deliberate out-of-tree symlinks into ~/.envit/store
    # (pinned read-only dependency checkouts, see AGENTS.md). They are not
    # repo content; copying them would either duplicate large trees or trip
    # the symlink-escape guard and fail EVERY verification on this repo.
    ".envit",
)


def _has_node_modules(repo_path: Path) -> bool:
    """Check if the repo has a pre-installed node_modules directory."""
    return (repo_path / "node_modules").is_dir()


def _validate_copy_symlinks(repo_path: Path) -> None:
    """Reject copied symlinks that escape the repo or bypass copy exclusions."""
    repo_root = Path(repo_path).resolve(strict=True)
    directories = [Path(repo_path)]

    while directories:
        directory = directories.pop()
        with os.scandir(directory) as scan:
            entries = list(scan)
        names = [entry.name for entry in entries]
        ignored = set(_VERIFY_COPY_IGNORE(str(directory), names))

        for entry in entries:
            if entry.name in ignored:
                continue
            source = directory / entry.name
            if entry.is_symlink():
                try:
                    resolved_target = source.resolve(strict=False)
                    relative_target = resolved_target.relative_to(repo_root)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError(
                        f"symlink escapes repository or cannot be resolved: {source}"
                    ) from exc

                target_path = repo_root
                for part in relative_target.parts:
                    target_path = target_path / part
                    if _VERIFY_COPY_IGNORE(str(target_path.parent), [target_path.name]):
                        raise ValueError(
                            f"symlink targets excluded path: {source} -> {resolved_target}"
                        )
            if not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                directories.append(source)


def _copy_repo_to_scratch(
    repo_path: Path,
    work: Path,
    *,
    copy_function=shutil.copy2,
) -> Path:
    """Validate symlinks, then copy the repository with gate exclusions."""
    _validate_copy_symlinks(repo_path)
    return shutil.copytree(
        repo_path,
        work,
        ignore=_VERIFY_COPY_IGNORE,
        copy_function=copy_function,
    )


def scratch_base_path() -> Path:
    """Writable scratch base, configurable for machines with a larger temp disk."""
    env = os.environ.get("SCHOOL_VERIFY_SCRATCH", "").strip()
    return Path(env) if env else Path(tempfile.gettempdir())


def _find_nix() -> Optional[str]:
    """Locate nix on PATH or in the standard Determinate Nix path."""
    which = shutil.which("nix")
    if which:
        return which
    determinate = Path("/nix/var/nix/profiles/default/bin/nix")
    return str(determinate) if determinate.exists() else None


def _skipped_verdict(cmd: str, reason: str) -> dict:
    """Return a visible non-pass when verification cannot be safely executed."""
    failures = [{"cmd": cmd, "exit": None, "stderr": reason}]
    if os.environ.get("VERIFY_GATE_STRICT") == "1":
        failures[0]["stderr"] += (
            "\n[VERIFY_GATE_STRICT] Escalation: verification could not run, "
            "so this issue cannot pass."
        )
        return {
            "passed": False, "skipped": False, "strict_escalated": True,
            "failures": failures, "ran": 0, "results": [],
            "telemetry": {"shell_starts": 0, "commands": 0, "copied_bytes": 0},
        }
    return {
        "passed": False, "skipped": True, "failures": failures, "ran": 0,
        "results": [],
        "telemetry": {"shell_starts": 0, "commands": 0, "copied_bytes": 0},
    }


def _flake_ref(flake_path: Path) -> Path:
    """Return the directory-form flake reference `nix develop` accepts."""
    flake_path = Path(flake_path)
    if flake_path.is_file():
        return flake_path.parent
    if (flake_path / "flake.nix").exists():
        return flake_path
    return flake_path / "flake.nix"


def _yaml_load(path: Path) -> dict:
    """Load project_verify.yaml, with a small stdlib fallback."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        pass
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


def _build_verify_script(commands: list[dict], work: Path, timeout: int) -> tuple[str, list[str], list[str]]:
    """Build bounded command wrappers with parseable start/end markers."""
    starts, ends = [], []
    lines = ["set +e", f'export PATH="{work}/node_modules/.bin:$PATH"']
    for index, command in enumerate(commands):
        cwd = (work / command["cwd"]).resolve()
        start, end = f"__SCHOOL_VERIFY_START_{index}__", f"__SCHOOL_VERIFY_END_{index}__"
        starts.append(start)
        ends.append(end)
        lines.append(f"printf '%s\\n' {shlex.quote(start)}")
        # Wall-clock timing is ADDITIVE: it is emitted as a second token AFTER
        # the integer status, so the existing `status_text.split()[0]` parser in
        # run_verify_gate still reads the exit code. If either timestamp or awk
        # is unavailable the elapsed field is empty — never fatal to the gate.
        lines.append("__verify_t0=$(date +%s.%N 2>/dev/null)")
        lines.append(
            f"(cd -- {shlex.quote(str(cwd))} && "
            f"timeout {int(timeout)}s bash -c {shlex.quote(command['cmd'])}) 2>&1"
        )
        lines.append("status=$?")
        lines.append("__verify_t1=$(date +%s.%N 2>/dev/null)")
        lines.append(
            "__verify_elapsed=$(awk -v s=\"$__verify_t0\" -v e=\"$__verify_t1\" "
            "'BEGIN { if (s == \"\" || e == \"\") { exit } printf \"%.3f\", e - s }' "
            "2>/dev/null)"
        )
        lines.append(f"printf '\\n%s%d %s\\n' {shlex.quote(end)} \"$status\" \"$__verify_elapsed\"")
    lines.append("exit 0")
    return "\n".join(lines), starts, ends


def run_verify_gate(
    repo_path: Path,
    project_verify: Optional[Path] = None,
    flake_path: Path | None = None,
    timeout: int = 300,
    diff_text: str = "",
) -> dict:
    """Run each declared check in a fresh copy inside a network-denied OS sandbox.

    When *diff_text* is provided, the gate detects the languages present in the
    diff and only runs verify commands relevant to those languages. This allows
    the gate to handle Python, Rust, Go, TypeScript, and other languages.
    """
    repo_path = Path(repo_path)
    try:
        _validate_copy_symlinks(repo_path)
    except (OSError, RuntimeError, ValueError) as exc:
        return _skipped_verdict("(copy)", f"Unsafe repository symlink: {exc}")

    flake_path = Path(flake_path) if flake_path else Path.cwd()
    languages = _detect_languages_from_diff(diff_text) if diff_text else None
    commands = _discover_commands(repo_path, project_verify, languages)
    if not commands:
        return _skipped_verdict("(discovery)", "No typecheck/test/lint commands discovered in repo.")

    nix_bin = _find_nix()
    if nix_bin is None:
        return _skipped_verdict("(nix)", "Nix not found — verify gate SKIPPED.")
    sandbox_bin = sandbox_exec_path()
    if sandbox_bin is None:
        return _skipped_verdict(
            "(sandbox)",
            "OS sandbox unavailable — verification SKIPPED; commands were not run. "
            "macOS sandbox-exec is required.",
        )

    flake_ref = _flake_ref(flake_path)
    if not Path(flake_ref).exists() and not (Path(flake_path) / "flake.nix").exists():
        return _skipped_verdict("(flake)", f"No flake.nix found at {flake_path} — verify gate SKIPPED.")

    scratch_base = scratch_base_path()
    scratch_base.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="school-verify-", dir=str(scratch_base)))
    try:
        work = scratch / "repo"
        isolated_home, isolated_tmp = scratch / "home", scratch / "tmp"
        isolated_home.mkdir()
        isolated_tmp.mkdir()
        sandbox_profile = write_sandbox_profile(
            scratch / "verify.sb",
            writable_paths=[scratch],
            allow_network=False,
            confine_reads=True,
            readable_paths=[
                Path("/nix/store"), Path("/System"), Path("/usr"),
                Path("/bin"), Path("/sbin"), Path("/dev"),
            ],
        )
        copied_bytes = 0

        def copy_file(src, dst, *, follow_symlinks=True):
            nonlocal copied_bytes
            try:
                copied_bytes += max(0, Path(src).stat().st_size)
            except OSError:
                pass
            return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)

        if _has_node_modules(repo_path):
            node_modules = repo_path / "node_modules"
            size = sum(file.stat().st_size for file in node_modules.rglob("*") if file.is_file())
            if size < 500 * 1024 * 1024:
                sys.stderr.write(f"[verify_gate] node_modules will be copied ({size / 1024 / 1024:.1f}MB)\n")
            else:
                return _skipped_verdict("(node_modules)", "Dependencies exceed the 500MB isolated-copy limit.")

        _copy_repo_to_scratch(repo_path, work, copy_function=copy_file)
        script, starts, ends = _build_verify_script(commands, work, timeout)
        sandboxed_script = (
            f"exec {shlex.quote(sandbox_bin)} -f {shlex.quote(str(sandbox_profile))} "
            f"/usr/bin/env -i PATH=\"$PATH\" HOME={shlex.quote(str(isolated_home))} "
            f"TMPDIR={shlex.quote(str(isolated_tmp))} TMP={shlex.quote(str(isolated_tmp))} "
            f"npm_config_userconfig={shlex.quote(str(isolated_home / '.npmrc'))} "
            f"CI=1 /bin/bash -c {shlex.quote(script)}"
        )
        try:
            result = subprocess.run(
                [nix_bin, "develop", f"{flake_ref}#verifyShell", "--command", "/bin/bash", "-c", sandboxed_script],
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=(timeout * max(1, len(commands))) + 30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failures = [{"cmd": "(verify_shell)", "exit": None, "stderr": "verify shell timed out"}]
            output = ""
            returncode = 124
        else:
            failures = []
            output = result.stdout or ""
            if result.stderr:
                output += f"\n{result.stderr}"
            returncode = result.returncode

        found_markers = 0
        results: list[dict] = []
        for index, command in enumerate(commands):
            start = output.find(starts[index])
            end = output.find(ends[index], start + len(starts[index]))
            status_text = output[end + len(ends[index]):].lstrip() if end >= 0 else ""
            exit_code = None
            duration_s = None
            tokens = status_text.split()
            try:
                exit_code = int(tokens[0])
            except (IndexError, ValueError):
                exit_code = None
            if len(tokens) > 1:
                # Duration is an ADDITIVE second token; a malformed/missing
                # value must never crash the gate — fall back to None.
                try:
                    duration_s = float(tokens[1])
                except (IndexError, ValueError):
                    duration_s = None

            if start >= 0 and end >= 0 and exit_code is not None:
                status = "pass" if exit_code == 0 else "fail"
            else:
                status = "no_marker"
            results.append({
                "name": command.get("name") or command["cmd"],
                "cmd": command["cmd"],
                "exit": exit_code,
                "status": status,
                "duration_s": duration_s,
            })

            if status != "no_marker":
                found_markers += 1
                if exit_code != 0:
                    detail = output[start + len(starts[index]):end].strip()
                    failures.append({"cmd": command["cmd"], "exit": exit_code, "stderr": (detail or "verify command failed")[-1500:]})

        if found_markers < len(commands) and not any(failure["cmd"] == "(sandbox)" for failure in failures):
            missing = [command["cmd"] for index, command in enumerate(commands)
                       if output.find(starts[index]) < 0
                       or output.find(ends[index], output.find(starts[index]) + len(starts[index])) < 0]
            detail = f"verify shell emitted {found_markers}/{len(commands)} command markers; execution evidence is incomplete"
            if missing:
                detail += " missing: " + "; ".join(missing)
            if returncode != 0:
                detail += "; " + output[-1500:]
            failures.append({"cmd": "(verify_shell)", "exit": returncode, "stderr": detail})

        return {
            "passed": not failures,
            "failures": failures,
            "ran": len(commands),
            "results": results,
            "telemetry": {"shell_starts": 1, "commands": len(commands), "copied_bytes": copied_bytes},
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    result = run_verify_gate(path)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
