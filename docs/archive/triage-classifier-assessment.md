# Triage Classification — Degraded Mode Assessment

**Date:** 2026-06-12

## What works locally (no model, zero tokens, instant):

| Task | Method | Accuracy | Verdict |
|------|--------|----------|---------|
| Category (bug/enhancement) | Rule-based classifier | 90% | Ready for production |
| State (with kilo/needs-info labels) | Rule-based classifier | ~80% | Ready for production |
| State (without clear labels) | Rule-based classifier | ~50% | Needs cloud fallback |

## What needs cloud:

| Task | Why |
|------|-----|
| State for ambiguous issues | Needs repo context + reasoning beyond label matching |
| Code implementation | Local models scored 2.1/5 — not reliable |

## Final degraded-mode architecture:

```
Normal mode:
  local-category (rule) + local-state (rule)
  → if state=ambiguous → cloud-state
  local-navigate → cloud-generate → local-verify

Degraded mode (cloud unavailable):
  local-category (rule, 90%) + local-state (rule, 60-80%)
  → route ready-for-human issues (kilo labels) locally
  → route needs-info issues locally
  → queue everything else for cloud when available
  → no code implementation in degraded mode
```

## Files:
- Classifier: school-core/triage_classifier.py
- Phase 1 model eval: eval-results/eval-f-foundry-scoring.md
- Phase 2 results + analysis: eval-results/eval-f-foundry-results.md
- Updated scores: school-core/data/scores.json
