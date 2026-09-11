"""Centralized path utilities for the school-core package.

Single source of truth for filesystem layout:
    REPO_ROOT/                  (project checkout root)
    ├── config/
    │   ├── profiles/<name>/SOUL.md
    │   ├── roles/*.yaml
    │   ├── anchors.yaml
    │   ├── escalation_thresholds.yaml
    │   └── github.yaml
    ├── data/                   (runtime state)
    └── src/school_core/        (this package)

Modules that currently hardcode ``Path(__file__).parent / "config" / ...``
should import from here instead. This keeps path logic in one place so
layout changes (e.g. moving config/ into src/) only touch one file.
"""

from __future__ import annotations

from pathlib import Path

# ── Package directory ──────────────────────────────────────────────────────
# src/school_core/paths.py → parent.parent = repo root
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent.parent

# ── Config paths ────────────────────────────────────────────────────────────
CONFIG_DIR = REPO_ROOT / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
ROLES_DIR = CONFIG_DIR / "roles"
ANCHORS_PATH = CONFIG_DIR / "anchors.yaml"
ESCALATION_PATH = CONFIG_DIR / "escalation_thresholds.yaml"
GITHUB_CONFIG_PATH = CONFIG_DIR / "github.yaml"

# ── Data paths ──────────────────────────────────────────────────────────────
DATA_DIR = REPO_ROOT / "data"


def soul_path(profile_name: str) -> Path:
    """Return the path to a profile's committed SOUL.md."""
    return PROFILES_DIR / profile_name / "SOUL.md"


def home_soul_path(profile_name: str) -> Path:
    """Return the path to a profile's machine-local SOUL.md override."""
    return Path.home() / ".hermes" / "profiles" / profile_name / "SOUL.md"


def role_path(role_name: str) -> Path:
    """Return the path to a role's YAML definition."""
    return ROLES_DIR / f"{role_name}.yaml"
