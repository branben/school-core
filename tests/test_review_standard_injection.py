"""The review checklist must actually reach the reviewer's system prompt.

`docs/quality-gates-checklist.md` is cited as canonical by the `code-quality-gates`
skill and by `config/profiles/student-reviewer/SOUL.md` ("checklist-driven review").
Before this wiring, nothing read it: `grep -nE 'read_text|open\\(|checklist|STANDARD'
src/review/adversarial_reviewer.py` returned zero hits.

Two properties are pinned here:
  1. when the standard exists, its text is IN the system prompt;
  2. when it is absent, that fact is RECORDED, not silently skipped. A reviewer that
     quietly reviews without its standard is the fail-open pattern this repo keeps
     paying for -- "no standard" and "standard applied" must not look identical.

Run: python -m pytest tests/test_review_standard_injection.py -v
"""

import json
import logging
import pathlib
import re

import pytest

from src.review.adversarial_reviewer import (
    DEFAULT_REVIEW_STANDARD_PATH,
    AdversarialReviewer,
    LensType,
)

PASS_JSON = json.dumps(
    {"findings": [], "verdict": "PASS", "score": 90, "confidence": 0.8}
)


def _capture_review(standard_path):
    """Run one review, returning (result, captured_prompts)."""
    captured = {}

    def call_model(prompt, system_prompt=None, **kw):
        captured["system"] = system_prompt
        captured["user"] = prompt
        return PASS_JSON

    reviewer = AdversarialReviewer(
        call_model_fn=call_model, review_standard_path=standard_path
    )
    result = reviewer.review(
        output="def f():\n    return 1\n",
        task={"title": "t", "body": "b", "domain": "code-implementation"},
        lens_types=[LensType.CORRECTNESS],
    )
    return result, captured, reviewer


class TestStandardReachesThePrompt:
    def test_standard_text_is_in_the_system_prompt(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("UNIQUE_MARKER_ABC123")
        _, captured, _ = _capture_review(std)
        assert "UNIQUE_MARKER_ABC123" in captured["system"]

    def test_standard_block_is_labelled(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("rule one")
        _, captured, _ = _capture_review(std)
        assert "[REVIEW STANDARD]" in captured["system"]

    def test_standard_does_not_pollute_codebase_context(self, tmp_path):
        """codebase_context is a FILE-REFERENCE signal for heuristic_scorer.

        Injecting the checklist there would corrupt the grounding score, so the
        standard must travel in the system prompt only.
        """
        std = tmp_path / "checklist.md"
        std.write_text("UNIQUE_MARKER_ABC123")
        _, captured, _ = _capture_review(std)
        assert "UNIQUE_MARKER_ABC123" not in captured["user"]


class TestMissingStandardIsVisible:
    def test_absent_standard_omits_the_block(self, tmp_path):
        missing = tmp_path / "absent.md"
        _, captured, _ = _capture_review(missing)
        assert "[REVIEW STANDARD]" not in captured["system"]

    def test_absent_standard_is_recorded_on_the_reviewer(self, tmp_path):
        missing = tmp_path / "absent.md"
        _, _, reviewer = _capture_review(missing)
        assert reviewer.standard_loaded is False

    def test_absent_standard_logs_a_warning(self, tmp_path, caplog):
        missing = tmp_path / "absent.md"
        with caplog.at_level(logging.WARNING):
            _capture_review(missing)
        assert any("standard" in r.message.lower() for r in caplog.records), (
            "a review that ran without its checklist must say so"
        )

    def test_present_standard_is_recorded_as_loaded(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("rule one")
        _, _, reviewer = _capture_review(std)
        assert reviewer.standard_loaded is True

    def test_empty_standard_file_counts_as_absent(self, tmp_path):
        std = tmp_path / "empty.md"
        std.write_text("   \n\n")
        _, captured, reviewer = _capture_review(std)
        assert reviewer.standard_loaded is False
        assert "[REVIEW STANDARD]" not in captured["system"]


class TestDefaultPath:
    def test_default_path_names_the_canonical_artifact(self):
        assert DEFAULT_REVIEW_STANDARD_PATH.name == "quality-gates-checklist.md"
    def test_default_path_resolves_inside_the_repo(self):
        assert DEFAULT_REVIEW_STANDARD_PATH.parent.name == "docs"

    def test_the_canonical_checklist_exists(self):
        """The two references (skill + reviewer SOUL) must point at a real file."""
        assert DEFAULT_REVIEW_STANDARD_PATH.exists(), (
            f"{DEFAULT_REVIEW_STANDARD_PATH} is cited as canonical but is absent"
        )


class TestRetryPathKeepsTheStandard:
    """The parse-failure retry fires exactly when things are already going wrong.

    Regression (found by an adversarial review, then confirmed by reading
    adversarial_reviewer.py): the retry rebuilt its system prompt inline and
    dropped BOTH the [LENS] block and the [REVIEW STANDARD] block. The retry
    result is the one that gets RETURNED, so the answer a caller receives could
    come from a prompt carrying no checklist at all -- the checklist's own
    thesis (silence is not a pass) violated by its carrier.
    """

    def test_retry_system_prompt_carries_the_standard(self, tmp_path):
        std = tmp_path / "checklist.md"
        std.write_text("UNIQUE_MARKER_ABC123")
        seen = []

        def call_model(prompt, system_prompt=None, **kw):
            seen.append(system_prompt)
            if len(seen) == 1:
                return "this is not json at all, and it is longer than twenty chars"
            return PASS_JSON

        reviewer = AdversarialReviewer(
            call_model_fn=call_model, review_standard_path=std
        )
        reviewer.review(
            output="def f():\n    return 1\n",
            task={"title": "t", "body": "b", "domain": "code-implementation"},
            lens_types=[LensType.CORRECTNESS],
        )
        assert len(seen) >= 2, "the parse-failure retry did not fire"
        assert "UNIQUE_MARKER_ABC123" in seen[1], (
            "the retry system prompt dropped the review standard"
        )


class TestReferenceImplementationParityRule:
    """Section 9 of the checklist pins the reference-implementation parity rule.

    This test exists because a checklist that is not loaded by the reviewer
    provides no protection. The outer-loop gate pins *that the file exists*;
    this class pins *that it contains the new section*.
    """

    def test_section_9_is_present_in_canonical_checklist(self):
        text = DEFAULT_REVIEW_STANDARD_PATH.read_text(encoding="utf-8")
        assert "## 9. Reference-implementation parity" in text

    def test_section_9_requires_named_reference_diff(self):
        text = DEFAULT_REVIEW_STANDARD_PATH.read_text(encoding="utf-8")
        assert "Named reference, not doc-only" in text
        assert "file:line" in text

    def test_section_9_requires_db_aware_flag_gate(self):
        text = DEFAULT_REVIEW_STANDARD_PATH.read_text(encoding="utf-8")
        assert "Flag-gate contract" in text
        assert "isFeatureFlagEnabled" in text

    def test_section_9_requires_db_override_test(self):
        text = DEFAULT_REVIEW_STANDARD_PATH.read_text(encoding="utf-8")
        assert "Test the DB-override path" in text
        assert "DB override" in text


OMNIROUTE_ROOT = DEFAULT_REVIEW_STANDARD_PATH.parents[2] / "OmniRoute"
_CITATION = re.compile(r"`([\w./-]+\.(?:ts|tsx|js|py|md)):(\d+)`")
_SYMBOL = re.compile(r"`(\w+)\(`|`(\w+)`")


def _bullets(text):
    """Split markdown into bullets: a '- ' line plus its indented continuation lines."""
    out, cur = [], []
    for line in text.splitlines():
        if re.match(r"^\s*-\s", line):
            if cur:
                out.append(" ".join(cur))
            cur = [line]
        elif cur and line.startswith(" ") and line.strip():
            cur.append(line.strip())
        else:
            if cur:
                out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _locate(path, root):
    """Find a cited file by exact relative path, else by unique basename under mcp-server.

    Returns (file, error). A bare basename shared by several files (`server.ts` is both
    v1 and v2) is an error, not a guess: the citation must use a full path.
    """
    exact = root / path
    if exact.exists():
        return exact, None
    if "/" in path:
        return None, None  # belongs to another repo or a different checkout
    hits = [p for p in (root / "open-sse" / "mcp-server").rglob(path) if "__tests__" not in p.parts]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, f"{path} is ambiguous ({len(hits)} files); cite the full path"
    return None, None


def _symbol_mismatches(text, root):
    """Return (checked, bad): citations whose cited line does not name a symbol in the same bullet."""
    checked, bad = 0, []
    for bullet in _bullets(text):
        cites = list(_CITATION.finditer(bullet))
        symbols = [a or b for a, b in _SYMBOL.findall(bullet)]
        if not cites or not symbols:
            continue
        for m in cites:
            path, line_no = m.group(1), int(m.group(2))
            target, err = _locate(path, root)
            if err:
                bad.append(err)
                continue
            if target is None:
                continue
            lines = target.read_text(encoding="utf-8").splitlines()
            if not (1 <= line_no <= len(lines)):
                bad.append(f"{path}:{line_no} is past end of file")
                continue
            checked += 1
            if not any(s in lines[line_no - 1] for s in symbols):
                bad.append(f"{path}:{line_no} does not name any of {symbols}")
    return checked, bad


class TestCitationsResolve:
    """A `file:line` citation must point at a real line that names its symbol.

    PR #15983 review cited `v2/server.ts:129` and `server.ts:230` for v1's
    `withScopeEnforcement`; the function was at `server.ts:180`. A wrong citation
    passes every string check, so this test resolves each one against the source.
    """

    def test_checklist_citations_resolve_to_named_symbols(self):
        text = DEFAULT_REVIEW_STANDARD_PATH.read_text(encoding="utf-8")
        # Only citations of the form `path:line` followed by a symbol in the same
        # bullet are checked; prose examples without a symbol are skipped.
        bad = []
        for m in _CITATION.finditer(text):
            path, line_no = m.group(1), int(m.group(2))
            target = DEFAULT_REVIEW_STANDARD_PATH.parents[1] / path
            if not target.exists():
                continue  # cited path belongs to another repo; checked below
            lines = target.read_text(encoding="utf-8").splitlines()
            if not (1 <= line_no <= len(lines)):
                bad.append(f"{path}:{line_no} is past end of file")
        assert not bad, "\n".join(bad)

    def test_citation_names_its_symbol_in_the_cited_line(self, tmp_path):
        """A `file:line` citation followed by a symbol must match that line.

        Red fixture: the PR #15983 bug, a citation pointing at a line that does
        not name the function the bullet attributes to it.
        """
        src = tmp_path / "server.ts"
        src.write_text(
            "import x from 'y';\n\nfunction other() {}\n\nfunction withScopeEnforcement() {}\n",
            encoding="utf-8",
        )
        bullet = "- `server.ts:3` (`withScopeEnforcement`) gates the call"
        checked, bad = _symbol_mismatches(bullet, tmp_path)
        assert checked == 1
        assert bad == ["server.ts:3 does not name any of ['withScopeEnforcement']"]

    def test_checklist_symbol_citations_name_their_symbols(self):
        text = DEFAULT_REVIEW_STANDARD_PATH.read_text(encoding="utf-8")
        checked, bad = _symbol_mismatches(text, OMNIROUTE_ROOT)
        assert checked >= 1, "no checklist citation was resolvable; the check is blind"
        # Known limit: a citation of a gate call (`server.ts:190`) names the gate, not the
        # bullet's function symbols, so the symbol check cannot verify it. Its existence
        # is still checked by test_checklist_citations_resolve_to_named_symbols.
        bad = [b for b in bad if "server.ts:190" not in b]
        assert not bad, "\n".join(bad)

    def test_v1_scope_enforcement_citation_is_correct(self):
        """Pins the corrected v1 citation against the OmniRoute source."""
        src = OMNIROUTE_ROOT / "open-sse/mcp-server/server.ts"
        if not src.exists():
            pytest.skip("OmniRoute checkout not present")
        lines = src.read_text(encoding="utf-8").splitlines()
        assert "function withScopeEnforcement(" in lines[179], (
            "server.ts:180 no longer names withScopeEnforcement"
        )
        assert "isMcpScopeEnforcementEnabled" in lines[189], (
            "server.ts:190 no longer names the scope-enforcement gate"
        )


def _module_scope_flag_reads(path, flags):
    """Return (line, flag) for each module-scope `const` statement that reads a flag.

    A statement runs from its `const` line to the next line ending in `;`, so a read
    wrapped onto a continuation line is caught. Indented function bodies are skipped.
    """
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    hits = []
    i = 0
    while i < len(lines):
        if re.match(r"^(export\s+)?const\s+\w+", lines[i]):
            start, stmt = i, []
            while i < len(lines):
                stmt.append(lines[i])
                if lines[i].rstrip().endswith(";"):
                    break
                i += 1
            text = "\n".join(stmt)
            for flag in flags:
                if f"process.env.{flag}" in text:
                    hits.append((start + 1, flag))
        i += 1
    return hits


class TestFlagGateScan:
    """No mcp-server module may read a DB-overridable flag from process.env at load.

    `isMcpV2Enabled()` once read only process.env while OMNIROUTE_MCP_V2 was
    registered as DB-overridable, so the dashboard toggle silently did nothing.
    """

    def _db_overridable_flags(self):
        defs = OMNIROUTE_ROOT / "src/shared/constants/featureFlagDefinitions.ts"
        text = defs.read_text(encoding="utf-8")
        # Each flag entry carries `key: "OMNIROUTE_..."`; a flag is treated as
        # DB-overridable when its entry is not marked env-only.
        entries = re.split(r"\n  \{\n", text)
        flags = set()
        for entry in entries:
            key = re.search(r'key: "(OMNIROUTE_\w+)"', entry)
            if key and "envOnly: true" not in entry:
                flags.add(key.group(1))
        return flags

    def test_scan_catches_wrapped_module_scope_read(self, tmp_path):
        """Red fixture: a module-scope read split across lines must still be caught."""
        src = tmp_path / "mod.ts"
        src.write_text(
            "const enabled =\n  process.env.OMNIROUTE_MCP_V2 === 'true';\n"
            "function f() { return process.env.OMNIROUTE_MCP_V2; }\n",
            encoding="utf-8",
        )
        assert _module_scope_flag_reads(src, {"OMNIROUTE_MCP_V2"}) == [
            (1, "OMNIROUTE_MCP_V2")
        ]

    def test_no_module_scope_env_read_of_db_overridable_flags(self):
        if not (OMNIROUTE_ROOT / "open-sse/mcp-server").exists():
            pytest.skip("OmniRoute checkout not present")
        flags = self._db_overridable_flags()
        assert flags, "found no flag definitions; the scan is blind"
        offenders = []
        for path in (OMNIROUTE_ROOT / "open-sse/mcp-server").rglob("*.ts"):
            if "__tests__" in path.parts:
                continue
            for n, flag in _module_scope_flag_reads(path, flags):
                offenders.append(f"{path.relative_to(OMNIROUTE_ROOT)}:{n} reads {flag}")
        assert not offenders, "\n".join(offenders)
