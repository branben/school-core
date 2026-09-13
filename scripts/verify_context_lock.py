#!/usr/bin/env python3
"""Verify envit context declaration + lockfile, and materialize declared skills.

Reproducibility gate for the context/skills declaration. Runs in CI (and
locally) with ONLY system `git` + the Python standard library — it deliberately
does NOT shell out to `envit`, whose embedded gix transport fails with
"An IO error occurred when talking to the server" against GitHub HTTPS even
when system `git` can clone the same repo (envit v0.1.0, gix 0.86).

What this checks:
  1. Manifest/lock consistency: every repo in envit.json resolves to exactly
     the lockfile entry that `--frozen` would use. Drift (a ref that does not
     resolve to the locked commit) is a hard error.
  2. Commit-kind: every locked SHA must resolve to a git COMMIT object. envit
     v0.1.0 writes annotated-TAG object IDs into the lockfile (it does not
     peel); a tag object in the lockfile is a hard error (it cannot be checked
     out as a commit).

What this does:
  3. Materializes the pinned repos into --dest/repos, then exactly the
     declared skill picks into --dest/skills, so the ripwire security scan has
     real content to scan.

Pure stdlib, mirrors scripts/obsidian_client.py convention.

Usage:
  verify_context_lock.py --dest <dir> [--manifest envit.json] [--lock envit.lock.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, unquote


def run(cmd, **kw):
    """Run a command, returning (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, **kw)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"executable not found: {cmd[0]}"


def fail(msg, *, code=1):
    print(f"[verify_context_lock] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def repo_name_from_source(source):
    """bar/foo (list form) or https://github.com/bar/foo(url form) → 'foo'."""
    source = source.rstrip("/")
    if "://" in source or source.startswith("git@"):
        path = urlparse(source).path if "://" in source else source.split(":", 1)[1]
        return unquote(path).rstrip(".git").rsplit("/", 1)[-1]
    return source.split("/")[-1]


def sources_key(source):
    """Lockfile uses https:// URLs; manifest may use owner/repo. Canonicalize."""
    src = source.rstrip("/")
    if "/" in src and "://" not in src and not src.startswith("git@"):
        return f"https://github.com/{src}.git"
    if src.endswith(".git"):
        return src
    return f"{src}.git"


def ensure_git():
    rc, _, err = run(["git", "--version"])
    if rc != 0:
        fail(f"system git required but unavailable: {err.strip()}")


def load_manifest(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot load manifest {path}: {e}")
    repos = data.get("repos") or []
    if not isinstance(repos, list) or not repos:
        fail(f"manifest {path}: 'repos' must be a non-empty list")
    skills = data.get("skills") or {}
    return repos, skills


def load_lock(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot load lockfile {path}: {e}")
    by_source = {}
    for entry in data.get("repos") or []:
        by_source[sources_key(entry.get("source", ""))] = entry
    if not by_source:
        fail(f"lockfile {path}: no 'repos' entries")
    return by_source


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", required=True, help="output dir for repos/ and skills/")
    ap.add_argument("--manifest", default=str(Path.cwd() / "envit.json"))
    ap.add_argument("--lock", default=str(Path.cwd() / "envit.lock.json"))
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    repos_root = dest / "repos"
    skills_root = dest / "skills"

    ensure_git()
    manifest_repos, manifest_skills = load_manifest(args.manifest)
    lock_by_source = load_lock(args.lock)

    repos_root.mkdir(parents=True, exist_ok=True)
    skills_root.mkdir(parents=True, exist_ok=True)

    defined_sources = {sources_key(r["source"]) if isinstance(r, dict) else "" for r in manifest_repos}
    missing = [s for s in defined_sources if s and s not in lock_by_source]
    if missing:
        fail("lockfile missing entries for manifest sources: " + ", ".join(missing))

    # 1 + 2: manifest/lock consistency + commit-kind
    for repo in manifest_repos:
        if not isinstance(repo, dict):
            fail(f"manifest repo entry must be an object: {repo!r}")
        source = repo.get("source", "")
        lock_entry = lock_by_source.get(sources_key(source))
        if lock_entry is None:
            fail(f"manifest source not in lockfile: {source}")
        name = lock_entry.get("name") or repo_name_from_source(source)
        locked = lock_entry.get("commit")
        if not locked:
            fail(f"lockfile entry '{name}' has no 'commit'")

        # clone / fetch the pinned commit with system git (reproducible)
        clone_url = lock_entry.get("source") or sources_key(source)
        bare_dir = repos_root / f"{name}.git"
        if not (bare_dir / "HEAD").exists():
            rc, _, err = run(["git", "clone", "--quiet", "--bare", clone_url, str(bare_dir)])
            if rc != 0:
                fail(f"git clone of {clone_url} failed: {err.strip()}")
        else:
            rc, _, err = run(["git", "-C", str(bare_dir), "fetch", "--quiet", "--force", "--tags", "origin"])
            if rc != 0:
                fail(f"git fetch of {name} failed: {err.strip()}")

        kind = "?"
        rc, out, _ = run(["git", "cat-file", "-t", locked], cwd=str(bare_dir))
        if rc == 0:
            kind = out.strip()
        if kind != "commit":
            fail(
                f"lockfile '{name}' pins {locked} but that object is '{kind}' "
                "(expected 'commit'; a TAG object is an envit v0.1.0 lockfile bug — "
                "fix the lockfile to the peeled commit SHA)"
            )

        # ref-drift: does the manifest's frozen ref resolve to the locked commit?
        ref = (repo.get("ref") or "").strip()
        if ref and ref != locked and ref.upper() != "HEAD" and len(ref) < 40:
            rc, out, _ = run(["git", "rev-parse", f"refs/tags/{ref}^{{}}"], cwd=str(bare_dir))
            if rc != 0:
                rc, out, _ = run(["git", "rev-parse", f"refs/heads/{ref}"], cwd=str(bare_dir))
            if rc == 0:
                current = out.strip().splitlines()[0]
                if current != locked:
                    fail(
                        f"drift: manifest '{source}' ref '{ref}' resolves to {current}, "
                        f"but the lockfile pins {locked}. Run a non-frozen sync to update."
                    )
            elif ref != locked:
                fail(f"manifest '{source}' ref '{ref}' not found by rev-parse in {name}")

        # materialize the working checkout at the locked commit
        wc = repos_root / name
        if not (wc / ".git").exists():
            rc, _, err = run(["git", "clone", "--quiet", str(bare_dir), str(wc)])
            if rc != 0:
                fail(f"git clone (worktree) of {name} failed: {err.strip()}")
        rc, _, err = run(["git", "-C", str(wc), "checkout", "--quiet", "--force", locked])
        if rc != 0:
            fail(f"git checkout {locked} in {name} failed: {err.strip()}")

    # 3: materialize declared skill picks
    for source_key, skill_cfg in manifest_skills.items():
        if not isinstance(skill_cfg, dict):
            fail(f"skill entry '{source_key}' must be an object")
        src_normalized = sources_key(source_key)
        lock_entry = lock_by_source.get(src_normalized)
        name = lock_entry["name"] if lock_entry else repo_name_from_source(source_key)
        wc = repos_root / name
        rel_skills = skill_cfg.get("path") or "skills"
        skill_root = wc / rel_skills
        if not skill_root.is_dir():
            fail(f"declared skills path '{rel_skills}' missing in {name} (path={rel_skills})")
        picks = skill_cfg.get("pick") or []
        if picks == "*":
            picks = sorted(d.name for d in skill_root.iterdir() if d.is_dir())
        if not picks:
            fail(f"no skill picks for {source_key} (pick={picks!r})")
        for pick in picks:
            src_skill = skill_root / pick
            if not src_skill.is_dir():
                fail(f"declared skill '{pick}' missing in {name}/skills")
            out_link = skills_root / pick
            if out_link.exists() or out_link.is_symlink():
                continue
            out_link.symlink_to(src_skill, target_is_directory=True)
            print(f"[verify_context_lock] linked skill {pick} -> {name}/skills/{pick}")

    print(f"[verify_context_lock] OK: {len(manifest_repos)} repos pinned, "
          f"{len(manifest_skills)} sources with skills materialized")
    print(f"[verify_context_lock] repos -> {repos_root}")
    print(f"[verify_context_lock] skills -> {skills_root}")


if __name__ == "__main__":
    main()