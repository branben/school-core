"""Execution evidence in the two-teacher review (BUILD lens).

The repo verify gate (verify_gate.py) now feeds REAL hermetic build/test
results into ``_run_two_judge_review``: real failures are CRITICAL
auto-vetoes (the repo's declared verify contract failing on the actual
checkout is hard evidence, not model opinion), skips are loud advisory
findings (never a fabricated pass), and the bookbag carries the raw
verification output (the ``verification`` field from HANDOFF.md's schema).
"""

from pathlib import Path

from unittest.mock import patch

import director

from director import _run_two_judge_review  # noqa: E402
from adversarial_reviewer import ReviewResult, Verdict  # noqa: E402
from bookbag import write_bookbag, read_bookbag  # noqa: E402


class _FakeReviewer:
    """Returns an unanimous PASS so acceptance is decided by build evidence."""

    def __init__(self, call_model_fn=None):
        self.call_model_fn = call_model_fn

    def review(self, **kwargs):
        return ReviewResult(verdict=Verdict.PASS, findings=[])


def _review_with_build_gate(monkeypatch, repo_path, vg_result, bead="bead-build-gate", gate=None):
    repo = "branben/sound-royale-ny"
    write_bookbag(
        bead,
        student="coder",
        domain="python-testing",
        difficulty="medium",
        task="write tests",
        output="def test_x(): pass",
        repo=repo,
    )
    monkeypatch.setattr(
        "director._resolve_repo_path", lambda repo: repo_path
    )
    monkeypatch.setattr(
        "director.run_verify_gate",
        gate or (lambda repo_path, project_verify=None, **kwargs: vg_result),
    )
    with patch("director.AdversarialReviewer", _FakeReviewer), \
         patch("director.call_model", side_effect=RuntimeError("no model in tests")):
        return _run_two_judge_review(
            bead=bead,
            output="def test_x(): pass",
            task={"domain": "python-testing", "difficulty": "medium"},
            repo=repo,
        )


def _finding(result, issue_class):
    return next((f for f in result["findings"] if f.get("issue_class") == issue_class), None)


def test_build_verify_real_failure_vetoes_review(monkeypatch, tmp_path):
    """A real verify-gate failure (compile/test/lint) is CRITICAL → auto-veto,
    even when both teachers would PASS."""
    vg = {
        "passed": False,
        "failures": [{
            "cmd": "python3 -m compileall -q *.py",
            "exit": 1,
            "stderr": "SyntaxError: invalid syntax in director.py",
        }],
        "ran": 1,
    }
    result = _review_with_build_gate(monkeypatch, tmp_path, vg)

    assert result["accepted"] is False
    finding = _finding(result, "verify_failed")
    assert finding is not None
    assert finding["severity"] == "CRITICAL"
    assert "SyntaxError" in finding["description"]
    assert result["build_verification"] is not None
    assert "passed" in result["build_verification"]

    bag = read_bookbag("bead-build-gate", repo="branben/sound-royale-ny")
    assert bag is not None
    assert bag.get("verification") is not None
    assert "SyntaxError" in bag["verification"]


def test_build_verify_skipped_is_advisory_not_veto(monkeypatch, tmp_path):
    """A skipped gate (no Nix / no declared commands) is a loud LOW advisory:
    it must not fabricate a pass, but it must not reject either."""
    vg = {
        "passed": False,
        "skipped": True,
        "failures": [{"cmd": "(nix)", "exit": None, "stderr": "Nix not found — verify gate SKIPPED."}],
        "ran": 0,
    }
    result = _review_with_build_gate(monkeypatch, tmp_path, vg)

    assert result["accepted"] is True
    finding = _finding(result, "verification_skipped")
    assert finding is not None
    assert finding["severity"] == "LOW"
    assert "Nix not found" in finding["description"]


def test_build_verify_passed_adds_positive_evidence(monkeypatch, tmp_path):
    """A green gate adds a positive LOW finding and lands in the bookbag."""
    vg = {"passed": True, "failures": [], "ran": 1}
    result = _review_with_build_gate(monkeypatch, tmp_path, vg)

    assert result["accepted"] is True
    finding = _finding(result, "verification_passed")
    assert finding is not None
    assert finding["severity"] == "LOW"
    bag = read_bookbag("bead-build-gate", repo="branben/sound-royale-ny")
    assert '"passed": true' in bag.get("verification", "")


def test_build_verify_no_repo_path_is_loudly_skipped(monkeypatch, tmp_path):
    """No resolvable checkout records a visible soft-skip, not a silent pass."""
    def _boom(*args, **kwargs):
        raise AssertionError("run_verify_gate must not be called without a repo path")

    monkeypatch.setattr("director.run_verify_gate", _boom)
    result = _review_with_build_gate(monkeypatch, None, None)

    assert result["accepted"] is True
    assert result["build_verification"] is not None
    finding = _finding(result, "verification_skipped")
    assert finding is not None
    assert finding["severity"] == "LOW"
    assert "checkout" in finding["description"].lower()


def test_build_verify_pins_flake_to_director_checkout(monkeypatch, tmp_path):
    """The review gate must not depend on the process working directory."""
    captured = {}

    def fake_gate(**kwargs):
        captured.update(kwargs)
        return {"passed": True, "failures": [], "ran": 1}

    result = _review_with_build_gate(monkeypatch, tmp_path, None, gate=fake_gate)

    assert result["accepted"] is True
    assert captured["flake_path"] == Path(director.__file__).resolve().parent


def test_build_verify_gate_error_is_strict_veto(monkeypatch, tmp_path):
    """Strict mode must reject an internal gate error instead of accepting it."""
    monkeypatch.setenv("VERIFY_GATE_STRICT", "1")

    def _boom(*args, **kwargs):
        raise RuntimeError("copytree failed")

    result = _review_with_build_gate(monkeypatch, tmp_path, None, gate=_boom)

    assert result["accepted"] is False
    finding = _finding(result, "verify_gate_error")
    assert finding is not None
    assert finding["severity"] == "CRITICAL"
    assert "copytree failed" in finding["description"]
    assert result["build_verification"] is not None


def test_build_verify_disabled_by_env(monkeypatch, tmp_path):
    """REVIEW_RUN_VERIFY_GATE=0 is the kill-switch for hermetic runs."""
    monkeypatch.setenv("REVIEW_RUN_VERIFY_GATE", "0")

    def _boom(*args, **kwargs):
        raise AssertionError("run_verify_gate must not run when disabled")

    monkeypatch.setattr("director.run_verify_gate", _boom)
    result = _review_with_build_gate(monkeypatch, tmp_path, None)

    assert result["accepted"] is True
    assert result["build_verification"] is None
    assert result["build_findings"] == []
