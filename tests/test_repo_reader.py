"""Tests for repo_reader.py — repo clone, file tree, keyword matching."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from repo_reader import (
    build_codebase_context,
    clone_repo,
    extract_keywords,
    find_relevant_files,
    get_file_tree,
    cleanup_stale_caches,
)


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

        # Create a fake clone directory that the function creates via mkdtemp
        fake_clone = tmp_path / "sound-royale-ny"
        fake_clone.mkdir()
        (fake_clone / ".git").mkdir()

        with patch("repo_reader.tempfile.mkdtemp", return_value=str(fake_clone)):
            result = clone_repo("branben/sound-royale-ny")
            assert result is not None
            assert result.exists()

    @patch("repo_reader.subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="repository not found"
        )
        result = clone_repo("nonexistent/repo")
        assert result is None


# --- Cleanup ---

class TestCleanupStaleCaches:
    def test_cleanup_function_exists(self):
        # Verify the cleanup function is importable and callable
        from repo_reader import cleanup_stale_caches
        # Should not raise with non-existent cache dir
        cleanup_stale_caches(max_age_hours=0)
