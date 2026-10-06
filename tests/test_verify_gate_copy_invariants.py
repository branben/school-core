"""Invariant guards for the verify gate's scratch-tree copy.

Bug, observed 2026-09-28 on branben__sound-royale-ny @ 994f2c2: the
under-500MB copy branch printed "node_modules will be hardlinked (N MB)" while
adding "node_modules" to the copytree ignore list. The gate then ran tsc/vitest
against a dependency-less tree, and the two-judge gate recorded the resulting
"This is not the tsc command" / "vitest: command not found" as CRITICAL findings
-- vetoing passing work and re-queuing the issue indefinitely.

The lesson is not "remember node_modules". It is that a copy step which ANNOUNCES
what it did, while doing something else, fails silently and for a long time. So
these guards assert properties of the copy rather than one dependency's name:

  1. whatever the gate says it copied must exist in the scratch tree
  2. the ignore set must never contain a directory the gate announces
  3. every package.json main/bin entry must resolve inside the scratch tree
     (this is the one that catches pnpm's symlink farm, bug #2)
  4. symlinks must not resolve outside the source repository or bypass copy exclusions

Guard 3 is the general package-entrypoint check: it fails for ANY package
manager, not just pnpm.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

import verify_gate


# --- 1. announcement must match the copy -------------------------------------

def test_announced_dirs_all_exist_after_copy(tmp_path):
    """Every dir the gate claims to copy must be present in the scratch tree.

    This is the direct encoding of the bug: the message was derived from
    `_has_node_modules()` on the SOURCE, and the copy silently excluded it.
    """
    repo = tmp_path / "repo"
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("x\n")
    (repo / "package.json").write_text("{}\n")
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("export const a=1\n")

    work = tmp_path / "scratch"
    shutil.copytree(repo, work, ignore=verify_gate._VERIFY_COPY_IGNORE)

    # The gate announces node_modules when this returns True on the source.
    if verify_gate._has_node_modules(repo):
        assert (work / "node_modules").is_dir(), (
            "gate announces node_modules is hardlinked, but the copy ignored it"
        )


# --- 2. the ignore set and the announcement must not contradict ---------------

def test_ignore_set_excludes_nothing_the_gate_announces() -> None:
    """No name the gate announces may be matched by the copy-ignore callable.

    A name appearing in both roles is a contradiction that no test of the
    resulting *behavior* can catch, because both halves are individually
    correct. `_VERIFY_COPY_IGNORE` is a shutil ignore_patterns CALLABLE, so this
    probes it by what it actually ignores rather than by reading its source.
    """
    ignore = verify_gate._VERIFY_COPY_IGNORE
    announced = {
        "node_modules",  # announced at verify_gate.py:382
    }

    ignored = set()
    for name in announced:
        probe = Path(name)
        if ignore(str(probe), [name]):  # returns a list of matched names
            ignored.add(name)

    assert not ignored, (
        f"gate announces copying {ignored} but the ignore callable also matches it"
    )


# --- 3. the general guard: declared entrypoints must resolve ------------------

def _declared_entrypoints(nm: Path) -> list:  # noqa: UP035
    """(package, entry) for every top-level package with a main/bin field."""
    out = []
    for pkg_json in nm.glob("*/package.json"):
        try:
            data = json.loads(pkg_json.read_text())
        except (OSError, ValueError):
            continue  # a corrupt manifest is pnpm's problem, not the copy's
        name = data.get("name") or pkg_json.parent.name
        for field in ("main", "module", "bin"):
            val = data.get(field)
            if isinstance(val, str):
                out.append((name, val))
            elif isinstance(val, dict):
                out.extend((name, v) for v in val.values() if isinstance(v, str))
    return out


def test_copied_node_modules_entrypoints_resolve(tmp_path):
    """Packages copied into the scratch tree must actually be loadable there.

    Fails for pnpm (symlink farm: node_modules/vitest -> .pnpm/vitest@X/...,
    a relative link whose target depth changes on copy), npm/yarn (missing
    transitive hoists), and any future layout that copies shells instead of
    contents. Bug #2 surfaced as ERR_MODULE_NOT_FOUND at runtime; this catches
    the same class without needing a Nix shell or a real install.
    """
    nm = tmp_path / "repo" / "node_modules"
    (nm / ".pnpm" / "vitest@1.0.0" / "node_modules" / "vitest").mkdir(parents=True)
    (nm / ".pnpm" / "vitest@1.0.0" / "node_modules" / "vitest" / "index.js").write_text("x\n")
    (nm / "vitest").symlink_to(".pnpm/vitest@1.0.0/node_modules/vitest")
    (nm / "vitest" / "package.json").write_text(
        json.dumps({"name": "vitest", "main": "index.js"})
    )
    repo = tmp_path / "repo"
    (repo / "package.json").write_text("{}\n")
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("export const a=1\n")

    work = tmp_path / "scratch"
    shutil.copytree(repo, work, ignore=verify_gate._VERIFY_COPY_IGNORE)

    copied_nm = work / "node_modules"
    if not copied_nm.is_dir():
        pytest.skip("node_modules not copied at all -- guard 1 covers that case")

    broken = []
    for name, entry in _declared_entrypoints(copied_nm):
        pkg = copied_nm / name
        target = pkg / entry
        if not target.exists():
            broken.append(f"{name} -> {entry}")

    assert not broken, (
        "package entrypoints unreachable inside the scratch tree: "
        + "; ".join(broken)
        + " -- the copy produced shells without resolvable contents"
    )


# --- 4. generated CodeGraph state must not break the scratch copy ------------

def test_copy_ignores_codegraph_runtime_socket():
    """A live CodeGraph Unix socket must not make verification copy fail."""
    with tempfile.TemporaryDirectory(prefix="cg-", dir="/tmp") as temp_dir:
        root = Path(temp_dir)
        repo = root / "repo"
        codegraph = repo / ".codegraph"
        codegraph.mkdir(parents=True)
        (codegraph / "index.sqlite").write_text("generated index")
        (repo / "verify.py").write_text("print('source')\n")

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(codegraph / "daemon.sock"))
            work = root / "scratch"
            shutil.copytree(repo, work, ignore=verify_gate._VERIFY_COPY_IGNORE)
        finally:
            server.close()

        assert (work / "verify.py").is_file()
        assert not (work / ".codegraph").exists()


# --- 5. repository symlinks must not copy data from outside the repo ---------

@pytest.mark.parametrize("link_style", ["absolute", "relative"])
def test_copy_rejects_file_symlink_to_outside_repo(tmp_path, link_style):
    """An outside target must be rejected before its contents enter scratch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("FAKE-SECRET-VERIFY-GATE-TEST")
    target = outside if link_style == "absolute" else Path("../outside-secret.txt")
    (repo / "escape").symlink_to(target)

    work = tmp_path / "scratch"
    with pytest.raises(ValueError, match="symlink escapes repository"):
        verify_gate._copy_repo_to_scratch(repo, work)

    assert not work.exists()


def test_copy_rejects_directory_symlink_to_outside_repo(tmp_path):
    """A linked outside directory must not be recursively copied into scratch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("FAKE-SECRET-DIRECTORY-TARGET")
    (repo / "external-dir").symlink_to(outside, target_is_directory=True)

    work = tmp_path / "scratch"
    with pytest.raises(ValueError, match="symlink escapes repository"):
        verify_gate._copy_repo_to_scratch(repo, work)

    assert not work.exists()


def test_copy_preserves_safe_in_repo_file_symlink_contents(tmp_path):
    """A safe in-repo file symlink still copies its target into scratch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.txt").write_text("safe in-repo content")
    (repo / "alias.txt").symlink_to(repo / "source.txt")

    work = tmp_path / "scratch"
    verify_gate._copy_repo_to_scratch(repo, work)

    copied_alias = work / "alias.txt"
    assert copied_alias.is_file()
    assert not copied_alias.is_symlink()
    assert copied_alias.read_text() == "safe in-repo content"


@pytest.mark.parametrize("ignored_target", [".env", ".git/config"])
def test_copy_rejects_symlink_to_ignored_sensitive_path(tmp_path, ignored_target):
    """An in-repo alias must not bypass copy exclusions for sensitive paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ignored_target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("FAKE-SECRET-VIA-SYMLINK")
    (repo / "alias.txt").symlink_to(target)

    with pytest.raises(ValueError, match="symlink targets excluded path"):
        verify_gate._validate_copy_symlinks(repo)


def test_copy_rejects_dangling_symlink_to_outside_repo(tmp_path):
    """An outside dangling target is rejected rather than copied as a link."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "external-dangling").symlink_to(tmp_path / "not-created")

    work = tmp_path / "scratch"
    with pytest.raises(ValueError, match="symlink escapes repository"):
        verify_gate._copy_repo_to_scratch(repo, work)

    assert not work.exists()


def test_run_verify_gate_rejects_symlink_before_reading_manifests(tmp_path, monkeypatch):
    """The real gate validates before command discovery can follow a file link."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_manifest = tmp_path / "outside-package.json"
    outside_manifest.write_text('{"scripts":{"test":"true"}}')
    (repo / "package.json").symlink_to(outside_manifest)

    monkeypatch.delenv("VERIFY_GATE_STRICT", raising=False)
    monkeypatch.setattr(
        verify_gate,
        "_discover_commands",
        lambda *_args: pytest.fail("manifest discovery ran before symlink validation"),
    )

    result = verify_gate.run_verify_gate(repo)

    assert result["passed"] is False
    assert result["skipped"] is True
    assert result["ran"] == 0
    assert "symlink" in result["failures"][0]["stderr"].lower()


# --- 6. generated CodeGraph state must not break the scratch copy ------------
