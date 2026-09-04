---
title: "MCP 2026-07-28: StdioTransport socket crash fix — chain of custody"
project: "Swift SDK MCP 2026-07-28 migration"
type: "chain-of-custody"
status: "active"
tags: [swift-sdk, mcp-2026-07-28, stdiotransport, chain-of-custody]
ai-first: true
anchor: "plan-mcp-07-28-stdio-custody"
updated: 2026-11-20
---

#Chain of custody: STDIO Transport socket crash fix (#327)

**Bead:** `school-core-zjcu` (P2, open)
**PR:** `#6` on `branben/swift-sdk` (fork) → `modelcontextprotocol/swift-sdk`
**PRD:** 2026-11-01-001-mcp-07-28-spec-upgrade-prd.md
**SPEC:** 2026-11-01-002-mcp-07-28-spec-upgrade-spec.md

##Chain of custody (end-to-end)

| Step | Artifact | Evidence | Verified |
|------|----------|----------|----------|
| 1 — Issue | #327 open, socket crash | `gh issue view 327` | ✅ |
| 2 — PRD | 2026-11-01-001-mcp-07-28-spec-upgrade-prd.md | linked in PR body | ✅ |
| 3 — SPEC | 2026-11-01-002-mcp-07-28-spec-upgrade-spec.md | linked in PR body | ✅ |
| 4 — Implementation | `StdioTransport.swift` (3 sites) + `StdioTransportTests.swift` | fix commit `458aa4e` | ✅ |
| 5 — Review | school-core review_gate.py (local) | review passed | ✅ |
| 6 — PR | `#6` on fork → upstream | PR #6 open | ✅ |
| 7 — Verification | `swift test --filter StdioTransport` → 12/12; `swift test` → 650/650 | verified | ✅ |
| 8 — Merge | upstream maintainer | ⏳ pending | — |

##Artifacts (TOT-published)

| Artifact | Reference | TOT URL |
|----------|-----------|---------|
| Outer-loop ticket | bead `school-core-zjcu` (P2, open) | https://tot.page/a-r4PniSN8wZvQNs10ht6g |
| PRD | 2026-11-01-001-mcp-07-28-spec-upgrade-prd.md | https://tot.page/RSmvl3owH4wp8kqS6PrcBw |
| SPEC | 2026-11-01-002-mcp-07-28-spec-upgrade-spec.md | https://tot.page/9KD5eQXj5hspA5GDpq0LwQ |
| Guided review | school-core review_gate.py (local) | https://tot.page/9win9YiHsBumDJo2UxxhDQ |

##Decision records (D1–D12)

| ID | Decision | Evidence |
|----|----------|----------|
| D1 | Convert via `UInt64(truncatingIfNeeded:)` | 3 sites in `StdioTransport.swift` |
| D2 | Apply to all 3 call sites | `st_dev` and `st_ino` compared |
| D3 | Regression test via `FileDescriptor.pipe()` | 12/12 passed |
| D4 | No feature flag | Fix is opt-in, no API change |
| D5 | `st_dev == -1` is valid | Socketpair test |
| D6 | Identity comparison | `st_dev`/`st_ino` compared |
| D7 | No live Node.js repro | Local verification + CI |
| D8 | Back-out plan | `git revert` 3 lines |
| D9 | Upstream-compatible | glibc uses `dev_t` |
| D10 | Cross-platform | `FileDescriptor.pipe()` |
| D11 | No feature flag | Fix is opt-in |
| D12 | All tests pass before handoff | 650/650 green |

##Design decisions summary

1. **Convert via `UInt64(truncatingIfNeeded:)`** — preserves bit pattern for identity comparison
2. **3 call sites** — state creation + 2 comparison sites
3. **Regression test** via `FileDescriptor.pipe()` — socketpair simulation
4. **No feature flag** — fix is opt-in, no API change
5. **Back-out plan** — revert 3 lines + remove test

##Verification evidence

| Phase | Command | Output |
|-------|---------|--------|
| Test | `swift test --filter StdioTransport` | 12/12 passed |
| Test | `swift test` | 650/650 passed |
| Build | `swift build` | Clean |
| Bead | `bd show school-core-zjcu` | P2, open |
| Merge | upstream maintainer | ⏳ pending |

---

**Chain of custody:** bead → PRD → SPEC → IMPLEMENT → REVIEW → PR → CI
