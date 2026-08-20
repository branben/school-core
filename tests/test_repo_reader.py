"""Tests for repo_reader.py — repo clone, file tree, keyword matching."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, call, MagicMock

import pytest

from repo_reader import (
    build_codebase_context,
    clone_repo,
    extract_keywords,
    find_relevant_files,
    get_file_tree,
    cleanup_stale_caches,
    CACHE_DIR,
    _has_live_worktrees,
    _git,
    shutil as _rr_shutil,
    subprocess as _rr_subprocess,
)
import repo_reader


# --- Keyword extraction ---

class TestExtractKeywords:
    def test_extracts_meaningful_words(self):
        text = "Room creation fails with console error when host tries to join"
        keywords = extract_keywords(text)
        assert "room" in keywords
        assert "creation" in keywords
        assert "fails" in keywords
        assert "console" in keywords
        assert "error" in keywords or "host" in keywords

    def test_filters_stop_words(self):
        text = "The app is not working and it is broken"
        keywords = extract_keywords(text)
        assert "the" not in keywords
        assert "and" not in keywords
        assert "not" not in keywords
        assert "is" not in keywords

    def test_limits_to_max(self):
        text = "implement add subtract multiply divide modulo power sqrt abs round floor ceil"
        keywords = extract_keywords(text, max_keywords=5)
        assert len(keywords) == 5

    def test_empty_input(self):
        assert extract_keywords("") == []

    def test_filters_short_words(self):
        text = "fix the app bug"
        keywords = extract_keywords(text)
        assert "fix" not in keywords  # 3 chars, filtered
        assert "the" not in keywords
        assert "app" not in keywords  # 3 chars, filtered
        assert "bug" not in keywords  # 3 chars, filtered
        assert "bug" not in keywords  # 3 chars, filtered


# --- File tree ---

class TestGetFileTree:
    def test_returns_tree_for_real_repo(self, tmp_path):
        # Create a fake git repo
        repo = tmp_path / "test-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)

        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("def main(): pass\n")
        (repo / "src" / "utils.py").write_text("def util(): pass\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_main.py").write_text("def test_main(): pass\n")

        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        tree = get_file_tree(repo)
        assert "src" in tree
        assert "main.py" in tree or "utils.py" in tree

    def test_empty_for_non_repo(self, tmp_path):
        tree = get_file_tree(tmp_path)
        assert tree == ""


# --- Relevant files ---

class TestFindRelevantFiles:
    def test_finds_files_by_keyword(self, tmp_path):
        repo = tmp_path / "test-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)

        (repo / "room_manager.py").write_text("def create_room(): pass\ndef join_room(): pass\n")
        (repo / "player_utils.py").write_text("def get_player(): pass\n")
        (repo / "README.md").write_text("# Sound Royale\n")

        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        results = find_relevant_files(repo, ["room", "create"])
        file_names = [f.name for f in results]
        assert "room_manager.py" in file_names

    def test_returns_empty_for_no_keywords(self, tmp_path):
        repo = tmp_path / "test-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        (repo / "main.py").write_text("pass\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        assert find_relevant_files(repo, []) == []

    def test_limits_to_max_files(self, tmp_path):
        repo = tmp_path / "test-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)

        for i in range(10):
            (repo / f"room_{i}.py").write_text(f"def room_{i}(): pass\n")

        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        results = find_relevant_files(repo, ["room"], max_files=3)
        assert len(results) <= 3


# --- Codebase context ---

class TestBuildCodebaseContext:
    def test_returns_context_block(self, tmp_path):
        repo = tmp_path / "test-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)

        (repo / "main.py").write_text("def main(): pass\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        context = build_codebase_context(repo, "Fix the main function")
        assert "## Codebase Context" in context
        assert "File Tree" in context

    def test_returns_empty_for_none(self):
        assert build_codebase_context(None, "some issue") == ""

    def test_returns_empty_for_invalid_path(self, tmp_path):
        assert build_codebase_context(tmp_path / "nonexistent", "some issue") == ""

    def test_truncates_long_files(self, tmp_path):
        repo = tmp_path / "test-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)

        # Create a file larger than MAX_FILE_CHARS
        huge = "x" * 5000
        (repo / "huge.py").write_text(huge)
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        context = build_codebase_context(repo, "Fix the huge file")
        assert len(context) <= 11000  # MAX_TOTAL_CHARS + some overhead for headers
        assert "truncated" in context


# --- Clone (mocked) ---

class TestCloneRepo:
    @patch("repo_reader.subprocess.run")
    def test_returns_path_on_success(self, mock_run, tmp_path):
        # Mock the clone command
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        # clone_repo now clones into the stable cache path (CACHE_DIR/repo__slug),
        # not a temp dir. Verify it returns that deterministic path.
        result = clone_repo("octocat/Hello-World")
        assert result is not None
        expected = CACHE_DIR / "octocat__Hello-World"
        assert result == expected

    @patch("repo_reader.subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="repository not found"
        )
        result = clone_repo("nonexistent/repo")
        assert result is None


# --- force_fresh must not orphan live crew worktrees (B8 fix) ---


def _fake_clone_layout(base: Path, repo_slug: str, *, with_worktree: bool):
    """Create a minimal on-disk clone at the cache path for *repo_slug*.

    No real git required: _has_live_worktrees only inspects the
    <repo>/.git/worktrees/<name> directory tree on disk.
    """
    repo_path = base / repo_slug.replace("/", "__")
    repo_path.mkdir(parents=True)
    (repo_path / ".git").mkdir()
    if with_worktree:
        wt = repo_path / ".git" / "worktrees"
        wt.mkdir()
        (wt / "crew-abc").mkdir()  # a registered (attached) worktree
    return repo_path


class TestHasLiveWorktrees:
    def test_true_when_worktree_attached(self, tmp_path):
        rp = _fake_clone_layout(tmp_path, "owner/repo", with_worktree=True)
        assert repo_reader._has_live_worktrees(rp) is True

    def test_false_when_no_worktrees_dir(self, tmp_path):
        rp = _fake_clone_layout(tmp_path, "owner/repo", with_worktree=False)
        assert repo_reader._has_live_worktrees(rp) is False

    def test_false_when_worktrees_dir_empty(self, tmp_path):
        rp = _fake_clone_layout(tmp_path, "owner/repo", with_worktree=False)
        (rp / ".git" / "worktrees").mkdir()  # exists but no entries
        assert repo_reader._has_live_worktrees(rp) is False


class TestCloneRepoForceFresh:
    """force_fresh=True normally rmtree's the cached clone and re-clones. That
    must NOT happen while live crew worktrees are attached — it would orphan
    them (their .git link points into the deleted clone). In that case
    force_fresh is downgraded to a safe `git pull --ff-only` refresh.
    """

    @patch("repo_reader.subprocess.run")
    @patch("repo_reader.shutil.rmtree")
    @patch("repo_reader._git")
    def test_live_worktree_skips_rmtree_and_refreshes(
        self, mock_git, mock_rmtree, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(repo_reader, "CACHE_DIR", tmp_path / "cache")
        rp = _fake_clone_layout(tmp_path / "cache", "owner/repo", with_worktree=True)

        result = clone_repo("owner/repo", force_fresh=True)

        # The clone is preserved — no destructive delete.
        assert result == rp
        mock_rmtree.assert_not_called()
        # A safe refresh was issued in place of the re-clone.
        mock_git.assert_called_once_with(rp, "pull", "--ff-only")
        # No fresh clone subprocess (git clone ...) is launched.
        assert not any(
            "clone" in (c.args[0] if c.args else [])
            for c in mock_run.call_args_list
        )

    @patch("repo_reader.subprocess.run")
    @patch("repo_reader.shutil.rmtree")
    @patch("repo_reader._git")
    def test_no_live_worktree_still_removes_and_reclones(
        self, mock_git, mock_rmtree, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(repo_reader, "CACHE_DIR", tmp_path / "cache")
        rp = _fake_clone_layout(tmp_path / "cache", "owner/repo", with_worktree=False)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        result = clone_repo("owner/repo", force_fresh=True)

        # No live worktrees: the old behaviour is preserved — nuke + re-clone.
        mock_rmtree.assert_called_once_with(rp, ignore_errors=True)
        assert result == rp
        # A git clone was actually launched (force_fresh re-clone path).
        assert any(
            "clone" in (c.args[0] if c.args else [])
            for c in mock_run.call_args_list
        )

    @patch("repo_reader.subprocess.run")
    @patch("repo_reader.shutil.rmtree")
    @patch("repo_reader._git")
    def test_non_force_refreshes_without_rmtree(
        self, mock_git, mock_rmtree, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(repo_reader, "CACHE_DIR", tmp_path / "cache")
        rp = _fake_clone_layout(tmp_path / "cache", "owner/repo", with_worktree=True)

        result = clone_repo("owner/repo", force_fresh=False)

        assert result == rp
        mock_rmtree.assert_not_called()
        mock_git.assert_called_once_with(rp, "pull", "--ff-only")


# --- Cleanup ---

class TestCleanupStaleCaches:
    def test_cleanup_function_exists(self):
        # Verify the cleanup function is importable and callable
        from repo_reader import cleanup_stale_caches
        # Should not raise with non-existent cache dir
        cleanup_stale_caches(max_age_hours=0)
