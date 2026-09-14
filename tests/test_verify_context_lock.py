"""Tests for scripts/verify_context_lock.py (envit declaration reproducibility gate).

Uses local file:// bare repos as fixtures — no network. Covers the two bugs
this gate exists for:
  * a tag OBJECT id in the lockfile (envit v0.1.0 writes the tag object SHA,
    not the peeled commit) must be a hard failure;
  * a genuine commit id must pass and produce a materialized skills tree.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_context_lock.py"


def run_script(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def local_repo(tmp_path):
    """A local bare repo with one commit and an annotated tag, plus helper to
    build file:// manifest/lock entries referencing it."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "skills").mkdir()
    (src / "skills" / "demo-skill").mkdir(parents=True)
    (src / "skills" / "demo-skill" / "SKILL.md").write_text("# demo-skill\n\nplaceholder\n")

    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "seed"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    # Annotated tag v1.0.0 -> tag object id (the envit bug case).
    subprocess.run(["git", "-C", str(src), "tag", "-a", "v1.0.0", "-m", "release", "HEAD"], check=True)
    tag_obj = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "refs/tags/v1.0.0"], check=True, capture_output=True, text=True
    ).stdout.strip()
    # Peeled commit id (the correct case).
    peeled = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "refs/tags/v1.0.0^{}"], check=True, capture_output=True, text=True
    ).stdout.strip()

    bare = tmp_path / "demo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    def make(commit_id, with_skill=True):
        skills = {f"file://{bare}": {"path": "skills", "pick": ["demo-skill"], "modelInvocable": False}} if with_skill else {}
        manifest = {
            "repos": [{"source": f"file://{bare}", "ref": "v1.0.0", "update": "frozen"}],
            "skills": skills,
        }
        lock = {
            "version": 1,
            "repos": [{
                "name": "demo",
                "source": f"file://{bare}",
                "ref": "v1.0.0",
                "commit": commit_id,
            }],
        }
        return manifest, lock

    return {
        "tag_obj": tag_obj,
        "peeled": peeled,
        "make": make,
        "manifest_path": tmp_path / "envit.json",
        "lock_path": tmp_path / "envit.lock.json",
    }


def _write(manifest, lock, manifest_path, lock_path):
    manifest_path.write_text(json.dumps(manifest))
    lock_path.write_text(json.dumps(lock))


def test_tag_object_in_lockfile_fails(local_repo, tmp_path):
    manifest, lock = local_repo["make"](local_repo["tag_obj"])
    _write(manifest, lock, local_repo["manifest_path"], local_repo["lock_path"])
    result = run_script(
        "--dest", str(tmp_path / "out"),
        "--manifest", str(local_repo["manifest_path"]),
        "--lock", str(local_repo["lock_path"]),
    )
    assert result.returncode != 0
    assert "commit" in result.stderr.lower()
    assert "tag" in result.stderr.lower()


def test_peeled_commit_passes_and_materializes(local_repo, tmp_path):
    manifest, lock = local_repo["make"](local_repo["peeled"])
    _write(manifest, lock, local_repo["manifest_path"], local_repo["lock_path"])
    out = tmp_path / "out"
    result = run_script(
        "--dest", str(out),
        "--manifest", str(local_repo["manifest_path"]),
        "--lock", str(local_repo["lock_path"]),
    )
    assert result.returncode == 0, result.stderr
    assert (out / "repos" / "demo").is_dir()
    assert (out / "skills" / "demo-skill" / "SKILL.md").is_file()


def test_ref_drift_fails(local_repo, tmp_path):
    # Re-point the tag at a second commit, then lock the OLD peeled commit:
    # the manifest ref (v1.0.0) now resolves elsewhere -> drift error.
    src = tmp_path / "src-drift"
    src.mkdir()
    (src / "skills").mkdir(parents=True)
    (src / "skills" / "demo-skill").mkdir()
    (src / "skills" / "demo-skill" / "SKILL.md").write_text("# demo-skill\n\nx\n")
    (src / "f").write_text("v1")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "first"], check=True)
    old_peeled = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (src / "f").write_text("v2")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "second"], check=True)
    subprocess.run(["git", "-C", str(src), "tag", "-a", "v1.0.0", "-m", "release", "HEAD"], check=True)

    bare = tmp_path / "demo-drift.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    source = f"file://{bare}"
    manifest = {
        "repos": [{"source": source, "ref": "v1.0.0", "update": "frozen"}],
        "skills": {source: {"path": "skills", "pick": ["demo-skill"], "modelInvocable": False}},
    }
    lock = {"version": 1, "repos": [{"name": "demo-drift", "source": source, "ref": "v1.0.0", "commit": old_peeled}]}
    manifest_path = tmp_path / "envit.json"
    lock_path = tmp_path / "envit.lock.json"
    manifest_path.write_text(json.dumps(manifest))
    lock_path.write_text(json.dumps(lock))

    result = run_script(
        "--dest", str(tmp_path / "out"),
        "--manifest", str(manifest_path),
        "--lock", str(lock_path),
    )
    assert result.returncode != 0
    assert "drift" in result.stderr.lower()