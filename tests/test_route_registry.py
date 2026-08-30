"""Tests for deterministic workflow/verifier resolution."""

from pathlib import Path

from route_registry import audit_routes, resolve_route


def _skills(tmp_path):
    root = tmp_path / "skills"
    for name in ("incremental-implementation", "debugging-and-error-recovery", "loop-library", "performance-optimization", "idea-refine", "code-review-and-quality"):
        path = root / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"# {name}\n")
    return [root]


def test_missing_ce_work_uses_explicit_installed_fallback(tmp_path):
    resolution = resolve_route("ce-work", skill_roots=_skills(tmp_path))

    assert resolution.status == "ready"
    assert resolution.skill == "incremental-implementation"
    assert resolution.fallback is True
    assert "pytest" in resolution.verifier


def test_delegated_verification_never_silently_passes(tmp_path):
    resolution = resolve_route("verify-delegated-work", skill_roots=_skills(tmp_path))

    assert resolution.status == "ready"
    assert resolution.fallback is True
    assert resolution.skill == "code-review-and-quality"
    assert "test_review_packet" in resolution.verifier


def test_unknown_or_missing_fallback_is_blocked(tmp_path):
    resolution = resolve_route("unknown-workflow", skill_roots=_skills(tmp_path))

    assert resolution.status == "blocked"
    assert resolution.verifier is None


def test_audit_reports_all_targets_and_blockers(tmp_path):
    report = audit_routes(
        ["ce-work", "ce-debug", "unknown-workflow"],
        skill_roots=_skills(tmp_path),
    )

    assert report["status"] == "blocked"
    assert report["blocked_targets"] == ["unknown-workflow"]
    assert len(report["targets"]) == 3
