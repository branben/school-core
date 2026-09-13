"""Tests for scripts/prime.py — the ticket-scoped context-block emitter.

Covers the pure, deterministic parts of Prime: ticket-reference parsing,
keyword-based relevant-file scoring, guardrail/verification shaping, and the
handoff block layout. Network-backed loaders (gh / bd / obsidian) are mocked
so the suite never depends on a live bridge or issue track.
"""

import subprocess

import pytest

import scripts.prime as prime
from scripts.prime import (
    _default_repo,
    _emit_markdown,
    _guardrails,
    _parse_issue_ref,
    _relevant_files,
    _verification,
    load_ticket,
)

SAMPLE_TICKET = {
    "source": "github",
    "id": "branben/school-core#26",
    "title": "test: Add tests for untested campus modules and code review",
    "body": (
        "## What to build\n"
        "Code review identified 29+ testing gaps. Key areas:\n"
        "- Core: Campus.run() / run_async(), _handle_signal(), "
        "_auto_load_agents() with corrupt files\n"
        "- Memory: save_observation subprocess failure\n"
    ),
    "labels": ["ready-for-agent"],
    "state": "OPEN",
    "milestone": None,
    "url": "https://github.com/branben/school-core/issues/26",
    "author": "branben",
    "created_at": "2026-08-18T00:00:00Z",
    "updated_at": "2026-08-18T00:00:00Z",
}


def test_parse_issue_ref_formats():
    assert _parse_issue_ref("owner/repo#123") == ("owner", "repo", 123)
    assert _parse_issue_ref("#123") is None  # resolved against default repo
    assert _parse_issue_ref(
        "https://github.com/branben/school-core/issues/42"
    ) == ("branben", "school-core", 42)
    assert _parse_issue_ref("docs/plans/foo.md") is None
    assert _parse_issue_ref("BEAD-7") is None


def test_default_repo_resolves_git_origin():
    slug = _default_repo()
    assert "/" in slug
    assert not slug.endswith(".git")
    assert slug == "branben/school-core"


@pytest.fixture
def tiny_repo(tmp_path):
    """A git repo with a few source files to score against."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "campus").mkdir()
    (repo / "src" / "campus" / "runner.py").write_text("def run():\n    pass\n")
    (repo / "src" / "campus" / "memory.py").write_text("def save():\n    pass\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_campus_runner.py").write_text(
        "def test_run():\n    assert True\n"
    )
    (repo / "README.md").write_text("# Campus runner\n")
    (repo / ".gitignore").write_text("data/\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "prime-test"], check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "prime@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"], check=True,
        capture_output=True,
    )
    return repo


def test_relevant_files_scores_filename_hits(tiny_repo, monkeypatch):
    monkeypatch.setattr(prime, "ROOT", tiny_repo)
    ticket = {
        "title": "test campus runner regression",
        "body": "campus runner memory save",
    }
    files = _relevant_files(ticket, max_files=8)
    joined = "\n".join(files)
    assert "src/campus/runner.py" in files
    assert "tests/test_campus_runner.py" in files
    # runtime data and git metadata are never candidates
    assert all(not f.startswith("data/") for f in files)
    assert ".git" not in joined


def test_relevant_files_excludes_data_dir(tiny_repo, monkeypatch):
    monkeypatch.setattr(prime, "ROOT", tiny_repo)
    (tiny_repo / "data").mkdir()
    (tiny_repo / "data" / "runner_state.json").write_text("{}")
    subprocess.run(["git", "-C", str(tiny_repo), "add", "-f", "data"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tiny_repo), "commit", "-qm", "add data"],
                   check=True, capture_output=True)
    files = _relevant_files({"title": "runner state", "body": "runner"}, 8)
    assert all(not f.startswith("data/") for f in files)


def test_relevant_files_empty_when_no_match(tiny_repo, monkeypatch):
    monkeypatch.setattr(prime, "ROOT", tiny_repo)
    files = _relevant_files({"title": "zzz nonexistent", "body": "qqqq zzzz"}, 8)
    assert files == []


def test_guardrails_include_vault_and_beads_and_github_source(tmp_path):
    rules = _guardrails(tmp_path, {**SAMPLE_TICKET, "source": "github"})
    joined = "\n".join(rules).lower()
    assert "beads" in joined
    assert "allowlist" in joined
    assert "pr" in joined  # live-github guardrail


def test_guardrails_no_github_pr_rule_for_local_source(tmp_path):
    rules = _guardrails(tmp_path, {**SAMPLE_TICKET, "source": "file"})
    assert not any("do not push" in r.lower() for r in rules)


def test_verification_suggests_checks_for_github_source():
    checks = _verification(
        {"source": "github", "id": "owner/repo#9", "title": "#9"},
        ["tests/test_foo.py"],
    )
    joined = "\n".join(checks)
    assert "CI green" in joined
    assert "pytest" in joined
    assert "tests/test_foo.py" in joined


def test_emit_markdown_has_all_six_sections(tmp_path):
    block = _emit_markdown(SAMPLE_TICKET, _fake_args(tmp_path))
    for section in [
        "## Metadata",
        "## Task",
        "## Code Paths",
        "## Pre-Dispatch Evidence",
        "## Guardrails",
        "## Verification",
    ]:
        assert section in block
    assert SAMPLE_TICKET["title"] in block
    assert "Candidate files (keyword-scored" in block or "Repo tree" in block


def test_emit_markdown_degraded_when_no_context(tmp_path):
    ticket = {
        **SAMPLE_TICKET,
        "body": "",
        "id": "STUB-99",
        "title": "VerdantKelpOtterDelta",
        "source": "beads",
    }
    block = _emit_markdown(ticket, _fake_args(tmp_path))
    assert "STUB-99" in block
    assert "No candidate files matched" in block


def _fake_args(repo_path):
    class _Args:
        def __init__(self, repo_path):
            self.ticket = "branben/school-core#26"
            self.repo = str(repo_path)
            self.out = ""
            self.top_k = 3
            self.no_obsidian = True
            self.no_orca = True
            self.as_json = False

    return _Args(repo_path)


def test_load_ticket_error_with_no_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(prime, "_bd_show", lambda t: None)
    monkeypatch.setattr(prime, "_beads_jsonl", lambda t: None)
    monkeypatch.setattr(prime, "_gh_issue", lambda t: None)
    monkeypatch.setattr(prime, "_local_doc", lambda t: None)
    with pytest.raises(ValueError):
        load_ticket("NOPE-1")


def test_load_ticket_uses_beads_jsonl(monkeypatch, tmp_path):
    import json as _json
    beads = tmp_path / ".beads"
    beads.mkdir()
    (beads / "issues.jsonl").write_text(
        _json.dumps({
            "id": "BEAD-7", "title": "Slice the pipeline",
            "body": "one testable concern", "labels": ["slice"],
            "status": "open",
        })
    )
    monkeypatch.setattr(prime, "ROOT", tmp_path)
    rec = load_ticket("BEAD-7")
    assert rec["source"] == "beads"
    assert rec["title"] == "Slice the pipeline"
    assert rec["body"] == "one testable concern"


def test_overlapping_orca_filters_to_school_core():
    from scripts.prime import _overlapping_orca

    wts = [
        {"path": "/Users/brandonbennett/Documents/KnowledgeCore", "branch": "main"},
        {"path": "/Users/brandonbennett/school-core", "branch": "chore/condense-pipeline"},
        {"path": "/Users/brandonbennett/orca/workspaces/school-core/teacher-coo-branben__school-core",
         "branch": "branben/teacher-coo-branben__school-core"},
        {"path": "/Users/brandonbennett/actions-runner/_work/school-core/school-core",
         "branch": "main"},
    ]
    overlaps = _overlapping_orca(["tests/test_foo.py"], wts)
    paths = [o["path"] for o in overlaps]
    assert "/Users/brandonbennett/school-core" in paths
    assert "/Users/brandonbennett/orca/workspaces/school-core/teacher-coo-branben__school-core" in paths
    assert "/Users/brandonbennett/actions-runner/_work/school-core/school-core" in paths
    # unrelated vault checkout is not an overlap risk for this repo
    assert all("KnowledgeCore" not in p for p in paths)
    assert _overlapping_orca(["tests/test_foo.py"], None) == []
    assert _overlapping_orca([], wts) == []