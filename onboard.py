#!/usr/bin/env python3
"""onboard.py — bootstrap ICM L0-L4 context + envit + project_verify for a target repo.

WHY THIS EXISTS
---------------
school-core was implicitly hard-wired to one target repo: `.github/workflows/
school-loop.yml` pins `SCHOOL_REPO` to a literal slug, `envit.json` declares
context by hand, and each target repo carries a hand-written `project_verify.yaml`
plus hand-written `CLAUDE.md` / `CONTEXT.md`. Onboarding a new repo was
copy-edit-drift work, repeated per repo.

`onboard` makes the framework repo-agnostic: give it `--repo owner/name` and it
generates the four artifacts a target repo needs, from evidence in the repo
itself — never from memory or a template with placeholders left in.

WHAT IT GENERATES (Icm L0–L4)
----------------------------
| Layer | Artifact | What it answers |
|-------|----------|-----------------|
| L0 | `CLAUDE.md` | "Where am I?" — identity, key paths, build/test, architecture one-liner, conventions |
| L1 | `CONTEXT.md` | "Where do I go?" — task-routing table + failure-edge ownership |
| L2 | `.envit/AGENTS.md` | stage-contract pointers for the declared dependency repos |
| L3 | `envit.json` + `envit.lock.json` | pinned dependency/skills declaration |
| L4 | `.envit/repos/<name>/` (symlink) | working artifact — the pinned checkout the agent reads |

Plus `project_verify.yaml` — the hermetic verify gate's declared command list,
in the exact shape `verify_gate.py::_discover_commands` consumes.

DESIGN RULES (why it is written this way)
----------------------------------------
* **Generated files are marked GENERATED** with the tool + timestamp + source
  repo. Re-running is a no-op-safe refresh, never an append.
* **Nothing is invented.** Every path/command in the output is either read from
  the repo (package.json scripts, Cargo.toml targets, pyproject.toml tool
  sections, Makefile targets) or emitted as an explicit, labelled heuristic
  with its evidence. `LANGUAGES` is the only inferred field and it is derived
  from repo evidence.
* **Fail-closed, not best-effort.** `onboard_repo` raises `OnboardError` when
  the target cannot be cloned or the repo metadata cannot be fetched. The CLI
  converts that to exit code 2 (configuration error) and prints the report.
* **No network writes.** This tool only *reads* from GitHub and writes into the
  local checkout. It never commits, pushes, opens PRs, or mutates the target.
* **Standard library + PyYAML only** (PyYAML is already a school-core runtime
  dependency). `tomllib` is stdlib on 3.11+; the TOML writer is a hand-rolled
  emitter so no `tomli`/`toml` package is required.

USAGE
-----
    # Against the orchestrator checkout (default) — writes into school-core/.
    python -m onboard --repo owner/repo --write

    # Into a target checkout instead (L0/L1 + project_verify land there).
    python -m onboard --repo owner/repo --write --out /path/to/target

    # Evidence only — report without writing anything.
    python -m onboard --repo owner/repo --dry-run

    # Evidence-gated (default is report-only): refuse to write unless every
    # generation step produced real output.
    python -m onboard --repo owner/repo --write --require-evidence
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

try:  # stdlib on 3.11+; absent on older interpreters (no write support then).
    import tomllib  # type: ignore
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

try:  # PyYAML is a school-core runtime dep (requirements.txt), but degrade cleanly.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - only affects YAML emission
    yaml = None


# ── constants ──────────────────────────────────────────────────────────────

ENVIT_MANIFEST = "envit.json"
ENVIT_LOCK = "envit.lock.json"
PROJECT_VERIFY = "project_verify.yaml"
L0_FILE = "CLAUDE.md"
L1_FILE = "CONTEXT.md"
ENVIT_AGENTS = ".envit/AGENTS.md"

# Path fragments that never describe an agent's working tree.
_SKIP_PARTS = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist",
    "build", "target", ".next", ".nuxt", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "coverage", "htmlcov", ".envit",
})

# Language tags the verify gate understands (mirrors verify_gate._EXT_TO_LANG).
_LANG_BY_EXT = {
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

# Failure edges / next actions — bounded vocabularies from failure_taxonomy.py.
_FAILURE_EDGES = ("model", "task_contract", "skill", "tool", "runtime", "verifier", "judge")

# Top-level directory names that are artifacts, scratch, or docs — never
# product source. Excluded from key paths so L0 does not send an agent into
# generated output.
_OUTPUT_DIR_HINTS = frozenset({
    "output", "outputs", "dogfood-output", "dogfood", "dist", "build", "out",
    "generated", "scratch", "tmp", "logs", "coverage", "report", "reports",
    "artifacts", "artifacts-tmp", "bench", "benchmarks", "tmp-out",
})

# The dependency repo that carries the ripwire skill suite. When it is NOT a
# declared dependency of the target, the ripwire workflow rows do not apply —
# they are not shipped to every repo by default.
_RIPWIRE_SLUG = "redhat-et/ripwire"

# The ripwire skill picks the framework itself declares (envit.proposed.json).
# Emitted verbatim so a target that declares ripwire gets the same skill set
# the orchestrator uses, not an unbounded `"*"`.
_RIPWIRE_PICKS = (
    "ripwire-router",
    "ripwire-orient",
    "ripwire-navigate",
    "ripwire-before-you-build",
    "ripwire-reuse-first",
    "ripwire-change-check",
    "ripwire-write-tests",
    "ripwire-handoff",
    "ripwire-security-scan",
)

# Languages that constitute CODE. A directory whose dominant language is not in
# this set is docs/config, not a product area.
_CODE_LANGS = frozenset({
    "python", "rust", "go", "typescript", "javascript", "c", "cpp", "java",
    "ruby", "php", "bash", "toml", "swift", "kotlin", "scala", "elixir",
})

# Language tag → verify commands. Emitted only for languages the repo actually
# contains (repo evidence), so the gate never runs a node check on a Rust repo.
_LANG_VERIFY: dict[str, list[dict[str, str]]] = {
    "python": [
        {"name": "python-compile", "cmd": "python3 -m compileall -q .", "cwd": ".",
         "languages": "python"},
    ],
    "rust": [
        {"name": "rust-check", "cmd": "cargo check 2>&1", "cwd": ".", "languages": "rust"},
    ],
    "go": [
        {"name": "go-build", "cmd": "go build ./... 2>&1", "cwd": ".", "languages": "go"},
    ],
    "typescript": [
        {"name": "tsc-noemit", "cmd": "npx tsc --noEmit 2>&1", "cwd": ".",
         "languages": "typescript"},
    ],
    "javascript": [
        {"name": "node-check", "cmd": "node --check \"$(find . -name '*.js' -not -path './node_modules/*' | head -1)\" 2>&1",
         "cwd": ".", "languages": "javascript"},
    ],
    "yaml": [
        {"name": "yaml-validate",
         "cmd": "python3 -c \"import yaml, sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]\" "
                "$(find . -name '*.yaml' -o -name '*.yml' | grep -v node_modules | head -5) 2>&1 || true",
         "cwd": ".", "languages": "yaml"},
    ],
    "json": [
        {"name": "json-validate",
         "cmd": "python3 -c \"import json, sys; [json.load(open(f)) for f in sys.argv[1:]]\" "
                "$(find . -name '*.json' | grep -v node_modules | head -5) 2>&1 || true",
         "cwd": ".", "languages": "json"},
    ],
    "markdown": [
        {"name": "markdown-readable",
         "cmd": "python3 -c \"import sys; [open(f).read() for f in sys.argv[1:]]; "
                "print(len(sys.argv[1:]), 'md ok')\" $(find . -name '*.md' | grep -v node_modules | head -5) 2>&1 || true",
         "cwd": ".", "languages": "markdown"},
    ],
}

# package.json script name → verify command. Only scripts that are real gates
# (not build/serve/dev) are surfaced.
_SCRIPT_VERIFY: list[tuple[str, str]] = [
    ("typecheck", "typecheck"),
    ("lint", "lint"),
    ("test", "test"),
    ("check", "check"),
]


class OnboardError(RuntimeError):
    """A target repo could not be onboarded — fail closed, never half-write."""


# ── evidence model ─────────────────────────────────────────────────────────


@dataclass
class RepoInfo:
    """GitHub metadata for the target, as reported by `gh repo view`."""

    slug: str
    name: str = ""
    description: str = ""
    primary_language: str = ""
    topics: list[str] = field(default_factory=list)
    default_branch: str = "main"
    homepage: str = ""
    stars: int = 0
    source: str = "gh"  # "gh" | "local" — how the metadata was obtained

    @property
    def display_name(self) -> str:
        return self.name or self.slug.rsplit("/", 1)[-1]


@dataclass
class Evidence:
    """Everything the generator could read from the target checkout.

    `generated` flags which sections were emitted from real repo evidence
    (paths, scripts, targets) versus generic/placeholder boilerplate. Callers
    that need fail-closed behaviour gate on it (`--require-evidence`).
    """

    root: Path
    languages: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)      # package.json dirs (rel)
    rust_manifests: list[str] = field(default_factory=list)
    pyprojects: list[str] = field(default_factory=list)
    makefile: str = ""
    go_mod: str = ""
    key_paths: list[str] = field(default_factory=list)
    scripts: dict[str, dict[str, str]] = field(default_factory=dict)  # pkg rel-dir -> {name: cmd}
    rust_targets: list[str] = field(default_factory=list)
    python_tools: list[str] = field(default_factory=list)   # pytest | ruff | mypy
    go_packages: list[str] = field(default_factory=list)
    dependency_repos: list[dict[str, str]] = field(default_factory=list)
    envit_entries: list[dict[str, Any]] = field(default_factory=list)
    readme_summary: str = ""                                # README prose, verbatim
    stack: list[tuple[str, str]] = field(default_factory=list)  # README `## Tech Stack` rows
    areas: list[tuple[str, str]] = field(default_factory=list)  # (top dir, stack label)
    package_manager: str = ""                               # npm | pnpm | yarn | bun
    generated: set[str] = field(default_factory=set)

    @property
    def detected(self) -> bool:
        """True when at least one concrete, repo-derived fact was read."""
        return bool(self.generated)


# ── small helpers ──────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_sha() -> str:
    """Short SHA of the onboard checkout — stamps generated files with provenance."""
    for candidate in (Path(__file__).resolve().parent, Path.cwd()):
        try:
            out = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return "unknown"


def _rel(path: Path, root: Path) -> str:
    try:
        value = path.relative_to(root).as_posix()
    except ValueError:
        value = path.as_posix()
    return value or "."


def _display(path: Path, root: Path) -> str:
    """Repo-relative path for display: '' renders as '.' (the gate's cwd token)."""
    return _rel(path, root) or "."


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "repo"


def _gh(args: list[str], timeout: int = 30) -> Optional[str]:
    """Run a `gh` command, returning stdout or None. Never raises."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"[onboard] gh call failed ({' '.join(args[:3])}): {exc}\n")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        sys.stderr.write(f"[onboard] gh error ({proc.returncode}): {err[0] if err else ''}\n")
        return None
    return proc.stdout


# ── repo metadata ──────────────────────────────────────────────────────────


def _remote_slug(repo_path: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        url = url[4:].replace(":", "/")
    if "github.com/" in url:
        return url.rsplit("github.com/", 1)[-1]
    return None


def _github_repo_info(slug: str) -> Optional[RepoInfo]:
    """Fetch repo metadata via `gh repo view`. None when gh/unauthenticated.

    A local filesystem path is not a slug — skip the gh round-trip rather than
    emitting a spurious "argument error" for it.
    """
    if "/" not in slug or slug.startswith((".", "/")) or slug.count("/") != 1:
        return None
    out = _gh([
        "repo", "view", slug,
        "--json", "name,description,primaryLanguage,languages,repositoryTopics,"
                   "defaultBranchRef,homepageUrl,stargazerCount",
    ])
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("name"):
        return None
    languages = []
    for entry in data.get("languages") or []:
        name = (entry.get("node") or {}).get("name") if isinstance(entry, dict) else None
        if name:
            languages.append(name)
    topics = []
    for topic in data.get("repositoryTopics") or []:
        name = topic.get("topic", {}).get("name") if isinstance(topic, dict) else None
        if name:
            topics.append(name)
    primary = (data.get("primaryLanguage") or {}).get("name") or ""
    branch = (data.get("defaultBranchRef") or {}).get("name") or "main"
    return RepoInfo(
        slug=slug,
        name=str(data.get("name") or ""),
        description=str(data.get("description") or ""),
        primary_language=primary,
        topics=topics,
        default_branch=branch,
        homepage=str(data.get("homepageUrl") or ""),
        stars=int(data.get("stargazerCount") or 0),
        source="gh",
    )


def _local_repo_info(repo_path: Path, slug: str) -> RepoInfo:
    """Derive metadata from the checkout itself when gh cannot (or is not wanted)."""
    branch = "main"
    # Prefer the REMOTE's default branch: a local checkout's HEAD is whatever
    # branch happened to be cloned, which is not the repo's default.
    for args in (["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                 ["rev-parse", "--abbrev-ref", "HEAD"]):
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_path), *args],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            break
        if proc.returncode == 0 and proc.stdout.strip():
            value = proc.stdout.strip()
            branch = value.rsplit("/", 1)[-1] if "/" in value else value
            break
    readme = ""
    for name in ("README.md", "readme.md", "README.rst", "README.txt"):
        candidate = repo_path / name
        if candidate.is_file():
            readme = _read_text(candidate, limit=4000)
            break
    first_line = ""
    for line in readme.splitlines():
        if line.strip():
            first_line = line.strip().lstrip("#").strip()
            break
    resolved = _remote_slug(repo_path) or slug
    return RepoInfo(
        slug=resolved,
        name=repo_path.name,
        description=first_line,
        default_branch=branch,
        source="local",
    )


def resolve_repo_info(slug: str, repo_path: Optional[Path] = None) -> RepoInfo:
    """gh metadata first; fall back to the local checkout; never fabricate."""
    info = _github_repo_info(slug)
    if info is not None:
        return info
    if repo_path is not None and repo_path.exists():
        return _local_repo_info(repo_path, slug)
    # No gh and no checkout: this is the fail-closed case the caller decides on.
    return RepoInfo(slug=slug, source="unavailable")


# ── repo inspection ────────────────────────────────────────────────────────


def _tracked_files(root: Path) -> list[str]:
    """Repo file list from `git ls-files` (falls back to a bounded os.walk)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    found: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_PARTS]
        for name in files:
            found.append(Path(base, name).relative_to(root).as_posix())
    return found


def _language_tag(path: str) -> Optional[str]:
    lowered = path.lower()
    for ext, lang in _LANG_BY_EXT.items():
        if lowered.endswith(ext):
            return lang
    return None


def _looks_like_source(rel: str) -> bool:
    """A tracked file that is plausibly part of the product (not tooling)."""
    parts = rel.split("/")
    if any(part in _SKIP_PARTS for part in parts):
        return False
    first = parts[0] if len(parts) > 1 else ""
    # Hidden trees (.github, .agents, .venv, …) are tooling, not product source.
    if parts[0].startswith("."):
        return False
    if first in {"test", "tests", "spec", "specs", "docs", "doc", "scripts",
                 "bench", "benchmarks", "examples", "example", "migrations",
                 "tasks", "notes", "changelog", "changes"}:
        return False
    if first in _OUTPUT_DIR_HINTS:
        return False
    # Only CODE counts as product source. A root README.md or a docs/notes.md is
    # markdown, not code — without this, a repo's README alone looks like a
    # source directory.
    return _language_tag(rel) in _CODE_LANGS


def _readme_evidence(root: Path, files: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """Pull the README's own summary + `## Tech Stack` table, if present.

    This is the highest-signal prose evidence a repo carries about what it is;
    it is read verbatim (no paraphrase, no invention). Returns
    ``(summary, [(layer, technology), ...])``.
    """
    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "readme.md"):
        if name in files:
            readme = _read_text(root / name)
            break
    if not readme:
        return "", []
    # Prose blocks are contiguous non-blank, non-markup line runs. The README
    # tagline is usually a one-line block that precedes the real description;
    # picking the LONGEST block takes the substantive paragraph while still
    # quoting the repo verbatim.
    blocks: list[str] = []
    current: list[str] = []
    for line in readme.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if stripped.startswith(("#", "<", "!", "[", "|", "```", "-", ">", "*")):
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    summary = max(blocks, key=len)[:280] if blocks else ""
    stack: list[tuple[str, str]] = []
    lines = readme.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^#{2,4}\s+.*tech\s*stack", line, re.I):
            continue
        for row in lines[index + 1:]:
            stripped = row.strip()
            if not stripped:
                continue
            if not stripped.startswith("|"):
                break
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) < 2 or set(cells[0]) <= set("-: "):
                continue
            layer = re.sub(r"[*`]", "", cells[0]).strip()
            tech = re.sub(r"[*`]", "", cells[1]).strip()
            if layer and tech and layer.lower() not in {"layer", "technology"}:
                stack.append((layer, tech))
        break
    return summary.strip(), stack


# Framework markers: filename → label. Ordered by specificity; first hit wins.
_FRAMEWORK_MARKERS: tuple[tuple[str, str], ...] = (
    ("manage.py", "Django"),
    ("app.py", "Flask"),
    ("asgi.py", "ASGI"),
    ("wsgi.py", "WSGI"),
    ("next.config.js", "Next.js"),
    ("next.config.mjs", "Next.js"),
    ("vite.config.ts", "Vite"),
    ("vite.config.js", "Vite"),
    ("svelte.config.js", "Svelte"),
    ("angular.json", "Angular"),
    ("go.mod", "Go module"),
    ("main.rs", "Rust"),
    ("lib.rs", "Rust"),
    ("index.tsx", "React"),
    ("App.tsx", "React"),
    ("index.ts", "TypeScript"),
    ("server.ts", "Node server"),
    ("index.js", "Node"),
)


def _derive_areas(root: Path, files: list[str]) -> list[tuple[str, str]]:
    """Per top-level source directory: (dir, stack label), from real evidence.

    A label is only emitted when a framework marker file, a package.json, or a
    language tag actually appears inside that directory — never guessed.
    """
    areas: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel in sorted(files):
        if "/" not in rel:
            continue
        top = rel.split("/")[0]
        if top in seen or not _looks_like_source(rel):
            continue
        seen.add(top)
        label = ""
        # 1. framework marker anywhere in the tree
        for marker, name in _FRAMEWORK_MARKERS:
            if any(part == marker for f in files for part in f.split("/")
                   if f.startswith(top + "/")):
                label = name
                break
        # 2. its own package.json
        if not label and (top + "/package.json") in files:
            label = "npm package"
        # 3. dominant language — only when the area holds CODE. A directory
        #    whose most common language is markdown/json/yaml is docs or
        #    config, not a product area, and is left out of the table.
        if not label:
            langs = sorted({
                tag for f in files
                if f.startswith(top + "/")
                for tag in [_language_tag(f)] if tag and tag in _CODE_LANGS
            })
            if langs:
                label = " + ".join(langs)
        if label:
            areas.append((top, label))
    return areas


def _derive_architecture(root: Path, files: list[str], info: RepoInfo,
                         areas: Optional[list[tuple[str, str]]] = None) -> str:
    """One-line architecture statement derived from directory evidence."""
    derived = areas if areas is not None else _derive_areas(root, files)
    if derived:
        body = ", ".join(f"`{name}/` ({label})" for name, label in derived[:6])
        statement = "top-level areas: " + body
    else:
        statement = "top-level areas: " + ", ".join(
            sorted({f.split("/")[0] for f in files if "/" in f})[:6]
        )
    has_backend = bool((root / "backend").is_dir() or (root / "server").is_dir())
    has_mobile = bool((root / "mobile").is_dir() or (root / "ios").is_dir()
                      or (root / "android").is_dir())
    shape = []
    if has_backend and has_mobile:
        shape.append("frontend + backend + native mobile")
    elif has_backend:
        shape.append("frontend + backend")
    elif has_mobile:
        shape.append("frontend + native mobile")
    if info.primary_language:
        shape.append(f"primary language {info.primary_language}")
    if not shape and not derived:
        return f"{info.display_name} repository (language/topology not yet characterized)."
    return "; ".join([statement, *shape]) + "."


def _infer_dependencies(root: Path, info: RepoInfo, ev: Evidence) -> None:
    """Discover external repos the target depends on (envit entries).

    Evidence sources, in order of preference:
      1. dependency manifests that declare a repo — `pyproject.toml`
         `[tool.uv.sources]` / `[tool.poetry.dependencies]` git URLs,
         `requirements.txt` `git+https://…` lines;
      2. the target's own `envit.json` (if the repo already declares some);
      3. the orchestrator's envit manifest (context repos every target reads).
    Only entries that resolve to a git URL are emitted — a package name is not
    a repo and must never be declared as one.
    """
    found: dict[str, dict[str, str]] = {}

    def _add(source: str, ref: str = "HEAD", via: str = "manifest") -> None:
        source = source.rstrip("/")
        if source.endswith(".git"):
            source = source[:-4]
        if "://" in source or source.startswith("git@"):
            if "github.com/" not in source:
                return
            slug = source.rsplit("github.com/", 1)[-1]
        else:
            # `owner/repo` form (the envit manifest's own convention)
            slug = source
        slug = slug.rstrip("/")
        if "/" not in slug or slug.count("/") != 1:
            return
        found.setdefault(slug, {"source": slug, "ref": ref, "via": via})

    # 1a. pyproject git sources
    for manifest in ev.pyprojects:
        text = _read_text(root / manifest)
        if not text:
            continue
        for match in re.finditer(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text):
            _add(match.group(1), via=f"{manifest}")
        if tomllib is not None:
            try:
                data = tomllib.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                sources = (data.get("tool", {}) or {}).get("uv", {}).get("sources") or {}
                for entry in sources.values():
                    if isinstance(entry, dict) and isinstance(entry.get("git"), str):
                        _add(entry["git"], ref=str(entry.get("rev") or entry.get("tag") or "HEAD"),
                             via=f"{manifest}")

    # 1b. requirements.txt git+ lines
    for req in sorted(root.glob("**/requirements*.txt")):
        if any(part in _SKIP_PARTS for part in req.parts):
            continue
        for match in re.finditer(
            r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", _read_text(req)
        ):
            _add(match.group(1), via=_display(req, root))

    # 2. the target's own envit declaration
    target_envit = root / ENVIT_MANIFEST
    if target_envit.is_file():
        try:
            data = json.loads(target_envit.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        for entry in (data or {}).get("repos") or []:
            source = entry.get("source") if isinstance(entry, dict) else None
            if isinstance(source, str):
                _add(source, ref=str(entry.get("ref") or "HEAD"), via=f"{ENVIT_MANIFEST}")

    # 3. orchestrator-declared context repos (ripwire is the canonical one)
    here = Path(__file__).resolve().parent
    for base in (here, Path.cwd()):
        manifest = base / ENVIT_MANIFEST
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for entry in data.get("repos") or []:
            source = entry.get("source") if isinstance(entry, dict) else None
            if isinstance(source, str):
                _add(source, ref=str(entry.get("ref") or "HEAD"),
                     via=f"{_display(manifest, base)}")
        break

    ev.dependency_repos = [found[k] for k in sorted(found)]
    if ev.dependency_repos:
        ev.generated.add("deps")


def _collect_evidence(root: Path, info: RepoInfo) -> Evidence:
    """Read every generation input from the checkout. No network, no guessing."""
    ev = Evidence(root=root)
    files = _tracked_files(root)
    if not files:
        files = [
            str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and not any(part in _SKIP_PARTS for part in p.parts)
        ]

    # languages — from tracked files, plus the repo's declared primary language
    langs: set[str] = set()
    for rel in files:
        tag = _language_tag(rel)
        if tag:
            langs.add(tag)
    if info.primary_language:
        tag = _LANG_BY_EXT.get("." + info.primary_language.lower())
        if tag:
            langs.add(tag)
    ev.languages = sorted(langs)
    if ev.languages:
        ev.generated.add("languages")

    # key paths — top-level directories that hold product source. Root-level
    # docs/config (AGENTS.md, README.md, .sub-tasks.json) are not "paths" an
    # agent navigates into, so they are excluded unless the repo has no
    # source directories at all. A directory counts only if it contains CODE —
    # a markdown-only tree is docs, not a product area.
    code_files = [rel for rel in files if _language_tag(rel) in _CODE_LANGS]
    key: list[str] = []
    for rel in sorted(code_files):
        if "/" not in rel or not _looks_like_source(rel):
            continue
        top = rel.split("/")[0]
        if top not in key:
            key.append(top)
    if not key:
        for rel in sorted(files):
            if "/" in rel or not _looks_like_source(rel):
                continue
            if rel.rsplit(".", 1)[-1] in {"py", "rs", "go", "ts", "tsx", "js", "jsx"}:
                key.append(rel)
    ev.key_paths = key[:10]
    if ev.key_paths:
        ev.generated.add("paths")

    # README evidence — the repo's own prose, read verbatim (never paraphrased)
    ev.readme_summary, ev.stack = _readme_evidence(root, files)
    if ev.readme_summary or ev.stack:
        ev.generated.add("readme")

    # per-area stack labels
    ev.areas = _derive_areas(root, files)
    if ev.areas:
        ev.generated.add("areas")

    # package manager — declared field wins, then a lockfile, then npm
    ev.package_manager = "npm"
    for lock, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                          ("bun.lockb", "bun"), ("package-lock.json", "npm")):
        if (root / lock).is_file():
            ev.package_manager = manager
            break
    root_pkg = root / "package.json"
    if root_pkg.is_file():
        try:
            declared = json.loads(root_pkg.read_text()).get("packageManager", "")
        except (OSError, json.JSONDecodeError):
            declared = ""
        match = re.match(r"^(npm|pnpm|yarn|bun)@", str(declared))
        if match:
            ev.package_manager = match.group(1)
    ev.generated.add("package_manager")

    # package.json scripts (per sub-project)
    for pkg in sorted(root.rglob("package.json")):
        if any(part in _SKIP_PARTS for part in pkg.parts):
            continue
        try:
            data = json.loads(pkg.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        scripts = data.get("scripts") or {}
        rel_dir = _rel(pkg.parent, root)
        ev.packages.append(rel_dir)
        if isinstance(scripts, dict):
            ev.scripts[rel_dir] = {
                str(k): str(v) for k, v in scripts.items() if isinstance(v, str)
            }
    if ev.packages:
        ev.generated.add("packages")

    # Cargo targets
    for manifest in sorted(root.rglob("Cargo.toml")):
        if any(part in _SKIP_PARTS for part in manifest.parts):
            continue
        rel = _display(manifest, root)
        ev.rust_manifests.append(rel)
        text = _read_text(manifest)
        for match in re.finditer(r'^\[\[bin\]\]\s*\nname\s*=\s*"([^"]+)"', text, re.M):
            ev.rust_targets.append(match.group(1))
        for match in re.finditer(r'^\[lib\]\s*\nname\s*=\s*"([^"]+)"', text, re.M):
            target = match.group(1).replace("-", "_")
            if target not in ev.rust_targets:
                ev.rust_targets.append(target)
    if ev.rust_manifests:
        ev.generated.add("rust")

    # pyproject tooling
    for manifest in sorted(root.rglob("pyproject.toml")):
        if any(part in _SKIP_PARTS for part in manifest.parts):
            continue
        rel = _display(manifest, root)
        ev.pyprojects.append(rel)
        text = _read_text(manifest)
        for tool in ("pytest", "ruff", "mypy", "black", "isort"):
            if f"[tool.{tool}]" in text:
                ev.python_tools.append(tool)
    if ev.pyprojects:
        ev.generated.add("python")

    # Makefile targets
    for name in ("Makefile", "makefile", "GNUmakefile"):
        candidate = root / name
        if candidate.is_file():
            ev.makefile = name
            text = _read_text(candidate)
            targets = re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:", text, re.M)
            ev.rust_targets.extend(t for t in targets[:8] if t not in ev.rust_targets)
            break
    if ev.makefile:
        ev.generated.add("makefile")

    # go module
    go_mod = root / "go.mod"
    if go_mod.is_file():
        ev.go_mod = "go.mod"
        text = _read_text(go_mod)
        match = re.search(r"^module\s+(\S+)", text, re.M)
        if match:
            ev.go_packages.append(match.group(1))
        ev.generated.add("go")

    _infer_dependencies(root, info, ev)
    return ev


# ── generators ─────────────────────────────────────────────────────────────


def _generated_banner(what: str, info: RepoInfo, stamp: str, sha: str) -> str:
    return (
        f"<!-- BEGIN ONBOARD-GENERATED: {what} | source={info.slug} | "
        f"generated={stamp} | onboard@{sha} -->\n"
        "<!-- This block is generated by school-core `onboard`. Edit the repo "
        "evidence it derives from, then re-run `python -m onboard --repo "
        f"{info.slug} --write`, rather than hand-editing. -->\n"
    )


def generate_claude_md(info: RepoInfo, ev: Evidence, stamp: str, sha: str) -> str:
    """L0 — 'Where am I?': identity, key paths, build/test, architecture, conventions."""
    lines: list[str] = []
    lines.append(f"# {info.display_name} — Project Instructions for AI Agents")
    lines.append("")
    lines.append(_generated_banner("L0 CLAUDE.md", info, stamp, sha))
    lines.append("")

    lines.append("## Identity")
    lines.append("")
    if info.description:
        lines.append(f"**{info.display_name}** — {info.description}")
    else:
        lines.append(f"**{info.display_name}** — {info.slug} (no GitHub description; "
                     "add one and re-run `onboard`).")
    lines.append("")
    meta = [f"`{info.slug}`", f"default branch `{info.default_branch}`"]
    if info.primary_language:
        meta.append(f"primary language {info.primary_language}")
    if info.topics:
        meta.append("topics: " + ", ".join(f"`{t}`" for t in info.topics[:6]))
    if info.homepage:
        meta.append(f"site: {info.homepage}")
    lines.append(" · ".join(meta))
    lines.append("")

    lines.append("## What this repo is")
    lines.append("")
    if ev.readme_summary:
        lines.append(ev.readme_summary)
    elif info.description:
        lines.append(info.description)
    else:
        lines.append("_No README summary or GitHub description detected. Add one and "
                     "re-run `onboard`._")
    lines.append("")
    if ev.stack:
        lines.append("**Stack (from the repo's own README):**")
        lines.append("")
        for layer, tech in ev.stack[:8]:
            lines.append(f"- **{layer}** — {tech}")
        lines.append("")

    lines.append("## Key paths")
    lines.append("")
    if ev.key_paths:
        for path in ev.key_paths:
            lines.append(f"- `{path}/`")
    else:
        lines.append("- _(no source directories detected — the repo may be empty or "
                     "contain only config)_")
    for rel in ev.packages[:5]:
        lines.append(f"- `{rel + '/' if rel != '.' else ''}package.json` — JS/TS project")
    for rel in ev.pyprojects[:5]:
        lines.append(f"- `{rel}` — Python packaging/tooling config")
    for rel in ev.rust_manifests[:5]:
        lines.append(f"- `{rel}` — Rust crate manifest")
    if ev.go_mod:
        lines.append(f"- `{ev.go_mod}` — Go module")
    if ev.makefile:
        lines.append(f"- `{ev.makefile}` — task runner")
    lines.append("")

    lines.append("## Build & Test")
    lines.append("")
    if ev.package_manager:
        lines.append(f"Package manager: `{ev.package_manager}`.")
        lines.append("")
    build_cmds: list[tuple[str, str]] = []
    for rel_dir in sorted(ev.scripts):
        scripts = ev.scripts[rel_dir]
        prefix = "" if rel_dir == "." else f"cd {rel_dir} && "
        for name, command in sorted(scripts.items()):
            if name in ("typecheck", "lint", "test", "check", "build", "verify",
                        "test:unit", "test:integration"):
                build_cmds.append((f"{rel_dir + '/' if rel_dir != '.' else ''}{name}",
                                   f"{prefix}{command}"))
    if build_cmds:
        lines.append("| Task | Command |")
        lines.append("|------|---------|")
        for label, command in build_cmds[:12]:
            lines.append(f"| {label} | `{command}` |")
    else:
        lines.append("_No build/test scripts detected. Add a `package.json` scripts "
                     "block or a `Makefile`, then re-run `onboard`._")
    lines.append("")

    lines.append("## Architecture Overview")
    lines.append("")
    lines.append(_derive_architecture(ev.root, _tracked_files(ev.root), info, ev.areas))
    lines.append("")
    if ev.areas:
        lines.append("| Area | Stack |")
        lines.append("|------|-------|")
        for name, label in ev.areas[:10]:
            lines.append(f"| `{name}/` | {label} |")
        lines.append("")
    if ev.rust_targets:
        lines.append("Rust targets: " + ", ".join(f"`{t}`" for t in ev.rust_targets[:8]) + ".")
        lines.append("")
    if ev.python_tools:
        lines.append("Python tooling: " + ", ".join(f"`{t}`" for t in ev.python_tools) + ".")
        lines.append("")
    lines.append("")

    lines.append("## Conventions & Patterns")
    lines.append("")
    lines.append(_conventions(ev))
    lines.append("")

    lines.append("## Verify (hermetic gate)")
    lines.append("")
    lines.append(f"This repo declares its verify commands in `{PROJECT_VERIFY}`. "
                 "school-core's hermetic gate (`verify_gate.py`) runs exactly those "
                 "commands inside a network-less shell — the compiler runs before the "
                 "critic speaks. Do not add commands that need network or missing "
                 "toolchains; they fail as infrastructure noise, not signal.")
    lines.append("")
    lines.append("<!-- END ONBOARD-GENERATED -->")
    lines.append("")
    return "\n".join(lines)


def _conventions(ev: Evidence) -> str:
    notes: list[str] = []
    if ev.packages:
        notes.append("JS/TS projects are managed per-directory from their own "
                     "`package.json`; each sub-project typechecks independently.")
    if ev.pyprojects:
        notes.append("Python packaging lives in `pyproject.toml`; tool configuration "
                     "is co-located per project.")
    if ev.rust_manifests:
        notes.append("Rust crates keep their own `Cargo.toml`; a workspace root "
                     "manifests the member crates.")
    if ev.go_mod:
        notes.append("Go code is built as one module; `go build ./...` covers all packages.")
    if not notes:
        notes.append("No language-specific conventions detected yet — add the "
                     "project's real conventions here, or re-run `onboard` once a "
                     "manifest file exists.")
    return "\n".join(f"- {note}" for note in notes)


def _routing_rows(ev: Evidence) -> list[tuple[str, str, str, str]]:
    """Task shape → workflow → verification → owned failure edge.

    Two blocks, both derived from repo evidence:
      * **repo-native gates** — one row per language the target actually
        contains, named from the commands it actually declares;
      * **workflow routing** — the CE skill row for each task shape. The
        `ripwire-*` suite is emitted ONLY when `redhat-et/ripwire` is a
        declared dependency of this target; otherwise the framework-neutral
        school-core skill rows are used. No repo is shipped ripwire skills it
        did not ask for.
    """
    rows: list[tuple[str, str, str, str]] = []

    # ── repo-native gates (evidence: languages + declared scripts) ──────────
    pm = ev.package_manager or "npm"
    for rel_dir, scripts in sorted(ev.scripts.items()):
        prefix = "" if rel_dir == "." else f"`{rel_dir}/` "
        for name in ("typecheck", "lint", "build", "test"):
            if name not in scripts:
                continue
            cd = "" if rel_dir == "." else f"cd {rel_dir} && "
            run = f"pnpm {name}" if pm == "pnpm" else f"{pm} run {name}"
            rows.append((
                f"{prefix}Run the `{name}` gate",
                f"`{cd}{run}`",
                f"`{name}` passes for this package",
                "verifier (quality, missing_evidence)",
            ))
    for rel in ev.rust_manifests:
        cwd = str(Path(rel).parent) if rel != "Cargo.toml" else "."
        rows.append((f"Compile the Rust crate in `{cwd}`", "`cargo check`",
                     "the crate compiles", "verifier (quality)"))
    for rel in ev.pyprojects:
        cwd = str(Path(rel).parent) if rel != "pyproject.toml" else "."
        text = _read_text(ev.root / rel)
        if "pytest" in text or (ev.root / cwd / "tests").is_dir():
            rows.append((f"Run the Python suite in `{cwd}`", "`pytest -q`",
                         "the repo's own tests pass", "verifier (missing_evidence)"))
    if ev.go_mod:
        rows.append(("Compile the Go module", "`go build ./...`",
                     "the module builds", "verifier (quality)"))

    # ── workflow routing (evidence: whether ripwire is a declared dep) ──────
    has_ripwire = any(d["source"].rstrip("/").endswith("ripwire")
                      for d in ev.dependency_repos)
    if has_ripwire:
        rows += [
            ("Prime / orient — cold repo, no direction",
             "`ripwire-orient` (`--recall` / `--report` / `--for`)",
             "what matters in a cold repo",
             "task_contract (wrong_file, missing_evidence)"),
            ("Plan a feature — requirements present",
             "`ripwire-before-you-build` (`--seams` / `--recall` / `--for`)",
             "what to touch, size estimate, interface contracts",
             "task_contract (incomplete)"),
            ("Implement / fix — named symbol or file",
             "`ripwire-navigate` (`--callers` / `--callees` / `--impact` / `--connect`)",
             "who calls this, is it safe to change",
             "skill (wrong_file)"),
            ("Reuse over duplication",
             "`ripwire-reuse-first` (`--exemplar` / `--clones`)",
             "reuse over copy; follow the repo's own pattern",
             "model (syntax)"),
            ("Validate a change",
             "`ripwire-change-check` (`--affected` / `--situ` / `--test-gate` / `--quality-delta`)",
             "which tests must run for THIS change; did I make it worse",
             "verifier (missing_evidence, quality)"),
            ("Write tests for existing code",
             "`ripwire-write-tests` (`--seams` + `--callers`)",
             "test the right seams",
             "verifier (missing_evidence)"),
            ("Review / pre-merge",
             "`ripwire-change-check` (`--pr-context` / `--merge-scout` / `--edit-check`)",
             "am I ready to push; is it safe to merge",
             "judge (quality, disagreement)"),
            ("Security review",
             "`ripwire-security-scan` (`--lint` + `--scan-skills`)",
             "is third-party skill/config safe to use",
             "tool (auth)"),
            ("Handoff",
             "`ripwire-handoff` (handoff bundle)",
             "the next agent gets the same state, not re-grepped",
             "runtime (spawn_failure, timeout)"),
        ]
    else:
        rows += [
            ("Prime / orient — cold repo, no direction",
             "`ripwire-orient` is NOT a declared dependency here — read "
             "`CLAUDE.md` + `CONTEXT.md` instead",
             "you know the repo's identity and entry points",
             "task_contract (wrong_file)"),
            ("Plan a feature — requirements present",
             "`plan` (spec before code) → `omh-plan`",
             "requirements are captured before implementation",
             "task_contract (incomplete)"),
            ("Implement / fix — named symbol or file",
             "`implement` (spec-driven) → `tdd` when behavior is specified",
             "the change matches the spec",
             "skill (wrong_file)"),
            ("Validate a change",
             "`requesting-code-review` (security scan + quality gates)",
             "did I make it worse",
             "verifier (missing_evidence, quality)"),
            ("Review / pre-merge",
             "`code-review` (changes since a fixed point)",
             "am I ready to push; is it safe to merge",
             "judge (quality, disagreement)"),
            ("Security review",
             "`repo-audit` (security posture + onboarding friction)",
             "is the repo safe to hand to an agent",
             "tool (auth)"),
            ("Handoff",
             "`handoff` (compact the session into a handoff document)",
             "the next agent gets the same state, not re-grepped",
             "runtime (spawn_failure, timeout)"),
        ]
    return rows


def generate_context_md(info: RepoInfo, ev: Evidence, stamp: str, sha: str) -> str:
    """L1 — 'Where do I go?': task routing + failure-edge ownership."""
    lines: list[str] = []
    lines.append(f"# {info.display_name} — Task Routing")
    lines.append("")
    lines.append(_generated_banner("L1 CONTEXT.md", info, stamp, sha))
    lines.append("")
    lines.append(f"Workspace-level routing for `{info.slug}`. Layer 1 of the ICM "
                 "hierarchy: it answers **where do I go?** — which workflow owns a "
                 "task shape, what it verifies, and which failure edge it owns.")
    lines.append("")

    lines.append("## Task routing")
    lines.append("")
    lines.append("| Task shape | Workflow | Verifies | Owns failure edge |")
    lines.append("|---|---|---|---|")
    for shape, workflow, verifies, edge in _routing_rows(ev):
        lines.append(f"| {shape} | {workflow} | {verifies} | {edge} |")
    lines.append("")

    lines.append("## Failure edges")
    lines.append("")
    lines.append("Every workflow above owns exactly one failure edge from "
                 "`failure_taxonomy.FAILURE_EDGES`: " + ", ".join(f"`{e}`" for e in _FAILURE_EDGES)
                 + ". A run that cannot name who failed is not representable.")
    lines.append("")

    lines.append("## Shared resources")
    lines.append("")
    lines.append(f"- L0 identity: `{L0_FILE}`")
    lines.append(f"- Verify commands: `{PROJECT_VERIFY}`")
    lines.append(f"- Pinned context + skills: `{ENVIT_MANIFEST}` / `{ENVIT_LOCK}`")
    if ev.dependency_repos:
        lines.append(f"- Declared dependency repos ({len(ev.dependency_repos)}): "
                     + ", ".join(f"`{d['source']}`" for d in ev.dependency_repos))
    else:
        lines.append("- Declared dependency repos: none discovered")
    lines.append("")

    lines.append("<!-- END ONBOARD-GENERATED -->")
    lines.append("")
    return "\n".join(lines)


def generate_envit_agents(info: RepoInfo, ev: Evidence, stamp: str, sha: str) -> str:
    """L2 — stage-contract pointers for the declared dependency repos."""
    lines: list[str] = []
    lines.append("# Repo context")
    lines.append("")
    lines.append(_generated_banner("L2 .envit/AGENTS.md", info, stamp, sha))
    lines.append("")
    lines.append(f"Generated by school-core `onboard` for `{info.slug}`. Do not edit "
                 "by hand. Each entry below is the full source of a dependency, "
                 "read-only, at an exact commit.")
    lines.append("")
    lines.append("> Search note: these are symlinks. `rg`/`fd` need `--follow`/`-L`, "
                 "or search the resolved path directly.")
    lines.append("")
    if ev.envit_entries:
        lines.append("| Repo | Ref | Discovered from | Path |")
        lines.append("|---|---|---|---|")
        for entry in ev.envit_entries:
            lines.append(
                f"| `{entry['source']}` | `{entry.get('ref', 'HEAD')}` | "
                f"{entry.get('via', 'manifest')} | `.envit/repos/{entry['name']}/` |"
            )
    else:
        lines.append("_No dependency repos declared. Onboard again once a manifest "
                     "declares a git dependency._")
    lines.append("")
    lines.append("<!-- END ONBOARD-GENERATED -->")
    lines.append("")
    return "\n".join(lines)


def _json_dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def build_envit_manifest(ev: Evidence, info: RepoInfo) -> dict:
    """envit.json shape: `repos` entries + per-repo `skills` picks.

    Keyed by the full `owner/repo` slug only — the bare repo name is a
    duplicate alias that makes the manifest ambiguous for no benefit.
    """
    repos: list[dict[str, str]] = []
    skills: dict[str, Any] = {}
    for dep in ev.dependency_repos:
        source = dep["source"]
        repos.append({"source": source, "ref": dep.get("ref") or "HEAD",
                      "update": "frozen"})
        if source == _RIPWIRE_SLUG:
            skills[source] = {"path": "skills", "pick": list(_RIPWIRE_PICKS),
                              "modelInvocable": False}
        else:
            skills[source] = {"path": "skills", "pick": "*",
                              "modelInvocable": False}
    return {"repos": repos, "skills": skills}


def _resolve_lock_entry(entry: dict[str, Any], repo_path: Optional[Path]) -> dict[str, Any]:
    """Add the pinned `commit` to a manifest entry, or mark it unresolved.

    Resolution order: the target's existing lockfile → the orchestrator's
    lockfile → `git ls-remote`. An entry that cannot be resolved is emitted with
    `commit: null` and `resolved: false` rather than silently dropped — the
    operator sees exactly what needs a real `envit sync`.
    """
    source = str(entry.get("source") or "")
    ref = str(entry.get("ref") or "HEAD")
    name = source.rstrip("/").rsplit("/", 1)[-1].replace(".git", "")

    # 1. existing lockfiles (target checkout, then orchestrator)
    bases = []
    if repo_path is not None:
        bases.append(repo_path)
    here = Path(__file__).resolve().parent
    bases.extend([here, Path.cwd()])
    for base in bases:
        lock = base / ENVIT_LOCK
        if not lock.is_file():
            continue
        try:
            data = json.loads(lock.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for locked in data.get("repos") or []:
            if str(locked.get("name")) == name or locked.get("source", "").endswith(source):
                return {
                    "name": name,
                    "source": str(locked.get("source") or source),
                    "ref": str(locked.get("ref") or ref),
                    "commit": str(locked.get("commit") or ""),
                }

    # 2. git ls-remote (only for non-HEAD refs; HEAD resolves locally below)
    url = source
    if "github.com/" in source and not source.startswith(("http", "git@")):
        url = f"https://github.com/{source}.git"
    if url.startswith("http") or url.startswith("git@"):
        want = ref if ref != "HEAD" else "HEAD"
        spec = f"refs/tags/{want}" if (ref != "HEAD" and not _looks_like_sha(ref)) else want
        for candidate in (spec, want):
            out = _gh(["api", f"repos/{source}/commits/{candidate}"], timeout=20)
            if out:
                try:
                    sha = str(json.loads(out).get("sha") or "").strip()
                    if sha:
                        return {"name": name, "source": url, "ref": ref, "commit": sha}
                except json.JSONDecodeError:
                    pass
    if _looks_like_sha(ref):
        return {"name": name, "source": url, "ref": ref, "commit": ref}
    return {"name": name, "source": url, "ref": ref, "commit": None}


def _looks_like_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", value or ""))


def build_envit_lock(ev: Evidence, info: RepoInfo, repo_path: Optional[Path]) -> dict:
    """Lock every declared dependency, including the ones that will not resolve.

    An unresolved entry is kept with ``commit: null`` — dropping it would hide
    the fact that the operator still needs a real ``envit sync`` for it.
    """
    entries = []
    for entry in ev.dependency_repos:
        resolved = _resolve_lock_entry(entry, repo_path)
        entries.append({**resolved, "resolved": bool(resolved.get("commit"))})
    return {"version": 1, "repos": entries}


def generate_project_verify(ev: Evidence, info: RepoInfo, stamp: str, sha: str) -> str:
    """project_verify.yaml in the exact shape verify_gate._discover_commands reads."""
    commands: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(name: str, cmd: str, cwd: str, languages: Optional[list[str]] = None) -> None:
        key = (name, cmd)
        if key in seen:
            return
        seen.add(key)
        entry: dict[str, Any] = {"name": name, "cmd": cmd, "cwd": cwd or "."}
        if languages:
            entry["languages"] = languages
        commands.append(entry)

    # 1. declared package.json gate scripts (repo evidence, highest signal)
    runner = ev.package_manager or "npm"
    for rel_dir in sorted(ev.scripts):
        scripts = ev.scripts[rel_dir]
        cwd = rel_dir
        # a sub-project may carry its own lockfile even when the root does not
        local_pm = runner
        for lock, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                              ("bun.lockb", "bun")):
            if (ev.root / rel_dir / lock).is_file():
                local_pm = manager
                break
        for script_name, label in _SCRIPT_VERIFY:
            if script_name in scripts:
                cmd = (f"{local_pm} {script_name}" if local_pm == "pnpm"
                       else f"{local_pm} run {script_name}")
                _add(f"{rel_dir}:{label}", cmd, cwd, ["typescript", "javascript"])

    # 2. Rust crates
    for rel in ev.rust_manifests:
        cwd = str(Path(rel).parent) if rel not in ("Cargo.toml", "./Cargo.toml") else "."
        _add(f"{cwd}:cargo-check", "cargo check 2>&1", cwd, ["rust"])

    # 3. Python projects
    for rel in ev.pyprojects:
        cwd = str(Path(rel).parent) if rel != "pyproject.toml" else "."
        text = _read_text(ev.root / rel)
        if "pytest" in text or (ev.root / cwd / "tests").is_dir():
            _add(f"{cwd}:pytest", "pytest -q", cwd, ["python"])
        if "[tool.ruff]" in text:
            _add(f"{cwd}:ruff", "ruff check .", cwd, ["python"])
        if "[tool.mypy]" in text:
            _add(f"{cwd}:mypy", "mypy .", cwd, ["python"])

    # 4. Go
    if ev.go_mod:
        _add("go-build", "go build ./... 2>&1", ".", ["go"])

    # 5. Makefile gates (only when no language-specific commands were found)
    if ev.makefile and not commands:
        text = _read_text(ev.root / ev.makefile)
        for target in re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:", text, re.M):
            if target in ("verify", "check", "test", "lint", "typecheck"):
                _add(f"make:{target}", f"make {target}", ".")

    # 6. language fallbacks for languages with no project file at all
    for lang in ev.languages:
        if any(lang in (cmd.get("languages") or []) for cmd in commands):
            continue
        for template in _LANG_VERIFY.get(lang, []):
            _add(template["name"], template["cmd"], template["cwd"], [lang])

    lines: list[str] = []
    lines.append(f"# {PROJECT_VERIFY} — hermetic verify commands for {info.slug}")
    lines.append("#")
    lines.append(f"# Generated by school-core `onboard` at {stamp} (onboard@{sha}).")
    lines.append("# Regenerate with: python -m onboard --repo "
                 f"{info.slug} --write")
    lines.append("#")
    lines.append("# HARD CONSTRAINT: every command must run in a NETWORK-LESS shell "
                 "with only the")
    lines.append("# pinned toolchain. There is no node_modules, no pip deps, no "
                 "network access.")
    lines.append("# Do NOT add npm/pytest/ruff commands until the target's "
                 "verification shell provisions")
    lines.append("# their toolchains — they would fail with command-not-found, "
                 "get filtered as")
    lines.append("# infrastructure noise, and burn a spawn per issue for zero signal.")
    lines.append("#")
    lines.append("# This file SHADOWS recursive package.json/pyproject.toml "
                 "discovery: verify_gate")
    lines.append("# runs exactly what is declared below and nothing else.")
    lines.append("verify:")

    if yaml is not None:
        body = yaml.safe_dump(commands, sort_keys=False, default_flow_style=False)
        for line in body.rstrip("\n").splitlines():
            lines.append(f"  {line}" if line.strip() else line)
    else:  # pragma: no cover - PyYAML is a runtime dep; this is belt-and-braces
        for entry in commands:
            lines.append(f"  - name: {entry['name']}")
            lines.append(f"    cmd: {entry['cmd']}")
            lines.append(f"    cwd: {entry['cwd']}")
            if entry.get("languages"):
                lines.append(f"    languages: [{', '.join(entry['languages'])}]")

    lines.append("")
    if not commands:
        lines.append("# No verify commands could be derived from repo evidence. Add a")
        lines.append("# package.json scripts block / pyproject.toml tool section, then re-run.")
    lines.append("")
    return "\n".join(lines)


# ── writer ─────────────────────────────────────────────────────────────────


def _replace_generated_block(text: str, new_block: str) -> str:
    """Swap the ONBOARD-GENERATED block, preserving any hand-written content."""
    pattern = re.compile(
        r"<!-- BEGIN ONBOARD-GENERATED:.*?(?:<!-- END ONBOARD-GENERATED -->\n?)",
        re.S,
    )
    if pattern.search(text):
        return pattern.sub(new_block, text, count=1)
    return text.rstrip("\n") + "\n\n" + new_block


def write_artifacts(
    out_root: Path,
    orchestrator_root: Path,
    info: RepoInfo,
    ev: Evidence,
    stamp: str,
    sha: str,
    *,
    write_envit: bool = True,
) -> list[str]:
    """Write every generated artifact. Returns the list of written paths."""
    written: list[str] = []
    out_root.mkdir(parents=True, exist_ok=True)

    claude = generate_claude_md(info, ev, stamp, sha)
    (out_root / L0_FILE).write_text(
        _replace_generated_block(_read_text(out_root / L0_FILE), claude), encoding="utf-8")
    written.append(str(out_root / L0_FILE))

    context = generate_context_md(info, ev, stamp, sha)
    (out_root / L1_FILE).write_text(
        _replace_generated_block(_read_text(out_root / L1_FILE), context), encoding="utf-8")
    written.append(str(out_root / L1_FILE))

    (out_root / PROJECT_VERIFY).write_text(
        generate_project_verify(ev, info, stamp, sha), encoding="utf-8")
    written.append(str(out_root / PROJECT_VERIFY))

    if not write_envit:
        return written

    # L2 — envit declaration + materialized pointers
    ev.envit_entries = [
        {**dep, "name": dep["source"].rstrip("/").rsplit("/", 1)[-1].replace(".git", "")}
        for dep in ev.dependency_repos
    ]
    envit_dir = out_root / ".envit"
    envit_dir.mkdir(parents=True, exist_ok=True)
    agents_path = envit_dir / "AGENTS.md"
    agents_path.write_text(
        _replace_generated_block(_read_text(agents_path), generate_envit_agents(info, ev, stamp, sha)),
        encoding="utf-8")
    written.append(str(agents_path))

    if ev.dependency_repos:
        manifest = build_envit_manifest(ev, info)
        (out_root / ENVIT_MANIFEST).write_text(_json_dump(manifest), encoding="utf-8")
        written.append(str(out_root / ENVIT_MANIFEST))
        lock = build_envit_lock(ev, info, out_root)
        (out_root / ENVIT_LOCK).write_text(_json_dump(lock), encoding="utf-8")
        written.append(str(out_root / ENVIT_LOCK))
        repos_root = envit_dir / "repos"
        repos_root.mkdir(parents=True, exist_ok=True)
        for dep in ev.envit_entries:
            link = repos_root / dep["name"]
            if link.is_symlink() or link.exists():
                continue
            store = _envit_store_path(dep)
            if store is not None:
                link.symlink_to(store, target_is_directory=True)
                written.append(str(link))

    return written


def _envit_store_path(dep: dict[str, str]) -> Optional[Path]:
    """Locate the pinned checkout in envit's global store, if materialized."""
    name = dep.get("name") or dep["source"].rstrip("/").rsplit("/", 1)[-1]
    source = dep["source"].rstrip("/")
    if source.endswith(".git"):
        source = source[:-4]
    slug = source.rsplit("github.com/", 1)[-1] if "github.com/" in source else source
    parts = slug.split("/")
    owner = parts[0] if len(parts) == 2 else ""
    base = Path.home() / ".envit" / "store" / "checkouts" / "github.com" / owner / name
    if not base.is_dir():
        return None
    for child in sorted(base.iterdir()):
        if child.is_dir() and not child.name.endswith(".git"):
            return child
    return None


# ── orchestration ──────────────────────────────────────────────────────────


def onboard_repo(
    repo: str,
    *,
    write: bool = False,
    out_root: Optional[Path] = None,
    clone: bool = True,
    offline: bool = False,
    require_evidence: bool = False,
) -> dict:
    """Onboard one target repo. Returns the report dict.

    Args:
        repo: ``owner/name`` slug (or ``.`` / a local path).
        write: write artifacts; otherwise report only.
        out_root: where L0/L1/project_verify land (default: the checkout).
        clone: clone the target into the school-core cache for inspection.
        offline: never call `gh` (use the checkout's own evidence only).
        require_evidence: fail-closed — raise unless real repo facts were read.
    """
    stamp = utc_now()
    sha = short_sha()

    local = Path(repo).expanduser()
    if local.exists() and (local / ".git").exists():
        repo_path = local.resolve()
        slug = _remote_slug(repo_path) or repo
    elif clone:
        from src.core.repo_reader import clone_repo
        repo_path = clone_repo(repo)
        slug = repo
    else:
        raise OnboardError(
            f"cannot inspect {repo!r}: not a local checkout and clone is disabled"
        )
    if repo_path is None or not repo_path.exists():
        raise OnboardError(f"could not clone or locate target repo {repo!r}")

    info = RepoInfo(slug=slug, source="unavailable") if offline else resolve_repo_info(slug, repo_path)
    ev = _collect_evidence(repo_path, info)
    if require_evidence and not ev.detected:
        raise OnboardError(
            f"no repo evidence read from {slug} — refusing to write generated files"
        )

    target_root = (out_root or repo_path).expanduser()
    orchestrator_root = Path(__file__).resolve().parent

    written: list[str] = []
    if write:
        written = write_artifacts(
            target_root, orchestrator_root, info, ev, stamp, sha,
            write_envit=True,
        )

    report = {
        "repo": slug,
        "source": info.source,
        "description": info.description,
        "primary_language": info.primary_language,
        "default_branch": info.default_branch,
        "languages": ev.languages,
        "key_paths": ev.key_paths,
        "packages": ev.packages,
        "rust_manifests": ev.rust_manifests,
        "pyprojects": ev.pyprojects,
        "makefile": ev.makefile,
        "go_mod": ev.go_mod,
        "rust_targets": ev.rust_targets,
        "python_tools": ev.python_tools,
        "readme_summary": ev.readme_summary,
        "stack": [f"{layer}: {tech}" for layer, tech in ev.stack],
        "areas": [f"{name} ({label})" for name, label in ev.areas],
        "package_manager": ev.package_manager,
        "dependency_repos": ev.dependency_repos,
        "written": written,
        "target_root": str(target_root),
        "generated_at": stamp,
        "onboard_sha": sha,
        "detected": ev.detected,
    }
    return report


def format_report(report: dict) -> str:
    """Human-readable summary of an onboarding run."""
    lines: list[str] = []
    lines.append(f"[onboard] {report['repo']}  (metadata: {report['source']})")
    if report.get("description"):
        lines.append(f"[onboard]   {report['description'][:160]}")
    lines.append(f"[onboard]   primary language: {report.get('primary_language') or 'unknown'}"
                 f" · branch: {report.get('default_branch')}")
    lines.append(f"[onboard]   languages: {', '.join(report.get('languages') or []) or 'none detected'}")
    lines.append(f"[onboard]   key paths: {', '.join(report.get('key_paths') or []) or 'none'}")
    lines.append(f"[onboard]   packages: {', '.join(report.get('packages') or []) or 'none'}"
                 f" · rust: {', '.join(report.get('rust_manifests') or []) or 'none'}"
                 f" · python: {', '.join(report.get('pyprojects') or []) or 'none'}")
    if report.get("package_manager"):
        lines.append(f"[onboard]   package manager: {report['package_manager']}")
    if report.get("areas"):
        lines.append(f"[onboard]   areas: {', '.join(report['areas'])}")
    if report.get("stack"):
        lines.append(f"[onboard]   stack (README): {', '.join(report['stack'][:6])}")
    if report.get("dependency_repos"):
        lines.append(f"[onboard]   dependency repos ({len(report['dependency_repos'])}):")
        for dep in report["dependency_repos"]:
            lines.append(f"[onboard]     - {dep['source']} @ {dep.get('ref', 'HEAD')}"
                         f"  (via {dep.get('via', 'manifest')})")
    else:
        lines.append("[onboard]   dependency repos: none discovered")
    if report.get("written"):
        lines.append(f"[onboard] wrote {len(report['written'])} file(s):")
        for path in report["written"]:
            lines.append(f"[onboard]   ✓ {path}")
    else:
        lines.append("[onboard] report-only — pass --write to generate files")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onboard",
        description="Generate ICM L0-L4 context + envit + project_verify for a target repo.",
    )
    parser.add_argument("--repo", required=True,
                        help="owner/name slug, or a local path to a checkout")
    parser.add_argument("--write", action="store_true",
                        help="write generated files (default: report only)")
    parser.add_argument("--out", default=None,
                        help="directory for L0/L1/project_verify (default: the target checkout)")
    parser.add_argument("--no-clone", action="store_true",
                        help="do not clone; require --repo to be a local checkout")
    parser.add_argument("--offline", action="store_true",
                        help="never call gh; use the checkout's own evidence only")
    parser.add_argument("--require-evidence", action="store_true",
                        help="fail closed unless real repo facts were read")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        report = onboard_repo(
            args.repo,
            write=args.write,
            out_root=Path(args.out).expanduser() if args.out else None,
            clone=not args.no_clone,
            offline=args.offline,
            require_evidence=args.require_evidence,
        )
    except OnboardError as exc:
        sys.stderr.write(f"[onboard] ERROR: {exc}\n")
        return 2
    except Exception as exc:  # unexpected — surface, never silently succeed
        sys.stderr.write(f"[onboard] UNEXPECTED ERROR: {exc}\n")
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
