---
id: foundry-subprocess-transport
created: 2026-06-13T00:00:00
created_by: orchestrator
type: architecture
project: agent-school
status: active
tags: [foundry, subprocess, transport, workaround]
---

# Foundry Local Subprocess Transport

## Problem

Foundry Local's OpenAI-compatible REST API does not reliably bind to a port. `foundry server status` reports "Ready" on a dynamic port, but HTTP connections get `Connection refused`. The CLI (`foundry model list`, `foundry model load`) works via a different IPC mechanism.

## Solution

Use `foundry complete <model> <prompt>` as a subprocess instead of HTTP calls.

## Implementation

`_foundry_call()` in `executor.py` now:
1. Builds a single prompt from OpenAI message format
2. Calls `foundry complete <model> <prompt> --max-tokens 4096 --temperature 0.3`
3. Auto-loads models on "not loaded" error (retries once)
4. Strips terminal formatting (box-drawing, ANSI codes) from output
5. Returns dict in OpenAI response format

## COMBO_MAP Model Names

Foundry models use short aliases (not full variant IDs):

| Agent | Model Alias |
|-------|------------|
| foundry-coder-0.5b | qwen2.5-coder-0.5b |
| foundry-coder-1.5b | qwen2.5-coder-1.5b |
| foundry-coder-7b | qwen2.5-coder-7b |
| foundry-smollm3-3b | smollm3-3b |
| foundry-phi4 | phi-4 |

## Performance

| Model | Size | GPU Cached | Cold | Warm |
|-------|------|-----------|------|------|
| coder-0.5b | 528MB | ✅ | ~8s | ~3s |
| coder-1.5b | 1.3GB | ✅ | ~10s | ~5s |
| coder-7b | 4.7GB | ✅ | ~25s | ~15s |
| smollm3-3b | 2.2GB | ❌ (CPU) | >300s (timeout) | N/A |
| phi-4 | 8.4GB | ❌ | >300s (timeout) | N/A |

## Limitations

1. **Cold start penalty**: Each `foundry complete` call starts a new process. Models not in GPU cache take 30s+ to load.
2. **Large models timeout**: smollm3-3b (CPU) and phi-4 (8.4GB) exceed the 300s timeout even when pre-loaded. Phi-4 inference is very slow at 8.4GB on M1.
3. **No streaming**: `foundry complete` returns the full response at once — no token streaming.
4. **Terminal formatting**: Box-drawing characters leak through the regex strip in some cases.

## Recommendations

- **Primary models**: Use coder-0.5b, coder-1.5b, coder-7b for daily work (all GPU-cached, fast)
- **Avoid on M1**: phi-4 (too large, too slow). Consider phi-4-mini (3.7GB) instead.
- **Future**: When Foundry fixes the REST API binding, switch back to HTTP for keep-alive warmth and streaming.

---
*Last updated: 2026-06-13*
