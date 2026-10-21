"""Real-invocation integration tests for `src/entire_review.py`.

Invokes the actual `entire` CLI binary against a real temporary git repository.
Skipped if `entire` is not installed on PATH or in standard home bin paths.
"""

import subprocess
from pathlib import Path

import pytest

from src.entire_review import _get_entire_path, run_entire_review


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repository with an initial commit on main."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)

    main_file = repo_path / "main.py"
    main_file.write_text("def hello():\n    return 'world'\n")
    subprocess.run(["git", "add", "main.py"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True)


@pytest.mark.skipif(not _get_entire_path(), reason="entire CLI not installed")
# A real review takes ~200s (measured 185-208s) and the consolidating judge can
# run to its own 20m cap, while the subprocess cap is 900s. The project default
# (pytest.ini timeout=60) kills this test mid-review, so it needs a budget above
# the subprocess cap. CI never reaches it: `entire` is not installed there, so the
# skipif above fires and the test skips.
@pytest.mark.timeout(1200)
def test_run_entire_review_real_invocation(tmp_path):
    """Real invocation of entire review against a git worktree with changes."""
    _init_git_repo(tmp_path)

    # Make a commit on a feature branch so there is a diff against main
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_path, check=True, capture_output=True)
    feature_file = tmp_path / "feature.py"
    feature_file.write_text("def feature_func():\n    # TODO: implement feature\n    pass\n")
    subprocess.run(["git", "add", "feature.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=tmp_path, check=True)

    result = run_entire_review(str(tmp_path), base_branch="main")

    assert isinstance(result, dict)
    assert "status" in result
    assert result["status"] in ("pass", "fail", "skipped", "error")
    assert "findings" in result
    assert isinstance(result["findings"], list)
    assert result["skipped"] is False

    for finding in result["findings"]:
        assert "file" in finding
        assert "severity" in finding
        assert "message" in finding
