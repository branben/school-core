# School-Core Comprehensive Audit & Task Plan

> Generated: 2026-07-22  
> Method: Full codebase audit (25+ modules), dependency graph analysis, blast-radius mapping

---

## Architecture Snapshot (Current State)

```
conductor.py ───── Principal CLI
  └── director.py ─── routing + system prompts + review orchestration
        ├── executor.py ─── OmniRoute/A2A transport layer
        ├── scoring.py ─── EMA score store + gate thresholds
        ├── bookbag.py ─── student output persistence
        ├── adversarial_reviewer.py ─── CTO+COO engine
        │     └── lenses/*.py ─── correctness, security, completeness prompts
        ├── context_orchestrator.py ─── vault/engram/skills/bookbag injection
        │     ├── engram_adapter.py ─── persistent memory
        │     └── 03-Skills/ (KnowledgeCore) ─── CE workflows
        ├── anchor_loader.py ─── semantic anchors from config/anchors.yaml
        ├── trajectory.py ─── persistent trajectory logging
        └── sleep_state.py ─── session sleep/wake
```

---

## Dependency Map

### Layer 1: Foundation (zero internal dependencies)
| Module | Dependencies | Notes |
|--------|-------------|-------|
| `bookbag.py` | stdlib only | Stable, proven |
| `engram_adapter.py` | `engram` CLI binary | Thin wrapper |
| `decision_log.py` | stdlib only | Stable |
| `activity_log.py` | stdlib only | Stable |
| `escalation_log.py` | stdlib only | Stable |
| `lenses/*.py` | `LensType` enum | Pure string constants |
| `anchor_loader.py` | `yaml` lib + dataclasses | Stable |
| `verify_gate.py` | stdlib only | Exists but UNWIRED |
| `triage_classifier.py` | stdlib only | Wired but unused in main pipeline |

### Layer 2: Utilities (depend on Layer 1)
| Module | Dependencies |
|--------|-------------|
| `scoring.py` | `execution_scorer`, `heuristic_scorer` |
| `trajectory.py` | `engram_adapter` |
| `context_orchestrator.py` | `engram_adapter`, `bookbag` |
| `consolidation_writer.py` | `engram_adapter` |
| `sleep_state.py` | `engram_adapter` |

### Layer 3: Core Logic (depend on Layer 2)
| Module | Dependencies |
|--------|-------------|
| `executor.py` | OmniRoute HTTP, A2A HTTP (stdlib + urllib) |
| `adversarial_reviewer.py` | `lenses/*.py` |
| `director.py` | `executor`, `scoring`, `context_orchestrator`, `anchor_loader`, `adversarial_reviewer`, `trajectory`, `bookbag`, `decision_log`, `triage_classifier` |
| `routing.py` | `scoring` |

### Layer 4: Entry Points (depend on Layer 3)
| Module | Dependencies |
|--------|-------------|
| `conductor.py` | `director`, `scoring`, `bookbag` |
| `mcp_server.py` | `director`, `routing`, `scoring`, `executor`, `context_orchestrator`, `trajectory` |
| `autonomous_loop.py` | `director`, `scoring` |
| `cli.py` | `scoring` |
| `issue_bridge.py` | `scoring`, `triage_classifier` |
| `staff/plugins/*.py` | `scoring` |

---

## Identified Tasks (Ordered by Workstream)

### Workstream A: MCP Server Modernization
**Blast radius:** `mcp_server.py` only (no other files change)  
**Risk:** Low — self-contained file, no callers depend on internals  
**Effort:** Small (~50 lines changed)

| # | Task | Files | Depends On |
|---|------|-------|------------|
| A1 | Switch MCP server from `SYSTEM_PROMPTS` to `ROLE_SYSTEM_PROMPTS` | `mcp_server.py` | Nothing |
| A2 | Add `role` parameter to `enrich_prompt` call in `school_execute` | `mcp_server.py` | A1 |
| A3 | Inject semantic anchors via `_anchor_context()` in MCP handlers | `mcp_server.py` | A1 |
| A4 | Remove dead "Foundry" backend reference from `_backend_for_agent` | `mcp_server.py` | Nothing |

### Workstream B: Verify Gate → Review Pipeline
**Blast radius:** ~2 files (`director.py` + potentially `verify_gate.py`)  
**Risk:** Low — additive change, existing review flow unchanged  
**Effort:** Small (~30 lines added)

| # | Task | Files | Depends On |
|---|------|-------|------------|
| B1 | Call `verify_gate.run_verify_gate()` inside `_run_two_judge_review()` when domain has executable output | `director.py`, `verify_gate.py` | Nothing |
| B2 | Feed execution failures as CRITICAL findings into CTO review | `director.py` | B1 |

### Workstream C: Dead Code Cleanup
**Blast radius:** Low — nothing depends on these  
**Effort:** Small

| # | Task | Files | Depends On |
|---|------|-------|------------|
| C1 | Fix `test_anchor_loader.py` — remove `from prompt_composer import compose_prompt` | `tests/test_anchor_loader.py` | Nothing |
| C2 | Fix `test_role_loader.py` — remove `from prompt_composer import ROLE_ANCHORS` | `tests/test_role_loader.py` | Nothing |
| C3 | Delete `prompt_composer.py` | `prompt_composer.py` | C1, C2 |
| C4 | Delete dead `prompt_composer.py` reference in `anchor_loader.py` docstring | `anchor_loader.py` | Nothing |

### Workstream D: Browser Role Anchors
**Blast radius:** `config/anchors.yaml` + `director.py`  
**Risk:** Low — additive only  
**Effort:** Small (~5 anchors)

| # | Task | Files | Depends On |
|---|------|-------|------------|
| D1 | Add web-automation domain anchors to `config/anchors.yaml` (e.g., `Page Object Model`, `Selector Strategy`, `Wait Strategy`) | `config/anchors.yaml` | Nothing |
| D2 | Add `web-automation` domain to browser's `ROLE_ANCHOR_DOMAINS` | `director.py` | D1 |

### Workstream E: Auth Token Security
**Blast radius:** `executor.py`  
**Risk:** Low — env var fallback only  
**Effort:** Tiny (~3 lines)

| # | Task | Files | Depends On |
|---|------|-------|------------|
| E1 | Read API_KEY from env var with fallback to hardcoded value | `executor.py` | Nothing |

### Workstream F: Documentation Sync
**Blast radius:** `campus.md`  
**Risk:** Trivial  
**Effort:** Small

| # | Task | Files | Depends On |
|---|------|-------|------------|
| F1 | Update `campus.md` to reflect current Principal→CTO+COO architecture (the aspirational table was last updated — verify it matches) | `campus.md` | Nothing |
| F2 | Document the 5 specialized roles, bookbag schema, and Orca dispatch flow | `campus.md` | F1 |

---

## Parallelization Matrix

```
              A1  A2  A3  A4  B1  B2  C1  C2  C3  C4  D1  D2  E1  F1  F2
A1 (mcp role)  ─   ▶   ▶   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆
A2 (mcp role)  ─   ─   ▶   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆
A3 (mcp role)  ─   ─   ─   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆
A4 (foundry)   ─   ─   ─   ─   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆
B1 (verify)    ◆   ◆   ◆   ◆   ─   ▶   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆
B2 (verify)    ◆   ◆   ◆   ◆   ─   ─   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆
C1 (test fix)  ◆   ◆   ◆   ◆   ◆   ◆   ─   ◆   ▶   ◆   ◆   ◆   ◆   ◆   ◆
C2 (test fix)  ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ▶   ◆   ◆   ◆   ◆   ◆   ◆
C3 (delete)    ◆   ◆   ◆   ◆   ◆   ◆   ▶   ▶   ─   ◆   ◆   ◆   ◆   ◆   ◆
C4 (doc fix)   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ◆   ◆   ◆   ◆   ◆
D1 (anchors)   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ▶   ◆   ◆   ◆
D2 (anchors)   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ─   ◆   ◆   ◆
E1 (auth)      ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ◆   ◆
F1 (docs)      ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ▶
F2 (docs)      ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ─   ─

▶ = must be sequential (depends on)
◆ = safe to parallelize (no shared code)
```

### Optimal Execution Order

**Phase 1 — Parallel (safe to run all at once):**
| Batch | Tasks | Files changed |
|-------|-------|--------------|
| Batch 1 | A1, A4 (MCP role switch + Foundry removal) | `mcp_server.py` |
| Batch 2 | B1 (Verify gate wiring) | `director.py` |
| Batch 3 | C1, C2, C4 (Test fixes + docstring fix) | `tests/test_anchor_loader.py`, `tests/test_role_loader.py`, `anchor_loader.py` |
| Batch 4 | D1 (Anchors YAML) | `config/anchors.yaml` |
| Batch 5 | E1 (Auth token env var) | `executor.py` |
| Batch 6 | F1 (Campus docs) | `campus.md` |

**Phase 2 — Sequential (depends on Phase 1):**
| Batch | Tasks | Depends on |
|-------|-------|------------|
| Batch 7 | A2, A3 (MCP role param + anchors) | Batch 1 (A1) |
| Batch 8 | B2 (Execution failures as findings) | Batch 2 (B1) |
| Batch 9 | C3 (Delete prompt_composer.py) | Batch 3 (C1, C2) |
| Batch 10 | D2 (Browser domain mapping) | Batch 4 (D1) |
| Batch 11 | F2 (Detailed docs) | Batch 6 (F1) |

---

## What NOT To Do (Out of Scope)

| Thing | Why |
|-------|-----|
| Wire Serena/CodeGraph MCP into executor.py | Massive architectural change — needs MCP client integration, not a quick wiring |
| Rebuild student execution pipeline | The current text-completion-only model is a design choice; changing it is a v2 problem |
| Add web-automation anchors for browser | D1 covers this, but the actual *anchors content* needs you to decide which methodologies fit |
| Add new roles or domains | Would require new seeds, new prompts, new COMBO_MAP entries — beyond audit scope |
| Refactor scoring.py EMA weights | Would change existing agent behavior — needs validation against real data |

---

## Recommendation: Optimal Order

### If you want maximum parallel speed (6 agents in parallel):
```
Wave 1 (parallel): A1+A4  │  B1  │  C1+C2+C4  │  D1  │  E1  │  F1
Wave 2 (parallel): A2+A3  │  B2  │  C3  │  D2  │  F2
```

### If you want to do it yourself, start with:
```
1. E1 (auth token) — 3 lines, 30 seconds
2. C1, C2, C4 (dead imports) — 2 minutes
3. A1, A4 (MCP server) — 10 minutes, biggest impact
4. D1 (anchors) — 10 minutes
5. B1 (verify gate) — 15 minutes
6. Run all tests → C3 (delete prompt_composer) + D2 + F1+F2
```

---

## Blast Radius by File

| File | How many imports it | Who imports it | Can it change alone? |
|------|---------------------|----------------|----------------------|
| `mcp_server.py` | 0 (no one imports it) | — | ✅ Yes, fully isolated |
| `executor.py` | 6 files | director, mcp_server, routing | ⚠️ Needs test pass after |
| `director.py` | 4 files | conductor, mcp_server, autonomous_loop, issue_bridge | ⚠️ Needs test pass after |
| `prompt_composer.py` | 2 test files | test_anchor_loader, test_role_loader | ✅ Can delete cleanly |
| `config/anchors.yaml` | 1 file | anchor_loader (runtime) | ✅ Yes, additive only |
| `verify_gate.py` | 0 (unused) | — | ✅ Yes, fully isolated |
| `campus.md` | 0 | — | ✅ Yes, docs only |
| `anchor_loader.py` | 1 file | director | ⚠️ Needs test pass |
