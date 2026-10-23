"""Tests for onboard.py — repo-agnostic ICM L0-L4 / envit / project_verify generation.

Hermeticity rules for this module:
* No network. ``onboard._gh`` is stubbed in every test that reaches a code path
  which could call GitHub (metadata + lockfile resolution).
* No writes outside ``tmp_path``. ``_envit_store_path`` is stubbed to ``None``
  so the envit symlink step never reaches into ``~/.envit``.
* The orchestrator's own ``envit.json`` (school-core/envit.json) is real repo
  evidence for the "declared context repos" step, so tests assert on what it
  actually declares rather than on a fabricated list.
"""

import json
import subprocess
from pathlib import Path

import pytest

import onboard
from onboard import (
    ENVIT_LOCK,
    ENVIT_MANIFEST,
    L0_FILE,
    L1_FILE,
    PROJECT_VERIFY,
    OnboardError,
    _derive_areas,
    _derive_architecture,
    _language_tag,
    _looks_like_source,
    _readme_evidence,
    _replace_generated_block,
    _routing_rows,
    build_envit_manifest,
    build_envit_lock,
    format_report,
    generate_claude_md,
    generate_context_md,
    generate_project_verify,
    onboard_repo,
    utc_now,
)


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Stub every GitHub call so the suite never touches the network."""
    commits = {
        "redhat-et/ripwire": "a" * 40,
        "plannotator/effective-html": "b" * 40,
        "plannotator/guides": "c" * 40,
    }

    def _fake_gh(args, timeout=30):
        if args[:2] == ["api", "repos"] and "commits" in args[-1]:
            slug = args[-1].split("/commits/")[0]
            sha = commits.get(slug)
            return json.dumps({"sha": sha}) if sha else None
        return None

    monkeypatch.setattr(onboard, "_gh", _fake_gh)
    # Never reach into the developer's real envit store.
    monkeypatch.setattr(onboard, "_envit_store_path", lambda dep: None)
    # Deterministic provenance stamps.
    monkeypatch.setattr(onboard, "short_sha", lambda: "deadbeef")
    monkeypatch.setattr(onboard, "utc_now", lambda: "2026-01-02T03:04:05Z")


def _git(root: Path, *args) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(root),
        },
    )


def make_repo(tmp_path: Path, name: str = "demo", *, package_manager: str = "npm",
              lockfile: str = "package-lock.json") -> Path:
    """A synthetic git checkout shaped like a small JS + Python monorepo."""
    root = tmp_path / name
    root.mkdir()
    (root / "README.md").write_text(
        "# Demo\n"
        "\n"
        "[![TypeScript](https://img.shields.io/badge/TypeScript-3178C6)]"
        "(https://www.typescriptlang.org/)\n"
        "\n"
        "A short tagline.\n"
        "\n"
        "Demo is a sample repository used to exercise the onboarding generator "
        "end to end, including its build and test wiring.\n"
        "\n"
        "## Tech Stack\n"
        "\n"
        "| Layer | Technology |\n"
        "|-------|------------|\n"
        "| Frontend | React 18, TypeScript, Vite |\n"
        "| Backend | Django 4.2, DRF |\n"
    )
    (root / "package.json").write_text(json.dumps({
        "name": "demo",
        "packageManager": f"{package_manager}@1.2.3",
        "scripts": {"build": "vite build", "lint": "eslint .",
                    "test": "vitest run", "typecheck": "tsc --noEmit"},
    }))
    (root / lockfile).write_text("{}\n")
    (root / "src").mkdir()
    (root / "src" / "index.tsx").write_text("export const x = 1;\n")
    (root / "backend").mkdir()
    (root / "backend" / "manage.py").write_text("# django\n")
    (root / "backend" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("# notes\n")
    (root / "dogfood-output").mkdir()
    (root / "dogfood-output" / "report.md").write_text("# report\n")
    (root / ".github").mkdir()
    (root / ".github" / "workflows").mkdir()
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def info_for(root: Path, slug: str = "acme/demo"):
    return onboard.RepoInfo(slug=slug, name="demo", description="A demo repo",
                            primary_language="TypeScript", default_branch="main",
                            source="local")


# ── README evidence ────────────────────────────────────────────────────────

class TestReadmeEvidence:
    def test_extracts_summary_and_stack(self, tmp_path):
        root = tmp_path
        (root / "README.md").write_text(
            "# Demo\n\nA tagline.\n\nDemo does real work in production for many users.\n\n"
            "## Tech Stack\n\n| Layer | Technology |\n|-------|------------|\n"
            "| Frontend | React 18 |\n| Backend | Django |\n"
        )
        summary, stack = _readme_evidence(root, ["README.md"])
        assert "real work in production" in summary
        assert stack == [("Frontend", "React 18"), ("Backend", "Django")]

    def test_summary_skips_badge_block(self, tmp_path):
        root = tmp_path
        (root / "README.md").write_text(
            "# Demo\n\n[![TypeScript](https://x)](https://y)\n\n"
            "Real prose that describes the project properly.\n"
        )
        summary, _ = _readme_evidence(root, ["README.md"])
        assert summary.startswith("Real prose")
        assert "img.shields.io" not in summary

    def test_stack_header_row_is_not_a_data_row(self, tmp_path):
        root = tmp_path
        (root / "README.md").write_text(
            "## Tech Stack\n\n| Layer | Technology |\n|-------|------------|\n"
            "| Frontend | React |\n"
        )
        _, stack = _readme_evidence(root, ["README.md"])
        assert stack == [("Frontend", "React")]

    def test_no_readme_returns_empty(self, tmp_path):
        assert _readme_evidence(tmp_path, []) == ("", [])


# ── source detection ───────────────────────────────────────────────────────

class TestLooksLikeSource:
    @pytest.mark.parametrize("rel,expected", [
        ("src/index.tsx", True),
        ("backend/manage.py", True),
        ("crates/core/src/lib.rs", True),
        ("README.md", False),
        (".github/workflows/ci.yml", False),
        (".sub-tasks.json", False),
        ("node_modules/x/index.js", False),
        ("dogfood-output/report.md", False),
        ("docs/notes.md", False),
        ("tasks/todo.md", False),
    ])
    def test_classification(self, rel, expected):
        assert _looks_like_source(rel) is expected

    def test_language_tag(self):
        assert _language_tag("a/b.py") == "python"
        assert _language_tag("a/b.ts") == "typescript"
        assert _language_tag("a/b.md") == "markdown"
        assert _language_tag("a/b.unknown") is None


class TestDeriveAreas:
    def test_framework_marker_labels_area(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "manage.py").write_text("x\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.tsx").write_text("x\n")
        areas = dict(_derive_areas(tmp_path, [
            "backend/manage.py", "src/index.tsx",
        ]))
        assert areas["backend"] == "Django"
        assert areas["src"] == "React"

    def test_markdown_only_dir_is_not_an_area(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "notes.md").write_text("x\n")
        assert _derive_areas(tmp_path, ["docs/notes.md"]) == []

    def test_architecture_statement_names_areas(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "manage.py").write_text("x\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.tsx").write_text("x\n")
        statement = _derive_architecture(
            tmp_path, ["backend/manage.py", "src/index.tsx"],
            onboard.RepoInfo(slug="a/b", primary_language="TypeScript"),
            _derive_areas(tmp_path, ["backend/manage.py", "src/index.tsx"]),
        )
        assert "`backend/` (Django)" in statement
        assert "`src/` (React)" in statement


# ── package manager ────────────────────────────────────────────────────────

class TestPackageManager:
    def test_declared_field_wins_over_lockfile(self, tmp_path):
        root = make_repo(tmp_path, package_manager="pnpm", lockfile="yarn.lock")
        ev = onboard._collect_evidence(root, info_for(root))
        assert ev.package_manager == "pnpm"

    def test_lockfile_used_when_no_declared_field(self, tmp_path):
        root = make_repo(tmp_path, package_manager="", lockfile="pnpm-lock.yaml")
        (root / "package.json").write_text(json.dumps({
            "name": "demo", "scripts": {"test": "vitest run"}}))
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "drop packageManager")
        ev = onboard._collect_evidence(root, info_for(root))
        assert ev.package_manager == "pnpm"

    def test_defaults_to_npm(self, tmp_path):
        root = make_repo(tmp_path)
        (root / "package-lock.json").unlink()
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "no lockfile")
        ev = onboard._collect_evidence(root, info_for(root))
        assert ev.package_manager == "npm"


# ── routing rows ───────────────────────────────────────────────────────────

class TestRoutingRows:
    def _ev(self, **kw):
        ev = onboard.Evidence(root=Path("/tmp/x"), languages=["typescript"])
        ev.scripts = {".": {"test": "vitest run", "lint": "eslint ."}}
        ev.package_manager = "npm"
        ev.dependency_repos = kw.get("deps", [])
        return ev

    def test_repo_native_gate_row_uses_declared_pm(self):
        rows = _routing_rows(self._ev())
        shapes = [r[0] for r in rows]
        assert "Run the `test` gate" in shapes
        test_row = next(r for r in rows if r[0] == "Run the `test` gate")
        assert test_row[1] == "`npm run test`"

    def test_ripwire_rows_absent_when_not_declared(self):
        rows = _routing_rows(self._ev(deps=[]))
        assert not any("ripwire-navigate" in r[1] for r in rows)
        assert not any("ripwire-change-check" in r[1] for r in rows)
        # the routing block still exists — it just uses framework-neutral skills
        assert any("`plan`" in r[1] for r in rows)

    def test_ripwire_rows_present_when_declared(self):
        rows = _routing_rows(self._ev(
            deps=[{"source": "redhat-et/ripwire", "ref": "v0.6.0", "via": "envit.json"}]))
        assert any("`ripwire-orient`" in r[1] for r in rows)
        assert any("`ripwire-change-check`" in r[1] for r in rows)
        assert any("`ripwire-navigate`" in r[1] for r in rows)

    def test_every_row_names_a_known_failure_edge(self):
        rows = _routing_rows(self._ev(
            deps=[{"source": "redhat-et/ripwire", "ref": "v0.6.0", "via": "envit.json"}]))
        for shape, _workflow, _verifies, edge in rows:
            head = edge.split(" ", 1)[0]
            assert head in onboard._FAILURE_EDGES, f"{shape!r} → {edge!r}"


# ── generators ─────────────────────────────────────────────────────────────

class TestGenerateClaudeMd:
    def _ev(self, root: Path):
        ev = onboard._collect_evidence(root, info_for(root))
        return ev

    def test_marks_generated_block(self, tmp_path):
        root = make_repo(tmp_path)
        text = generate_claude_md(info_for(root), self._ev(root), utc_now(), "deadbeef")
        assert "<!-- BEGIN ONBOARD-GENERATED: L0 CLAUDE.md" in text
        assert "<!-- END ONBOARD-GENERATED -->" in text
        assert "source=acme/demo" in text

    def test_quotes_readme_stack_verbatim(self, tmp_path):
        root = make_repo(tmp_path)
        text = generate_claude_md(info_for(root), self._ev(root), utc_now(), "deadbeef")
        assert "React 18, TypeScript, Vite" in text
        assert "Django 4.2, DRF" in text

    def test_key_paths_exclude_output_and_docs(self, tmp_path):
        root = make_repo(tmp_path)
        text = generate_claude_md(info_for(root), self._ev(root), utc_now(), "deadbeef")
        assert "`dogfood-output/`" not in text
        assert "`docs/`" not in text
        assert "`src/`" in text
        assert "`backend/`" in text

    def test_build_table_uses_backticks_once(self, tmp_path):
        root = make_repo(tmp_path)
        text = generate_claude_md(info_for(root), self._ev(root), utc_now(), "deadbeef")
        assert "``vite build``" not in text
        assert "`vite build`" in text

    def test_replaces_prior_generated_block_preserving_handwritten(self):
        existing = (
            "# Hand written\n\nKeep me.\n\n"
            "<!-- BEGIN ONBOARD-GENERATED: L0 CLAUDE.md | source=x | generated=y | onboard@z -->\n"
            "old\n<!-- END ONBOARD-GENERATED -->\n"
        )
        new = "<!-- BEGIN ONBOARD-GENERATED: L0 CLAUDE.md | source=x | generated=n | onboard@z -->\nnew\n<!-- END ONBOARD-GENERATED -->\n"
        out = _replace_generated_block(existing, new)
        assert "Keep me." in out
        assert "old\n" not in out
        assert "new" in out
        assert out.count("<!-- BEGIN ONBOARD-GENERATED") == 1

    def test_appends_when_no_prior_block(self):
        out = _replace_generated_block("# Title\n\nbody\n", "NEWBLOCK\n")
        assert "# Title" in out
        assert "body" in out
        assert out.endswith("NEWBLOCK\n")


class TestGenerateContextMd:
    def test_marks_generated_block(self, tmp_path):
        root = make_repo(tmp_path)
        ev = onboard._collect_evidence(root, info_for(root))
        text = generate_context_md(info_for(root), ev, utc_now(), "deadbeef")
        assert "<!-- BEGIN ONBOARD-GENERATED: L1 CONTEXT.md" in text
        assert "<!-- END ONBOARD-GENERATED -->" in text

    def test_names_declared_dependency_repos(self, tmp_path):
        root = make_repo(tmp_path)
        ev = onboard._collect_evidence(root, info_for(root))
        text = generate_context_md(info_for(root), ev, utc_now(), "deadbeef")
        for dep in ev.dependency_repos:
            assert dep["source"] in text

    def test_lists_failure_edges(self, tmp_path):
        root = make_repo(tmp_path)
        ev = onboard._collect_evidence(root, info_for(root))
        text = generate_context_md(info_for(root), ev, utc_now(), "deadbeef")
        for edge in onboard._FAILURE_EDGES:
            assert f"`{edge}`" in text


class TestGenerateProjectVerify:
    def test_parses_as_yaml_and_declares_every_command(self, tmp_path):
        root = make_repo(tmp_path)
        ev = onboard._collect_evidence(root, info_for(root))
        text = generate_project_verify(ev, info_for(root), utc_now(), "deadbeef")
        import yaml
        data = yaml.safe_load(text)
        assert "verify" in data and data["verify"]
        names = [c["name"] for c in data["verify"]]
        assert ".:test" in names
        for cmd in data["verify"]:
            assert cmd["cmd"], cmd["name"]
            assert cmd["cwd"]

    def test_uses_declared_package_manager(self, tmp_path):
        root = make_repo(tmp_path, package_manager="pnpm")
        ev = onboard._collect_evidence(root, info_for(root))
        text = generate_project_verify(ev, info_for(root), utc_now(), "deadbeef")
        assert "pnpm test" in text
        assert "npm run test" not in text

    def test_documents_hermetic_constraint(self, tmp_path):
        root = make_repo(tmp_path)
        ev = onboard._collect_evidence(root, info_for(root))
        text = generate_project_verify(ev, info_for(root), utc_now(), "deadbeef")
        assert "NETWORK-LESS" in text
        assert "SHADOWS" in text


# ── envit manifest + lock ──────────────────────────────────────────────────

class TestEnvitManifest:
    def _ev(self, deps):
        ev = onboard.Evidence(root=Path("/tmp/x"))
        ev.dependency_repos = deps
        return ev

    def test_keys_skills_by_full_slug_only(self):
        ev = self._ev([
            {"source": "plannotator/guides", "ref": "HEAD", "via": "envit.json"},
            {"source": "redhat-et/ripwire", "ref": "v0.6.0", "via": "envit.json"},
        ])
        manifest = build_envit_manifest(ev, onboard.RepoInfo(slug="a/b"))
        assert set(manifest["skills"]) == {"plannotator/guides", "redhat-et/ripwire"}
        assert "guides" not in manifest["skills"]

    def test_ripwire_gets_explicit_picks_not_wildcard(self):
        ev = self._ev([{"source": "redhat-et/ripwire", "ref": "v0.6.0",
                        "via": "envit.json"}])
        manifest = build_envit_manifest(ev, onboard.RepoInfo(slug="a/b"))
        pick = manifest["skills"]["redhat-et/ripwire"]["pick"]
        assert pick == list(onboard._RIPWIRE_PICKS)
        assert "ripwire-router" in pick

    def test_other_repos_get_wildcard(self):
        ev = self._ev([{"source": "plannotator/guides", "ref": "HEAD",
                        "via": "envit.json"}])
        manifest = build_envit_manifest(ev, onboard.RepoInfo(slug="a/b"))
        assert manifest["skills"]["plannotator/guides"]["pick"] == "*"

    def test_repos_carry_frozen_update_policy(self):
        ev = self._ev([{"source": "plannotator/guides", "ref": "HEAD",
                        "via": "envit.json"}])
        manifest = build_envit_manifest(ev, onboard.RepoInfo(slug="a/b"))
        assert manifest["repos"][0]["update"] == "frozen"


class TestEnvitLock:
    def test_resolves_commit_from_existing_lockfile(self, tmp_path, monkeypatch):
        root = tmp_path / "target"
        root.mkdir()
        (root / ENVIT_LOCK).write_text(json.dumps({
            "version": 1,
            "repos": [{"name": "ripwire", "source": "https://github.com/redhat-et/ripwire.git",
                       "ref": "v0.6.0", "commit": "f" * 40}],
        }))
        monkeypatch.chdir(tmp_path)
        ev = onboard.Evidence(root=root)
        ev.dependency_repos = [{"source": "redhat-et/ripwire", "ref": "v0.6.0",
                                "via": "envit.json"}]
        ev.envit_entries = [{"source": "redhat-et/ripwire", "ref": "v0.6.0",
                             "name": "ripwire"}]
        lock = build_envit_lock(ev, onboard.RepoInfo(slug="a/b"), root)
        entry = next(e for e in lock["repos"] if e["name"] == "ripwire")
        assert entry["commit"] == "f" * 40
        assert entry["ref"] == "v0.6.0"

    def test_unresolvable_entry_is_marked_not_dropped(self, tmp_path, monkeypatch):
        root = tmp_path / "target"
        root.mkdir()
        monkeypatch.chdir(tmp_path)
        ev = onboard.Evidence(root=root)
        ev.dependency_repos = [{"source": "acme/private", "ref": "main",
                                "via": "envit.json"}]
        ev.envit_entries = [{"source": "acme/private", "ref": "main",
                             "name": "private"}]
        lock = build_envit_lock(ev, onboard.RepoInfo(slug="a/b"), root)
        entry = next(e for e in lock["repos"] if e["name"] == "private")
        assert entry["commit"] is None


# ── end-to-end orchestration ───────────────────────────────────────────────

class TestOnboardRepo:
    def test_writes_all_artifacts(self, tmp_path):
        root = make_repo(tmp_path)
        out = tmp_path / "out"
        report = onboard_repo(str(root), write=True, out_root=out, clone=False)
        for name in (L0_FILE, L1_FILE, PROJECT_VERIFY):
            assert (out / name).is_file(), name
        assert (out / ENVIT_MANIFEST).is_file()
        assert (out / ENVIT_LOCK).is_file()
        assert (out / ".envit" / "AGENTS.md").is_file()
        assert report["written"]
        assert report["detected"] is True

    def test_report_only_writes_nothing(self, tmp_path):
        root = make_repo(tmp_path)
        report = onboard_repo(str(root), write=False, out_root=tmp_path / "out",
                              clone=False)
        assert report["written"] == []
        assert not (tmp_path / "out").exists()

    def test_generated_l0_survives_a_rerun(self, tmp_path):
        """Re-running replaces the block; it must not append a second one."""
        root = make_repo(tmp_path)
        onboard_repo(str(root), write=True, clone=False)
        first = (root / L0_FILE).read_text()
        onboard_repo(str(root), write=True, clone=False)
        second = (root / L0_FILE).read_text()
        assert second.count("<!-- BEGIN ONBOARD-GENERATED") == 1
        assert len(second) <= len(first) + 200

    def test_fails_closed_without_a_checkout(self, tmp_path):
        with pytest.raises(OnboardError):
            onboard_repo(str(tmp_path / "nope"), write=True, clone=False)

    def test_requires_evidence_refuses_empty_repo(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(OnboardError):
            onboard_repo(str(empty), write=True, clone=False, require_evidence=True)

    def test_cli_report_only_is_exit_zero(self, tmp_path, capsys):
        root = make_repo(tmp_path)
        code = onboard.main(["--repo", str(root), "--no-clone", "--offline"])
        assert code == 0
        out = capsys.readouterr().out
        assert "[onboard]" in out
        assert "report-only" in out

    def test_cli_json_report_round_trips(self, tmp_path, capsys):
        root = make_repo(tmp_path)
        code = onboard.main(["--repo", str(root), "--no-clone", "--offline", "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["repo"].endswith("demo")
        assert payload["key_paths"]

    def test_cli_missing_repo_is_a_usage_error(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            onboard.main([])
        assert exc.value.code == 2

    def test_format_report_mentions_dependencies(self, tmp_path):
        root = make_repo(tmp_path)
        report = onboard_repo(str(root), write=False, clone=False)
        text = format_report(report)
        assert "dependency repos" in text
        assert "report-only" in text
