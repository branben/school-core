---
id: eval-post-consolidation
created: 2026-06-13T00:00:00
created_by: orchestrator
type: eval
project: agent-school
status: complete
tags: [eval, foundry, consolidation, baseline]
agents_involved: [orchestrator]
---

# Post-Consolidation Eval — 2026-06-13

## What Was Tested

All 5 Foundry Local models via `force_agent` routing:
- `foundry-coder-0.5b` (528MB)
- `foundry-coder-1.5b` (1.3GB)
- `foundry-coder-7b` (4.7GB) — production workhorse
- `foundry-smollm3-3b` (2.2GB)
- `foundry-phi4` (8.4GB)

Task: "Write a Python function that checks if a string is a string is a palindrome. Include a docstring and one test case."
Domain: `python-coding`

## Results

| Model | Status | Time | Response | Quality | Δ default | Δ python-coding |
|-------|--------|------|----------|---------|-----------|-----------------|
| foundry-coder-0.5b | success* | 11.2s | 875B | func✓ palindrome✓ doc✓ | +0.0 | -5.2 |
| foundry-coder-1.5b | success* | 8.8s | 766B | func✓ palindrome✓ doc✓ | +0.0 | -4.2 |
| foundry-coder-7b | success* | 7.7s | 819B | func✓ palindrome✓ doc✓ | +0.0 | -8.7 |
| foundry-smollm3-3b | success* | 7.8s | 868B | func✓ palindrome✓ doc✓ | +0.0 | -2.1 |
| foundry-phi4 | success* | 8.5s | 835B | func✓ palindrome✓ doc✓ | +0.0 | -2.1 |

\* All results were served by A2A fallback (OpenHands), not Foundry. See Critical Finding below.

## Critical Finding: Foundry REST API Not Functional

**Root cause**: Foundry Local's OpenAI-compatible REST API does not bind to a port, even though `foundry server status` reports "Ready". The CLI (`foundry model list`, `foundry model load`) works via a different IPC mechanism, but HTTP calls to the REST endpoint get `Connection refused`.

**Impact**: ALL previous "successful" Foundry evaluations in the trajectory data were actually A2A fallbacks to OpenHands. The Foundry models were never actually called through the REST API.

**Evidence**:
- `foundry server status` reports port (e.g., 60034) but `lsof -i :60034` shows nothing
- `curl http://127.0.0.1:60034/v1/models` → Connection refused
- `foundry model list` works (uses CLI/IPC, not HTTP)
- All trajectories show `agent: openhands` even when `force_agent: foundry-coder-7b` was specified

**Fixes Applied**:
1. **Dynamic port discovery**: `FOUNDRY_BASE` hardcoded as `http://localhost:11500/v1` was wrong — Foundry uses dynamic ports. Added `_get_foundry_base()` that parses `foundry server status` output.
2. **Auto-load regex fix**: Changed from broken regex (`"model" is not loaded` / `model \\u0027`) to simple `'not loaded' in detail.lower()`.
3. **Backend consolidation**: Removed Ollama as local backend. All local models now route through Foundry.

**Next Steps**:
- File Foundry Local bug report: REST API not binding to port
- As workaround, investigate if Foundry has a Unix socket or named pipe interface
- Alternative: use Foundry's Python SDK directly instead of REST API
- Until fixed, all local model calls fall through to A2A (OpenHands)

## Score Impact

Scores for Foundry models are **decreasing** on each eval run because:
1. `force_agent` → Foundry call fails → `_try_agent` error path → score penalized (task_score=0)
2. A2A fallback succeeds → `evaluate_and_update` runs on OpenHands, not the Foundry agent
3. Net result: Foundry agent gets punished, OpenHands gets rewarded

This is a pre-existing design issue with `force_agent` + A2A fallback, masked by the Foundry REST API being broken.

## Trajectories

51 total trajectories in `data/trajectories/`. 10 created during this eval (5 per run × 2 runs).

---
*Last updated: 2026-06-13*
