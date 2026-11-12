"""Tests for uncovered pure functions in student_vm_runner.py.

These helpers are pure Python with no I/O - cheap to test, medium signal.
They were uncovered because the existing test suite focuses on the SmolVmRunner
orchestration paths, not the validation utilities.

Running: python -m pytest tests/test_student_vm_runner_helpers.py -v
"""
import io
import tarfile
import tempfile
import uuid
from pathlib import Path

import pytest

from src.crew.student_vm_runner import (
    _candidate_store_path,
    _file_sha256,
    _guess_task_id_from_manifest_path,
    _norm_repository,
    _norm_task_id,
    _tar_entry_names,
)

# ── _file_sha256 ──────────────────────────────────────────────────────────────


class TestFileSha256:
    def test_unchanged_file_returns_same_hash(self, tmp_path: Path) -> None:
        p = tmp_path / "test.txt"
        p.write_text("hello world", encoding="utf-8")
        h1 = _file_sha256(p)
        p.write_text("hello world", encoding="utf-8")  # rewrite identical
        h2 = _file_sha256(p)
        assert h1 == h2

    def test_changed_file_returns_different_hash(self, tmp_path: Path) -> None:
        p = tmp_path / "test.txt"
        p.write_text("hello world", encoding="utf-8")
        h1 = _file_sha256(p)
        p.write_text("goodbye world", encoding="utf-8")
        h2 = _file_sha256(p)
        assert h1 != h2

    def test_empty_file_has_valid_sha256(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.txt"
        p.write_bytes(b"")
        h = _file_sha256(p)
        assert len(h) == 64
        assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ── _tar_entry_names ──────────────────────────────────────────────────────────


class TestTarEntryNames:
    def test_basic_archive(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = b"content"
            info = tarfile.TarInfo(name="foo.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            info2 = tarfile.TarInfo(name="sub/bar.txt")
            info2.size = 10
            tar.addfile(info2, io.BytesIO(b"1234567890"))

        names = _tar_entry_names(buf.getvalue())
        assert "foo.txt" in names
        assert "sub/bar.txt" in names

    def test_empty_archive(self) -> None:
        # _tar_entry_names requires a valid tar file; empty input raises
        with pytest.raises(tarfile.ReadError):
            _tar_entry_names(b"")

    def test_non_reg_member_raises(self) -> None:
        with pytest.raises((tarfile.ReadError, AssertionError)):
            _tar_entry_names(b"garbage")

    def test_file_and_dir_mixed(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = b"content"
            info = tarfile.TarInfo(name="file.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            dir_info = tarfile.TarInfo(name="adir/")
            dir_info.type = tarfile.DIRTYPE
            tar.addfile(dir_info)

        names = _tar_entry_names(buf.getvalue())
        assert "file.txt" in names


# ── _guess_task_id_from_manifest_path ────────────────────────────────────────


class TestGuessTaskIdFromManifestPath:
    def test_extracts_task_id(self) -> None:
        path = Path("/some/root/task-abc123/candidates.json")
        task_id = _guess_task_id_from_manifest_path(path)
        assert task_id == "task-abc123"

    def test_non_matching_task_id_returns_random(self) -> None:
        # "UPPER" doesn't match _TASK_ID_RE (lowercase only), so random fallback
        path = Path("/root/UPPER_TASK/candidates.json")
        task_id = _guess_task_id_from_manifest_path(path)
        assert task_id.startswith("task-")

    def test_root_flavor_returns_task_id(self) -> None:
        # "abc" is a valid TASK_ID, so it's returned as-is
        task_id = _guess_task_id_from_manifest_path(Path("/task-select/abc/candidates.json"))
        assert task_id == "abc"


# ── _candidate_store_path ─────────────────────────────────────────────────────


class TestCandidateStorePath:
    def test_generate_safe_path(self, tmp_path: Path) -> None:
        base = tmp_path / "store"
        result = _candidate_store_path(
            base, task_id="Task-ABC-123", repository="My/Project"
        )
        expected = base / "task-abc-123" / "my-project" / "candidates.json"
        assert result == expected

    def test_empty_fallbacks(self, tmp_path: Path) -> None:
        base = tmp_path / "store"
        result = _candidate_store_path(base, task_id="", repository="")
        assert result == base / "task" / "project" / "candidates.json"

    def test_rejects_invalid_characters(self, tmp_path: Path) -> None:
        result = _candidate_store_path(
            tmp_path, task_id="Task/Path", repository="Project"
        )
        # "/" in task_id is replaced by "-", " " stripped; slash in repository
        assert "task-path" in str(result)
        assert "project/candidates.json" in str(result)


# ── _norm_repository ──────────────────────────────────────────────────────────


class TestNormRepository:
    def test_whitespace_is_empty(self) -> None:
        with pytest.raises(ValueError, match="repository is empty"):
            _norm_repository("   ")

    def test_valid_repository(self) -> None:
        assert _norm_repository("MyOrg/MyRepo") == "myorg/myrepo"

    def test_single_name(self) -> None:
        assert _norm_repository("myproject") == "myproject"

    def test_reject_missing_slash(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _norm_repository("invalid!repo")

    def test_reject_only_dots(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _norm_repository("...")

    def test_reject_dot_at_start(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _norm_repository(".hidden")

    def test_reject_dot_at_end(self) -> None:
        with pytest.raises(ValueError, match="dot"):
            _norm_repository("hidden.")

    def test_reject_leading_slash(self) -> None:
        with pytest.raises(ValueError, match="invalid segments"):
            _norm_repository("/absolute")

    def test_reject_trailing_slash(self) -> None:
        with pytest.raises(ValueError, match="invalid segments"):
            _norm_repository("absolute/")


# ── _norm_task_id ─────────────────────────────────────────────────────────────


class TestNormTaskId:
    def test_valid_task_id(self) -> None:
        assert _norm_task_id("task-abc123") == "task-abc123"

    def test_valid_uppercase_normalized(self) -> None:
        # function lowercases before validating
        assert _norm_task_id("TASK-ABC123") == "task-abc123"

    def test_reject_empty(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _norm_task_id("")

    def test_reject_space(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _norm_task_id("task abc")

    def test_reject_too_long(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _norm_task_id("a" * 101)
