"""Focused tests for the vault-allowlist enforcement contract (U8).

These test the pure matching logic only — no temporary files, no network.
The privacy contract is: only allowlisted glob patterns may live in
`data/vault/`.
"""

from pathlib import Path

import pytest

from scripts.check_vault_allowlist import allowed, load_allowlist

# Re-export for clarity at the call site.
from context_orchestrator import _vault_allowlist_violation as runtime_guard

# The same patterns declared in config/vault_allowlist.yaml (kept in sync).
P = ["school/**", "docs/glossary/**", "anchors/*.md",
     "roles/*.md", ".vault_manifest.json", "README.md"]


@pytest.mark.parametrize(
    "path,expected",
    [
        # Allowed: curated notes
        ("school/2026-09-13-html-sdlc.md", True),
        ("school/sub/note.md", True),
        ("docs/glossary/terms.md", True),
        ("anchors/tdd.md", True),
        ("roles/student.md", True),
        (".vault_manifest.json", True),
        ("README.md", True),
        # Not allowed: anything personal/private or unknown
        ("private/stash.md", False),
        ("personal/journal.md", False),
        ("school.txt", False),
        ("anchors/private/secret.md", False),
        ("images/photo.png", False),
        (".obsidian/workspace.json", False),
    ],
)
def test_allowed_matches(path, expected, P=P):
    assert allowed(Path(path), P) is expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("school/deep/nested/note.md", True),   # `school/**` recurses
        ("docs/glossary/deep/nested/note.md", True),
        ("anchors/a/b/c.md", False),            # `anchors/*.md` is single-level
        ("roles/x/y/z.md", False),              # `roles/*.md` is single-level
    ],
)
def test_glob_recursion(path, expected, P=P):
    """`**` recurses across depth; single `*` does not."""
    assert allowed(Path(path), P) is expected


def test_allowlist_has_patterns():
    patterns = load_allowlist(Path("config/vault_allowlist.yaml"))
    assert len(patterns) >= 4
    assert any("school" in p for p in patterns)
    assert any("**" in p for p in patterns)


# --- Runtime guard (context_orchestrator) ---


def test_runtime_guard_clean_vault():
    """A clean curated vault has no violation."""
    from pathlib import Path
    assert runtime_guard(Path("data/vault")) is None


def test_runtime_guard_ignores_repo_root():
    """The repo-root vault is not allowlist-guarded (it's code, not notes)."""
    from pathlib import Path
    assert runtime_guard(Path(".").resolve()) is None


def test_runtime_guard_flags_personal(tmp_path):
    """A stray private file under the curated vault is a violation."""
    vault = tmp_path / "vault"
    (vault / "private").mkdir(parents=True)
    (vault / "private" / "stash.md").write_text("secrets")

    # The curated-vault guard resolves to <repo>/data/vault, so we exercise the
    # underlying scan logic (same function the guard calls) against a tmp vault.
    from scripts.check_vault_allowlist import scan_vault, load_allowlist
    patterns = load_allowlist(Path("config/vault_allowlist.yaml"))
    violations = scan_vault(vault, patterns)
    assert violations, "expected private/stash.md to be a violation"
    assert any("stash.md" in v.as_posix() for v in violations)