#!/usr/bin/env python3
"""
school_md_generator.py — Generate school.md for a repository.

Uses a cloud agent to digest a repo and produce a school.md file
that teaches an agent (or human) how to work within it.

Usage:
  from school_md_generator import generate_school_md
  school_md = generate_school_md("/path/to/repo", cloud=True)
"""

import os
import sys
import subprocess
from pathlib import Path
from typing import Optional

from executor import call_model, cloud_available, CLOUD_TIMEOUT

# System prompt for repo digestion
SCHOOL_MD_PROMPT = """You are analyzing a software repository. Produce a school.md file that teaches an AI agent (or human developer) how to work within this codebase.

The school.md must include these sections:

## 1. Project Overview
What does this project do? What problem does it solve? What are the key technologies?

## 2. Architecture
How is the codebase structured? What are the main modules/packages and how do they relate? Include a directory tree of the top-level structure.

## 3. Key Patterns & Conventions
How is code organized? What naming conventions are used? What design patterns appear repeatedly? What testing approach is used?

## 4. Key Files
List the 10-15 most important files and what each does. Focus on entry points, configuration, core logic, and tests.

## 5. Setup & Contributing
How to set up the dev environment. How to run tests. How to make changes and submit PRs. Include exact commands.

## 6. Common Gotchas
What would trip up a newcomer? Edge cases, non-obvious dependencies, things that look wrong but are intentional?

## 7. Triage Labels
What labels/issues exist in the repo's issue tracker? Map them to difficulty levels if applicable.

Be thorough but concise. Use code snippets where helpful. Output ONLY the markdown content — no preamble or explanation."""

# Approximate token limit — skip files larger than this
MAX_FILE_SIZE = 4000  # chars
# Max files to include in the digest
MAX_FILES = 30


def scan_repo(repo_path: Path) -> dict:
    """Scan a repo and return structured info about its contents."""
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        raise ValueError(f"Not a directory: {repo_path}")

    # Get top-level structure
    tree = _get_tree(repo_path)

    # Get key source files
    files = _find_source_files(repo_path)

    # Get README content
    readme = _find_readme(repo_path)

    # Get config files (package.json, Cargo.toml, pyproject.toml, etc.)
    configs = _find_config_files(repo_path)

    return {
        "path": str(repo_path),
        "name": Path(repo_path).resolve().name or repo_path.split("/")[-1],
        "tree": tree,
        "files": files,
        "readme": readme,
        "configs": configs,
    }


def _get_tree(repo_path: Path, max_depth: int = 3) -> str:
    """Get a directory tree string, excluding common noise."""
    ignore = {
        '.git', 'node_modules', '__pycache__', '.venv', 'venv',
        'target', 'build', 'dist', '.next', '.cargo', 'vendor',
        '.foundry', '.mypy_cache', '.pytest_cache',
    }
    lines = []

    def _walk(path: Path, depth: int, prefix: str):
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith('.') and entry.name != '.':
                continue
            if entry.name in ignore:
                continue
            rel = entry.relative_to(repo_path)
            if entry.is_dir():
                lines.append(f"{prefix}├── {entry.name}/")
                _walk(entry, depth + 1, prefix + "│   ")
            else:
                lines.append(f"{prefix}├── {entry.name}")

    _walk(repo_path, 0, "")
    return "\n".join(lines[:200])  # cap output


def _find_source_files(repo_path: Path) -> list:
    """Find key source files, prioritizing by extension and path."""
    extensions = {
        '.py', '.rs', '.ts', '.js', '.tsx', '.jsx', '.go', '.java',
        '.c', '.cpp', '.h', '.yaml', '.yml', '.toml', '.json',
    }
    skip_dirs = {
        '.git', 'node_modules', '__pycache__', '.venv', 'venv',
        'target', 'build', 'dist', '.next', 'vendor',
    }

    files = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in filenames:
            fpath = Path(root) / fname
            if fpath.suffix in extensions:
                try:
                    stat = fpath.stat()
                    if stat.st_size < MAX_FILE_SIZE * 4:  # ~4 chars per byte
                        files.append(fpath)
                except OSError:
                    pass

    # Sort: prefer top-level, then by size (smaller = more likely config/important)
    files.sort(key=lambda f: (len(f.relative_to(repo_path).parts), f.stat().st_size))
    return files[:MAX_FILES]


def _find_readme(repo_path: Path) -> str:
    """Find and read README file."""
    for name in ['README.md', 'README.rst', 'README.txt', 'README']:
        fpath = repo_path / name
        if fpath.exists():
            try:
                return fpath.read_text()[:3000]
            except Exception:
                pass
    return ""


def _find_config_files(repo_path: Path) -> dict:
    """Find and read config files."""
    configs = {}
    for name in ['package.json', 'Cargo.toml', 'pyproject.toml', 'setup.py',
                 'Makefile', 'Dockerfile', '.github/workflows']:
        fpath = repo_path / name
        if fpath.exists() and fpath.is_file():
            try:
                configs[name] = fpath.read_text()[:1500]
            except Exception:
                pass
    return configs


def _build_digest(repo_info: dict) -> str:
    """Build a text digest of the repo for the cloud agent."""
    lines = [
        f"# Repository: {repo_info['name']}",
        f"Path: {repo_info['path']}",
        "",
        "## Directory Structure",
        "```",
        repo_info['tree'],
        "```",
        "",
    ]

    if repo_info['readme']:
        lines.extend([
            "## README",
            repo_info['readme'][:2000],
            "",
        ])

    if repo_info['configs']:
        lines.append("## Configuration Files")
        for name, content in repo_info['configs'].items():
            lines.append(f"\n### {name}")
            lines.append(f"```\n{content}\n```")
        lines.append("")

    lines.append("## Key Source Files")
    for fpath in repo_info['files'][:15]:
        try:
            content = fpath.read_text()[:MAX_FILE_SIZE]
            rel = fpath.relative_to(Path(repo_info['path']))
            lines.append(f"\n### {rel}")
            lang = fpath.suffix.lstrip('.')
            lines.append(f"```{lang}")
            lines.append(content)
            lines.append("```")
        except Exception:
            pass

    return "\n".join(lines)


def generate_school_md(
    repo_path: str,
    cloud: bool = True,
    agent: str = "owl-alpha",
    output_path: Optional[str] = None,
) -> str:
    """
    Generate school.md for a repository.

    If cloud=True and cloud is available, uses a cloud agent to digest the repo.
    If cloud=False or cloud is unavailable, uses a local template approach.

    Returns the school.md content as a string.
    """
    repo_path = Path(repo_path)
    repo_info = scan_repo(repo_path)
    digest = _build_digest(repo_info)

    if cloud and cloud_available():
        # Use cloud agent for high-quality digestion
        prompt = f"{SCHOOL_MD_PROMPT}\n\n---\n\nHere is the repository to analyze:\n\n{digest}"
        try:
            school_md = call_model(agent, prompt, timeout=120)
            # Write to output if specified
            if output_path:
                Path(output_path).write_text(school_md)
            else:
                default_out = repo_path / "school.md"
                default_out.write_text(school_md)
                sys.stderr.write(f"[school_md] Written to {default_out}\n")
            return school_md
        except Exception as e:
            sys.stderr.write(f"[school_md] Cloud generation failed: {e}\nFalling back to template.\n")

    # Fallback: generate a basic template locally
    school_md = _generate_template(repo_info)

    if output_path:
        Path(output_path).write_text(school_md)
    else:
        default_out = repo_path / "school.md"
        default_out.write_text(school_md)
        sys.stderr.write(f"[school_md] Template written to {default_out}\n")

    return school_md


def _generate_template(repo_info: dict) -> str:
    """Generate a basic school.md template without using any model."""
    name = repo_info['name']
    lines = [
        f"# school.md — {name}",
        "",
        f"*{name} repository digest. Generated locally (no cloud).*",
        "",
        "## 1. Project Overview",
        f"Repository: `{repo_info['path']}`",
        "",
        "## 2. Directory Structure",
        "```",
        repo_info['tree'],
        "```",
        "",
    ]

    if repo_info['readme']:
        lines.extend([
            "## 3. README Excerpt",
            repo_info['readme'][:1000],
            "",
        ])

    if repo_info['configs']:
        lines.append("## 4. Configuration")
        for cname in repo_info['configs']:
            lines.append(f"- `{cname}`")
        lines.append("")

    lines.append("## 5. Key Files")
    for fpath in repo_info['files'][:10]:
        rel = str(fpath).replace(str(repo_info['path']), "").lstrip("/")
        lines.append(f"- `{rel}`")
    lines.append("")

    lines.extend([
        "## 6. Contributing",
        "(Run `generate_school_md(path, cloud=True)` with cloud available for full analysis.)",
        "",
        "## 7. Gotchas",
        "(Cloud-generated analysis unavailable in degraded mode.)",
    ])

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate school.md for a repository")
    parser.add_argument("repo", help="Path to the repository")
    parser.add_argument("--output", "-o", help="Output file path", default=None)
    parser.add_argument("--no-cloud", action="store_true", help="Skip cloud generation")
    parser.add_argument("--agent", default="owl-alpha", help="Cloud agent to use")
    args = parser.parse_args()

    result = generate_school_md(
        args.repo,
        cloud=not args.no_cloud,
        agent=args.agent,
        output_path=args.output,
    )
    print(result[:500] + "..." if len(result) > 500 else result)
