"""The crew brief must specify the report's evidence syntax, not just describe it.

WHY THIS EXISTS
---------------
The crew has never completed a REAL issue. student-whymage's reverse why-tree
identified the artifact handshake as the blocker behind admission, and the cause
is a brief/parser asymmetry in ``_crew_brief``:

    crew_dispatch.py:397-399  (status line — EXACT template given)
        "You append the final `done:` status naming the branch, commit, and
         base identity in this exact form:
             done: branch=<branch> commit=<commit> base=<base>"

    crew_dispatch.py:395-396  (report.md — PROSE ONLY, no template)
        "`report.md` exists at the report path above and names the branch,
         commit, and base identity."

``_artifact_identity`` (crew_dispatch.py:195+) then requires ALL THREE of
branch/commit/base to parse out of report.md, and
``crew_dispatch.py:860-890`` hard-fails the run when the report identity is
absent (``artifact_status_evidence_missing``) or disagrees with the status line
(``artifact_identity_mismatch``). Both appear in data/crew_runs.json.

So the agent is told the exact syntax for the machine-readable status and left
to improvise the report — and the parser only accepts specific shapes:
a ``field: value`` / ``field=value`` pair, or a ``## Branch`` heading followed
by a bare backticked bullet. There is also a documented gotcha: inside a
``## Base`` section a nested ``branch`` is treated as descriptive and must NOT
override the task branch.

An improvised report fails the gate. That is a brief defect, not a model defect:
nothing in the instructions could have told the agent what shape to emit.

These tests pin the brief to a template the parser demonstrably accepts, by
round-tripping the brief's own example through the real parser.
"""

import re

import crew_dispatch


def _brief(tmp_path) -> str:
    """Render the crew brief and return its text.

    ``_write_brief`` writes brief.md into the crew task dir, so point the task
    dir at a tmp path and read the file back.
    """
    crew_dispatch._task_dir = lambda cid: tmp_path / cid  # type: ignore[assignment]
    path = crew_dispatch._write_brief(
        crew_id="test-crew-1",
        task_text="implement the thing",
        issue_number=77,
        project_dir=tmp_path / "project",
    )
    return path.read_text(encoding="utf-8")


class TestBriefSpecifiesReportEvidenceSyntax:
    def test_brief_shows_an_explicit_report_template(self, tmp_path):
        """The report instruction must include a copyable template.

        REGRESSION: the brief gave an exact form for the `done:` status line but
        only prose for report.md, so the agent improvised a shape the parser
        rejected — artifact_status_evidence_missing / artifact_identity_mismatch.
        """
        brief = _brief(tmp_path)
        # A template means the three field names appear together in a
        # machine-shaped block, not merely mentioned in a sentence.
        assert re.search(r"branch\s*[:=]", brief, re.IGNORECASE), (
            "brief never shows a `branch:` / `branch=` field form for report.md"
        )
        assert re.search(r"commit\s*[:=]", brief, re.IGNORECASE)
        assert re.search(r"base\s*[:=]", brief, re.IGNORECASE)

    def test_the_briefs_report_template_actually_parses(self, tmp_path):
        """Round-trip: the template the brief hands the agent must satisfy the gate.

        This is the test that matters. A template that looks reasonable but does
        not parse would reproduce the same failure with extra confidence.
        """
        brief = _brief(tmp_path)

        # Extract the report template block the brief provides. It must contain
        # all three fields; feed exactly that text to the real parser.
        candidates = re.findall(
            r"(?:^|\n)((?:[ \t]*(?:[-*]\s*)?(?:branch|commit|base)\s*[:=][^\n]*\n?){3,})",
            brief,
            re.IGNORECASE,
        )
        assert candidates, (
            "no three-field evidence block found in the brief to hand the agent"
        )

        parsed_any = False
        for block in candidates:
            sample = block.replace("<branch>", "fm/issue-77-add-a-thing")
            sample = sample.replace("<commit>", "a1b2c3d4e5f6")
            sample = sample.replace("<base>", "main@0f0f0f0f")
            identity = crew_dispatch._artifact_identity(sample)
            if identity is not None:
                parsed_any = True
                assert identity["branch"], f"branch missing from {identity}"
                assert identity["commit"], f"commit missing from {identity}"
                assert identity["base"], f"base missing from {identity}"
        assert parsed_any, (
            "the brief's own evidence template does NOT parse via "
            "_artifact_identity — the agent would fail the gate while following "
            "instructions exactly"
        )

    def test_brief_still_specifies_the_status_line_form(self, tmp_path):
        """Guard: the status-line template must not be lost."""
        brief = _brief(tmp_path)
        assert "done: branch=" in brief, "status-line template regressed"

    def test_brief_warns_about_the_base_section_gotcha(self, tmp_path):
        """The parser treats a branch nested under ## Base as descriptive.

        An agent that writes its task branch inside a Base section produces a
        mismatch. The brief should steer away from that shape.
        """
        brief = _brief(tmp_path)
        assert "base" in brief.lower(), "brief does not mention base identity"
