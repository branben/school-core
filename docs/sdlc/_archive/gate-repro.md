# GATE 3: REPRO — Systematic Debugging with Serena

> **SDLC Gate:** Reproduce before you fix.
> **Inputs:** Bug report, failing test, or anomaly signal.
> **Outputs:** Confirmed reproduction, traced call chain, failing test, documented repro steps.
> **Next Gate:** GATE 4 (TDD) — write the fix cycle.

---

## 1. Purpose

GATE 3 exists to prevent the most expensive habit in software: **fixing symptoms instead of root causes.**

Every bug fix in school-core must pass through REPRO. No exceptions. The gate ensures:

1. The bug is **reproducible** — not a one-off flake.
2. The **call chain is traced** — you know which function feeds the bad value.
3. A **failing test exists** — proof the bug is real and the fix will be verifiable.
4. Reproduction steps are **documented** — in a plannotator guide so reviewers can follow.

This gate is where `systematic-debugging`, Serena MCP, and TDD converge.

---

## 2. The Iron Law

```
NO FIX WITHOUT A CONFIRMED REPRODUCTION AND A FAILING TEST
```

Violating this law is the #1 source of regression debt in school-core. The crew dispatch pipeline, verify gate, and issue_bridge all have long histories of "quick fixes" that masked real problems. GATE 3 is the antidote.

---

## 3. Phase 1 — Reproduce the Bug

### 3.1 Read the Error Carefully

Before running anything, read the full error message, stack trace, and surrounding context. In school-core:

```bash
# Vitest: read the full diff output
pnpm test -- --reporter=verbose 2>&1 | head -100

# Python: get the full traceback
pytest tests/test_module.py::test_name -v --tb=long 2>&1
```

**Common school-core signals:**

| Signal | Meaning | First action |
|--------|---------|--------------|
| `No X export is defined on the mock` | Incomplete vitest mock | Check component imports vs mock return shape |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Python SSL context | Pass `ssl.create_default_context(cafile=certifi.where())` |
| `TypeError: expand is not a function` | Dependency version conflict | Check lockfile overrides |
| Test passes alone, fails in suite | State pollution / test isolation | Check for module-level mutation |
| `Signal generation failed` (silent) | Leftover debug instrumentation | Search for `time.time()` without `import time` |

### 3.2 Confirm Reproduction

Run the failing scenario at least **three times** to rule out flakiness:

```bash
for i in 1 2 3; do
  pytest tests/test_name.py -v --tb=short 2>&1 | tail -5
done
```

If it fails inconsistently, you have a test isolation problem — that IS the bug. Document it.

### 3.3 Check Recent Changes

```bash
git log --oneline -15
git diff HEAD~5..HEAD --stat
git diff HEAD~5..HEAD -- src/problematic_file.py
```

---

## 4. Phase 2 — Trace with Serena MCP

Serena provides **semantic code intelligence** for school-core. Use it to trace the call chain without reading every file manually.

### 4.1 Find the Symbol

When you have a function, class, or variable name from the error:

```
mcp__serena__find_symbol(
  name_path="dispatch_crew",
  relative_path="crew_dispatch.py"
)
```

Returns the declaration with file, line number, and body.

### 4.2 Find All References

Trace who calls the problematic function:

```
mcp__serena__find_referencing_symbols(
  name_path="dispatch_crew",
  relative_path="crew_dispatch.py"
)
```

Returns every call site — critical for understanding data flow into the failing function.

### 4.3 Search for Patterns

When you don't know the exact name, search by regex:

```
mcp__serena__search_for_pattern(
  pattern=r"dispatch_crew\(",
  relative_path="src/"
)
```

### 4.4 Get Symbols Overview

For unfamiliar files, get the high-level symbol map first:

```
mcp__serena__get_symbols_overview(
  relative_path="crew_dispatch.py"
)
```

This tells you what classes, methods, and functions exist — orient before you dig.

### 4.5 Read File Chunks

Read specific sections of large files (school-core files are often 500+ LOC):

```
mcp__serena__read_file(
  relative_path="crew_dispatch.py",
  start_line=200,
  end_line=280
)
```

### 4.6 Trace Workflow Example

**Scenario:** `test_crew_dispatch` fails with `artifact_identity mismatch`.

1. `find_symbol("test_artifact_identity", "tests/test_crew_dispatch.py")` → get test body
2. `find_referencing_symbols("artifact_identity", "crew_dispatch.py")` → find all assignments
3. `search_for_pattern(r"artifact_identity\s*=")` → catch dynamic assignments
4. `get_symbols_overview("crew_dispatch.py")` → understand the module structure
5. `read_file("crew_dispatch.py", 450, 530)` → read the failing section

**Result:** The bad value originates at line 487, propagated through `_build_artifact()` → `dispatch_crew()` → test assertion.

---

## 5. Phase 3 — Write the Failing Test (Matt Pocock TDD)

Once you understand the bug, **lock it in with a test** before touching any production code.

### 5.1 RED — Write One Minimal Test

```python
def test_dispatch_crew_preserves_artifact_identity():
    """Bug: artifact_identity is lost when crew_dispatch rebuilds the envelope."""
    input_identity = ArtifactIdentity(sha="abc123", author="student-agent")
    result = dispatch_crew(
        issue_id="test-001",
        identity=input_identity,
        dry_run=True,
    )
    assert result.identity == input_identity, (
        f"Expected {input_identity}, got {result.identity}"
    )
```

**Rules:**
- One behavior per test (no "and" in the name).
- Real code, not mocks (unless the bug IS the mock).
- Name describes behavior, not implementation.

### 5.2 Verify RED — Watch It Fail

```bash
pytest tests/test_crew_dispatch.py::test_dispatch_crew_preserves_artifact_identity -v
```

**Mandatory.** If the test passes immediately, you wrote a test for existing behavior. Fix the test.

If the test errors (typo, import failure), fix the error, re-run until it fails for the **expected reason**.

### 5.3 Document the Expected Failure

Record the exact failure message. This is evidence for the plannotator guide:

```
Expected: ArtifactIdentity(sha='abc123', author='student-agent')
Actual:   ArtifactIdentity(sha='unknown', author='unknown')
```

---

## 6. Phase 4 — Document in Plannonator Guide

Every bug fix in school-core gets a guided review. The REPRO section is the foundation.

### 6.1 Save the Repro Diff

```bash
git diff HEAD > repro.patch          # uncommitted repro work
git diff origin/main...HEAD > repro.patch  # branch-based
```

### 6.2 Write the Guide JSON

```json
{
  "title": "Fix: artifact_identity lost in crew_dispatch envelope rebuild",
  "intent": "test_crew_dispatch fails because dispatch_crew rebuilds the artifact envelope without forwarding the original identity. This guide walks the repro, the Serena trace, and the fix.",
  "sections": [
    {
      "title": "The failing test",
      "overview": "test_dispatch_crew_preserves_artifact_identity reproduces the bug. The test constructs an ArtifactIdentity with a known sha and author, calls dispatch_crew in dry_run mode, and asserts the identity survives. It fails because dispatch_crew calls _build_envelope() which constructs a fresh ArtifactIdentity from defaults, discarding the input.",
      "diffs": [
        {
          "file": "tests/test_crew_dispatch.py",
          "summary": "New failing test that pins down the identity loss."
        }
      ]
    },
    {
      "title": "The root cause in dispatch_crew",
      "overview": "_build_envelope() at line 487 creates ArtifactIdentity() with no arguments. It should forward the identity passed into dispatch_crew. This is the line worth slowing down for — everything else follows from it.",
      "diffs": [
        {
          "file": "crew_dispatch.py",
          "summary": "Forward the input identity into _build_envelope instead of constructing a fresh one."
        }
      ]
    }
  ],
  "unplacedFiles": [],
  "review": { "gitRef": "origin/main...HEAD", "base": "origin/main" },
  "generator": { "engine": "hermes-agent", "model": "meituan/longcat-2.0:free" }
}
```

### 6.3 Export and Share

```bash
plannotator guide export --guide repro-guide.json --patch repro.patch
# → outputs: repro-guide.html

plannotator guide share --guide repro-guide.json --patch repro.patch
# → outputs: https://guides.show/xxx#key
```

---

## 7. Serena Tool Reference for REPRO

| Tool | When to Use | Example |
|------|-------------|---------|
| `find_symbol` | You have a name, need the definition | Find where `dispatch_crew` is declared |
| `find_referencing_symbols` | You need call sites | Who calls `dispatch_crew` with bad args? |
| `search_for_pattern` | You don't know the exact name | Find all `artifact_identity =` assignments |
| `get_symbols_overview` | Unfamiliar file | Map the structure of `crew_dispatch.py` |
| `read_file` | Read specific lines | Read lines 450-530 of a 1000-line file |
| `find_file` | Find files by glob | `find_file("test_*.py", "tests/")` |
| `find_implementations` | Find interface implementations | Find all `Executor` subclasses |
| `find_declaration` | Jump to a symbol's declaration | Where is `CrewConfig` defined? |

---

## 8. Integration with Other Gates

```
GATE 1 (PLAN) → GATE 2 (CONTEXT) → GATE 3 (REPRO) → GATE 4 (TDD) → GATE 5 (REVIEW)
                                              ↑
                                         You are here
```

**REPRO feeds TDD:** The failing test you write in Phase 3 becomes the RED step of the TDD cycle. When you move to GATE 4, you already have:

- A confirmed reproduction.
- A traced call chain (Serena).
- A failing test (RED).
- A documented guide (plannotator).

**REPRO blocks on failure:** If you cannot reproduce the bug, you cannot proceed. Options:
1. Gather more evidence (add logging, run in isolation).
2. Close the bead as "cannot reproduce" with evidence.
3. Reopen when new information arrives.

---

## 9. School-Core Specific Patterns

### 9.1 Test Isolation Failures

school-core has 1200+ tests. State pollution is the #1 repro challenge.

```bash
# Run the failing test in isolation
pytest tests/test_name.py -v

# Run with the full suite
pytest tests/ -q

# If it passes alone but fails in suite: ISOLATION BUG
# Use --forked (pytest-forked) to confirm
pip install pytest-forked
pytest tests/test_name.py --forked
```

### 9.2 Async/Concurrency Bugs

school-core uses asyncio heavily in crew_dispatch and issue_bridge.

```python
# Use pytest-asyncio with explicit mode
@pytest.mark.asyncio(mode="auto")
async def test_concurrent_dispatch_race():
    ...
```

### 9.3 Mock Signature Drift

When school-core updates a function signature, mocks silently return `undefined`:

```python
# Check mock completeness
vi.mock("../src/module", () => ({
  functionName: vi.fn(),  # returns undefined!
  // Missing: otherExport: vi.fn(),
}))
```

### 9.4 Worktree Contamination

school-core uses worktrees for crew dispatch. A stray `.venv` in a worktree breaks imports:

```bash
# Check for contamination
find .worktrees -name ".venv" -type d
```

---

## 10. Checklist

Before marking GATE 3 complete:

- [ ] Error message fully read and understood
- [ ] Bug reproduced consistently (3+ runs)
- [ ] Recent changes reviewed (git log, git diff)
- [ ] Call chain traced with Serena (find_symbol, find_referencing_symbols)
- [ ] Root cause identified (not just symptom)
- [ ] Failing test written and verified RED
- [ ] Reproduction steps documented in plannotator guide
- [ ] Guide exported and shared
- [ ] Bead status updated

---

## 11. References

- `systematic-debugging` skill — 4-phase root cause methodology
- `test-driven-development` skill — RED-GREEN-REFACTOR cycle
- `plannotator-guide` skill — guided review authoring
- `matt-pocock-methodology` skill — compound engineering patterns
- `serena` MCP tools — semantic code intelligence
- `school-core/docs/sdlc/gate-tdd.md` — next gate (TDD fix cycle)
- `school-core/docs/sdlc/gate-context.md` — previous gate (context engineering)
