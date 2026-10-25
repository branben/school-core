"""Outer-loop gate: the rule that a gate must never be fail-open.

The outer loop (issue -> PR -> review) is enforced by a pre-commit hook in
.beads/hooks/pre-commit that runs `bd lint`. These tests pin the two properties
that make that gate trustworthy, and pin the trap it must avoid.

The trap, measured 2026-10-24:

    $ bd query 'spec=nonexistent-xyz' >/dev/null; echo $?
    0                      # <-- rc=0 with NOTHING matched

`bd query` returns exit code 0 and the text "No issues found" when no issue
matches, and exit code 1 only for a malformed expression. So the exit code
cannot distinguish "found" from "not found" -- a guard written as
`bd query 'spec=X' || fail` PASSES while matching nothing. That is fail-open:
the gate is green and the thing it was meant to guarantee is absent.

Note also that `bd query` does not accept a second term:
`bd query 'type=task spec=X'` is a syntax error (rc=1), not an empty result.

The gate therefore uses `bd lint`, which DOES exit non-zero when an open work
item is missing a required section -- that is a real signal, not an absence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".beads" / "hooks" / "pre-commit"

#: Work types the gate is responsible for. `decision` and `epic` are containers,
#: not work, and may legitimately be unresolved (see the hook's own comment).
GATED_TYPES = ("task", "bug", "feature")

#: The fail-open shape: a guard that treats bd query's exit code as "found".
FAIL_OPEN_SHAPES = (
    "bd query",
    "| grep -q",
)


def _hook_source() -> str:
    assert HOOK.exists(), f"gate hook missing at {HOOK}"
    return HOOK.read_text()


def test_gate_hook_exists():
    """The outer-loop gate must be present; a missing hook is a silent no-gate."""
    assert HOOK.exists()
    assert HOOK.stat().st_mode & 0o111, "hook must be executable"


def test_gate_lints_every_gated_work_type():
    """Each gated type must be linted -- an unlisted type is an ungated path.

    Adding a type to GATED_TYPES and forgetting the hook fails this test.
    """
    src = _hook_source()
    loop = next(
        (ln for ln in src.splitlines() if ln.strip().startswith("for _t in")), None
    )
    assert loop is not None, "gate must iterate the gated work types"
    for t in GATED_TYPES:
        assert t in loop, f"gate does not lint work type {t!r}"
    assert '--type "$_t"' in src
    assert "bd lint" in src


def test_gate_does_not_use_fail_open_query_exit_code():
    """The gate must not treat `bd query`'s exit code as a found/not-found signal.

    Regression guard: `bd query` exits 0 when nothing matches, so a gate on its
    exit code is green while guaranteeing nothing.
    """
    src = _hook_source()
    assert "bd query" not in src, (
        "gate must not use `bd query` -- its exit code is fail-open "
        "(rc=0 with no matches). Use `bd lint`, which exits non-zero on a "
        "missing section."
    )


def test_gate_does_not_grep_pipe_a_query():
    """`| grep -q` over a query is the same fail-open shape in another syntax."""
    assert "grep -q" not in _hook_source()


@pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")
def test_bd_query_is_fail_open_when_nothing_matches():
    """Pin the trap itself, so a future bd change that fixes it is a visible event.

    If this ever starts failing, the gate's design rationale changes -- update
    the hook's comment and this test together.
    """
    r = subprocess.run(
        ["bd", "query", "spec=nonexistent-xyz-never-set"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # The trap: rc 0, and the output explicitly says nothing matched.
    assert r.returncode == 0, "bd query changed: rc is no longer fail-open"
    assert "No issues found" in r.stdout


@pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")
def test_bd_lint_exits_nonzero_on_missing_section():
    """`bd lint` is a real signal: it must exit non-zero on a missing section.

    Creates a throwaway task with no acceptance criteria, asserts the non-zero
    exit, then removes it. This is the property the gate depends on.
    """
    title = "SCRATCH gate-signal probe (auto-removed)"
    create = subprocess.run(
        ["bd", "create", title, "-t", "task"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert create.returncode == 0, create.stderr
    assert "school-core-" in create.stdout
    issue_id = next(
        tok for tok in create.stdout.split() if tok.startswith("school-core-")
    )
    try:
        r = subprocess.run(
            ["bd", "lint", "--type", "task"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert r.returncode != 0, (
            "bd lint no longer fails on a missing Acceptance Criteria -- the "
            "gate's signal is gone"
        )
        assert "Acceptance Criteria" in r.stdout
    finally:
        subprocess.run(
            ["bd", "close", issue_id, "--reason", "auto-removed gate probe"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
