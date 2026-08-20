"""N9 guard tests — worktree/terminal bloat boundary.

Pins the boundary that was missing when Orca accumulated 51 terminals and 11
suffix-sprayed teacher worktrees (observed 2026-08-19). Each test encodes a
specific way the audit could quietly stop protecting anything.
"""

import pytest

from worktree_bloat_guard import (
    BloatReport,
    MAX_TERMINALS_PER_WORKTREE,
    audit_residue,
    find_spray,
)


def _terms(worktree: str, n: int) -> list[dict]:
    return [{"worktreePath": f"/w/{worktree}", "handle": f"h{i}"} for i in range(n)]


def _wts(*names: str) -> list[dict]:
    return [{"path": f"/w/{n}"} for n in names]


class TestFindSpray:
    def test_detects_suffixed_persistent_role_clones(self):
        """The exact observed shape."""
        spray = find_spray([
            "teacher-coo-branben__sound-royale-ny",
            "teacher-coo-branben__sound-royale-ny-2",
            "teacher-coo-branben__sound-royale-ny-3",
            "teacher-cto-branben__sound-royale-ny-6",
        ])
        assert "teacher-coo-branben__sound-royale-ny" in spray
        assert spray["teacher-coo-branben__sound-royale-ny"] == [
            "teacher-coo-branben__sound-royale-ny-2",
            "teacher-coo-branben__sound-royale-ny-3",
        ]

    def test_ephemeral_crew_worktrees_are_not_spray(self):
        """Crew worktrees legitimately end in a number.

        `fm-fm-loop-20260820-044951-342` is a live crew worktree. Flagging it
        would make the guard cry wolf on every dispatch and get muted.
        """
        assert find_spray([
            "fm-fm-loop-20260820-044951-342",
            "fm-fm-loop-20260820-051122-343",
        ]) == {}

    def test_real_project_worktrees_are_not_spray(self):
        assert find_spray([
            "KnowledgeCore", "OmniRoute", "school-core", "photonics_tracker",
        ]) == {}

    def test_unsuffixed_persistent_role_alone_is_clean(self):
        assert find_spray([
            "teacher-cto-branben__sound-royale-ny",
            "teacher-coo-branben__sound-royale-ny",
        ]) == {}


class TestAuditResidue:
    def test_flags_the_observed_failure(self):
        """48 terminals in one worktree + 11 teacher clones must FAIL."""
        report = audit_residue(
            list_terminals=lambda: _terms("teacher-coo-x-2", 48),
            list_worktrees=lambda: _wts(
                "teacher-coo-x", "teacher-coo-x-2", "teacher-coo-x-3",
                "teacher-cto-x", "teacher-cto-x-2",
            ),
        )
        assert report.ok is False
        joined = " ".join(report.findings)
        assert "48 terminals" in joined
        assert "teacher-coo-x" in joined
        assert "create_worktree_persistent" in joined, (
            "finding must name the fix, or a reader cannot act on it"
        )

    def test_clean_state_passes(self):
        report = audit_residue(
            list_terminals=lambda: _terms("school-core", 2),
            list_worktrees=lambda: _wts("school-core", "KnowledgeCore"),
        )
        assert report.ok is True
        assert report.findings == []

    def test_post_cleanup_state_passes(self):
        """The state we actually cleaned up to: 0 terminals, 2 canonical roles."""
        report = audit_residue(
            list_terminals=lambda: [],
            list_worktrees=lambda: _wts(
                "school-core", "KnowledgeCore", "OmniRoute",
                "teacher-coo-branben__sound-royale-ny",
                "teacher-cto-branben__sound-royale-ny",
                "fm-fm-loop-20260820-044951-342",
            ),
        )
        assert report.ok is True, report.as_text()

    def test_boundary_is_exclusive_not_off_by_one(self):
        at_limit = audit_residue(
            list_terminals=lambda: _terms("w", MAX_TERMINALS_PER_WORKTREE),
            list_worktrees=lambda: _wts("w"),
        )
        assert at_limit.ok is True, "at the limit must pass"
        over = audit_residue(
            list_terminals=lambda: _terms("w", MAX_TERMINALS_PER_WORKTREE + 1),
            list_worktrees=lambda: _wts("w"),
        )
        assert over.ok is False, "one over the limit must fail"


class TestCliIsAdvisory:
    """The CI entry point must warn, never fail the cycle.

    Residue is a slow leak. Killing a run over a stale worktree would trade a
    real problem (no issues processed) for a cosmetic one. Note this is the
    OPPOSITE call from gateway_preflight, which exits 1 — a dead gateway means
    the cycle cannot do useful work at all.
    """

    def test_exits_zero_when_bloated(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "worktree_bloat_guard.audit_live",
            lambda: BloatReport(
                ok=False, findings=["48 terminals in one worktree (w)"],
                terminal_count=48, worktree_count=22,
            ),
        )
        import worktree_bloat_guard as g

        assert g.main() == 0, "bloat must not fail the cycle"
        out = capsys.readouterr().out
        assert "::warning::" in out, "breach must surface in the run summary"
        assert "48 terminals" in out

    def test_exits_zero_and_warns_when_unknown(self, monkeypatch, capsys):
        """An unreachable daemon must not read as clean."""
        monkeypatch.setattr(
            "worktree_bloat_guard.audit_live",
            lambda: BloatReport(ok=None, detail="orca daemon unreachable"),
        )
        import worktree_bloat_guard as g

        assert g.main() == 0
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "could not run" in out, (
            "UNKNOWN must be reported as unknown, not silently as OK"
        )

    def test_exits_zero_and_is_quiet_when_clean(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "worktree_bloat_guard.audit_live",
            lambda: BloatReport(ok=True, terminal_count=0, worktree_count=12),
        )
        import worktree_bloat_guard as g

        assert g.main() == 0
        out = capsys.readouterr().out
        assert "::warning::" not in out, "a clean audit must not emit warnings"
        assert "OK" in out


class TestAuditNeverBreaksTheCaller:
    def test_inspection_failure_reports_unknown_not_ok(self):
        """A broken audit must not silently become a passing audit.

        This is the fail-open trap that bit the adversarial reviewer twice: an
        error path that returns success is worse than no check at all.
        """
        def boom():
            raise RuntimeError("orca daemon unreachable")

        report = audit_residue(list_terminals=boom, list_worktrees=lambda: [])
        assert report.ok is None, "must be UNKNOWN, never True, on failure"
        assert "orca daemon unreachable" in report.detail
        assert "UNKNOWN" in report.as_text()

    def test_audit_does_not_raise(self):
        audit_residue(list_terminals=lambda: None, list_worktrees=lambda: None)  # type: ignore[arg-type]

    def test_report_is_readable_in_all_three_states(self):
        assert "OK" in BloatReport(ok=True, terminal_count=1, worktree_count=1).as_text()
        assert "BLOAT" in BloatReport(ok=False, findings=["x"]).as_text()
        assert "UNKNOWN" in BloatReport(ok=None, detail="y").as_text()
