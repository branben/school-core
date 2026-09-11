"""Shared helpers for the School Core package."""

from __future__ import annotations

from school_core.paths import soul_path, home_soul_path


def load_soul(profile_name: str) -> str:
    """Resolve a persona's SOUL.md.

    Resolution order (single source of truth = repo config/profiles):
        1. ``<repo>/config/profiles/<name>/SOUL.md``  (committed, authoritative)
        2. ``~/.hermes/profiles/<name>/SOUL.md``       (machine-local override)
        3. empty string (caller supplies a generic fallback)

    Keeping the repo copy authoritative means `git clone` + run works without
    a manual copy step; HOME remains an optional local override that cannot
    silently shadow the committed persona without being intentional.
    """
    repo_soul = soul_path(profile_name)
    if repo_soul.exists():
        return repo_soul.read_text().strip()
    home_soul = home_soul_path(profile_name)
    if home_soul.exists():
        return home_soul.read_text().strip()
    return ""
