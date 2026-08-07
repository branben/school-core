# ADR 0006: Serena LSP Integration for Layer 1 Structural Context

**Status:** Accepted  
**Date:** 2026-07-29

## Context

The framework's four-layer memory architecture (`campus.md`) defines
Layer 1 as "Structural context" — file trees, symbol indices, and import
graphs. Before this ADR, Layer 1 was served by two mechanisms with
opposite weaknesses:

| Source | Strength | Weakness |
|--------|----------|----------|
| **CocoIndex** (`_cocoindex_context`) | Semantic similarity ("find the auth flow") | Approximate — no exact symbol resolution |
| **`repo_reader.py`** (`find_relevant_files`) | Keyword grep on filenames | String matching, no type info, no references |

Neither provides exact, LSP-backed symbol lookup. When a student needs
"the definition of `createRoom`", the framework could only return
semantic guesses or filename matches — not the precise `file:line`.
This is a critical gap for code-heavy domains (`python-coding`,
`code-implementation`, `debugging`).

[Serena](https://github.com/oraios/serena) is an LSP-backed MCP server
providing exact symbol resolution across 30+ languages. It fills the gap
between CocoIndex (semantic/approximate) and nothing (exact/precise).

## Decision

We integrate Serena as a **pre-execution context enrichment layer** in
`context_orchestrator.py`, following the same non-blocking "shell out"
pattern used by CocoIndex and Engram.

### Components

| File | Role |
|------|------|
| `serena_adapter.py` (new) | Thin MCP stdio client — one-shot subprocess, full MCP handshake, returns parsed results |
| `context_orchestrator.py` (modified) | `_serena_context()` + `_extract_symbol_names()` + `SERENA_CHAR_BUDGET` |
| `director.py` (modified) | `_resolve_repo_path()` — maps repo slug → cached clone path for LSP root |
| `mcp_server.py` (modified) | `repo` param on `school_execute` tool schema |
| `tests/test_context_orchestrator.py` (modified) | 12 unit tests for `_extract_symbol_names` |

### Data Flow

```
director.run_task(repo="branben/school-core")
  → _resolve_repo_path(repo)
      → checks ~/.cache/<slug>/repos/branben__school-core/
      → falls back to current checkout root
  → enrich_prompt(domain, prompt, repo_path=repo_path)
      → _serena_context(prompt, repo_path)    ← NEW
          1. _extract_symbol_names(prompt)     → ["run_task", "ScoreStore"]
          2. find_symbol("run_task")           → director.py:467
          3. format as "Exact symbol locations" → system prompt
      → _cocoindex_context(prompt, vault)
      → _engram_context(domain, prompt)
      → _archival_context(domain, session_id)
```

### Design Properties

- **One-shot subprocess**: Serena is started, queried, and shut down per
  `enrich_prompt()` call. Slow (~5-10s per call) but prevents long-lived
  process management. Follows the same pattern as `_cocoindex_context`
  (`ccc search`) and `engram_adapter` (`engram save/search`).
- **Non-blocking**: `_serena_context()` returns `None` if Serena is
  unavailable, the repo clone doesn't exist, or any error occurs. The
  pipeline continues without it.
- **Domain-gated**: Only fires for code-heavy domains:
  `code-implementation`, `python-coding`, `python-testing`,
  `code-review`, `debugging`, `_default`.
- **Char budget**: `SERENA_CHAR_BUDGET = 600` caps symbol results to
  avoid context bloat (Layer 0 + Layer 1 share ~2K chars total).
- **Symbol extraction**: `_extract_symbol_names()` captures
  UpperCamelCase, lowerCamelCase, snake_case, ALL_CAPS, and
  backtick-quoted identifiers. Deduplicates case-insensitively, caps at
  5 symbols, filters tokens < 3 chars.
- **Repo path resolution**: `_resolve_repo_path()` checks the
  `repo_reader` cache first (no cloning), falls back to the current
  checkout root. Returns `None` for `REPO_GLOBAL` or unresolvable slugs.

## Consequences

- **Positive:** Agents in code-heavy domains now receive exact symbol
  locations in their system prompt before execution. This closes the
  Layer 1 precision gap.
- **Positive:** The `_extract_symbol_names()` function is independently
  testable (12 unit tests covering all identifier patterns).
- **Positive:** `repo_path` is optional and backward-compatible — no
  existing callers break.
- **Risk — subprocess overhead**: Starting Serena as a subprocess per
  `enrich_prompt()` call adds ~5-10s latency. Acceptable for now;
  long-lived process or batch queries are future optimizations.
- **Risk — Serena availability**: If Serena is not installed, the
  integration degrades silently. No runtime error, no context injection.
- **Risk — field name coupling**: Serena's MCP tool schema uses
  `name_path_pattern`, `name_path`, `relative_path`, and
  `body_location.start_line`. These are normalized in
  `_serena_context()`. If Serena's schema changes, the normalization
  layer may need updates.

## Rejected Alternatives

| Alternative | Reason |
|-------------|--------|
| **Integrate as MCP tool in `mcp_server.py`** | Agents must know to query Serena interactively; misses pre-task enrichment. `context_orchestrator` injects symbols into the system prompt before the agent starts. |
| **Replace CocoIndex with Serena** | CocoIndex is semantic/approximate — good for "find the auth flow". Serena is exact/precise — good for "find `createRoom`". They complement, not replace. |
| **Long-lived Serena MCP process** | Adds process lifecycle management complexity. One-shot subprocess is simpler for v1 and follows existing patterns. |
| **Python MCP client library** | Adds a dependency. The stdio-based approach in `serena_adapter.py` is zero-dependency and follows the codebase's "shell out" convention. |
| **Serena for all domains** | Symbol resolution is wasted on non-code domains (git-operations, code-review of prose). Domain-gating keeps context tight. |

## Bugs Caught During Build

1. **`find_symbol` parameter name**: Serena's MCP tool schema requires
   `name_path_pattern`, not `name`. Discovered via e2e test — the tool
   returned a Pydantic validation error. Fixed in `serena_adapter.py`.

2. **`find_referencing_symbols` same parameter name bug**: Same
   `name` → `name_path_pattern` fix applied (caught by code review).

3. **Field name mismatch**: Serena returns `name_path`, `relative_path`,
   and `body_location.start_line` — not `name`, `file`, `line`. Added
   normalization layer in `_serena_context()` display loop.

4. **Error result leakage**: When Serena returned tool errors (e.g.,
   validation failures), the `raw` key leaked into the context as `?`
   display. Added `"raw" not in item` filter in `_serena_context()`.

5. **Missing lowerCamelCase**: The original `_extract_symbol_names`
   regex only captured UpperCamelCase. LowerCamelCase (`createRoom`,
   `handleAuth`) was missed. Added `[a-z]+(?:[A-Z][a-z]+)+` pattern.

6. **`BOOKBAG_DIR` isolation via `setenv` too late**: Module-level
   constant evaluated at import time — `monkeypatch.setenv` had no
   effect. Fixed by using `monkeypatch.setattr` directly (separate PR,
   but discovered during the same e2e testing session).
