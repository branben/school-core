---
title: Plan B — Session-Aware Admission Slots (Complete Loop Design)
type: feat
status: active
date: 2026-09-15
origin: .scratch/plan-b-lobs.md
anchor: plan-b-session-aware-admission
project: OmniRoute
tags: [admission, resilience, 13648, session, slot]
ai-first: true
updated: 2026-09-15
---

# Plan B — Session-Aware Admission Slots (Complete Loop Design)

**Date:** 2026-09-15
**Status:** Active — ready for executor dispatch
**Plan type:** feat
**Scope:** OmniRoute `chatBodyAdmission.ts`
**Closes:** #13648

## BLUF

One large request holds the slot for 15s. Same-session retries queue for 2s, time out, agent dies. We fix this by letting a session's own siblings skip the line when it already holds a lease.

## Problem

The admission gate (`ChatAdmissionController`) has ONE global budget: `maxHeavyInFlight`. All sessions contend for it. When a session holds the slot for 15s, its own next turn must queue for only 2s before shedding 503. The queue can never bridge the occupancy, so the session self-sheds — 69 times in an afternoon on an 113k-token workload.

Verified locally: bumping `QUEUE_MS` to 20000 + `MAX_HEAVY` to 4 eliminates the shed. But that's a band-aid. The real problem: same-session requests contend against each other.

## Architecture Context

### Current Admission Flow

```
Client → /v1/chat/completions
  → withChatAdmission() wrapper
    → resolveSessionId(request)  ← HMAC fingerprint of API key
    → admitChatRequest(request, { sessionId, queueMs, controller })
      → ChatAdmissionController
        → tryAcquireHeavy()  ← atomic count check
        → tryAcquireHealthyHeadroom()  ← if heap healthy
        → acquireHeavyWithin(queueMs, signal, queuedBytes, sessionKey)
          → park into #queues[sessionKey]
          → timeout or abort → recordShed("queue_timeout")
```

### Key Existing Structures

- `ChatAdmissionController` — core class with `#activeHeavy`, `#queuedBytes`, `#activeHealthy`, `#heavyLeaseStartedAt`, `#queues`, `#fairKeys`, `#fairCursor`
- `IngestByteAdmissionController` — byte-level admission with its own queue system
- `resolveSessionId()` — HMAC fingerprint of API key (already used as fairness key)
- `withChatAdmission.ts` — route wrapper that calls `admitChatRequest`
- `admitChatRequest()` / `admitChatStructure()` — two admission paths
- `AdaptiveAdmissionRuntime` — separate virtual lanes system (combo/fusion fan-out)

## Design — Outer & Inner Loop

### Inner Loop (per-request, hot path)

Runs synchronously on every admission attempt. Zero awaits, zero races.

```
request arrives
  │
  ├─ tryAcquireHeavy()              ← existing: atomic count check (#activeHeavy < maxHeavyInFlight)
  │     success → return lease
  │
  ├─ NEW: tryAcquireSibling(parentLease)
  │     ├─ is this session already holding a lease?
  │     │     ├─ yes + cap available → grant sibling lease, register in #sessionSiblings
  │     │     └─ no → fall through
  │     └─ no → fall through
  │
  ├─ heap pressure?                 ← existing: fast-path healthy headroom
  │     └─ no pressure → tryAcquireHealthyHeadroom()
  │
  └─ park into #queues[sessionKey]  ← existing: bounded wait
        timeout or abort → recordShed("queue_timeout")
```

`tryAcquireSibling()` is sync and atomic — same shape as `tryAcquireHeavy()`. The parent lease is already held; siblings just bypass the global count.

### Outer Loop (release-triggered + background)

Runs on lease release and via an opt-in cleanup timer.

**Release-triggered (every `lease.release()`):**
```
lease.release()
  │
  ├─ existing: decrement #activeHeavy, remove from #heavyLeaseStartedAt
  │
  ├─ NEW: is this a parent lease?
  │     ├─ yes → promote head sibling against global budget
  │     │         ├─ global budget free → sibling promoted to full heavy lease
  │     │         └─ global budget busy → sibling stays in "pending" set
  │     └─ no → clean up from #sessionSiblings
  │
  └─ #dispatchFair() extended: siblings get priority over other keys
```

**Background sweep (opt-in, every N seconds):**
```
cleanup tick (default: disabled)
  │
  ├─ sweep #sessionSiblings
  │     ├─ parent gone but siblings remain → ORPHAN
  │     │     └─ force-release + recordShed("sibling_orphan")
  │     └─ sibling held past TTL without parent → LEAK
  │           └─ force-release + recordShed("sibling_leak")
  │
  └─ if orphans > threshold → log.warn + increment counter
```

## New State

```typescript
#sessionSiblings = new Map<string, {
  parent: symbol,
  siblings: Set<symbol>,
  pending: number,
}>();

#siblingTtlMs: number;           // 0 = disabled (default)
#siblingMaxPerSession: number;   // cap, default 4
```

## Failure Modes

| Mode | Detection | Mitigation |
|------|-----------|------------|
| Sibling leak (parent died) | Background sweep | Force-release + shed counter |
| Sibling cap abuse | Per-session Set size check | Reject new siblings at cap |
| Parent release race | Atomic state transition | CAS on parent token before promotion |
| Mixed parent+sibling in queue | Queue tagging | Siblings skip the queue, go straight to promotion |

## Rollout

Phase 1: Feature flag off by default.
Phase 2: Enable for internal dogfood.
Phase 3: Flip default after production validation.

## Requirements

### R1 — Inner Loop
- R1.1 `tryAcquireSibling(parentLease)` is sync, atomic, zero awaits.
- R1.2 Sibling acquisition only succeeds if same session already holds a parent lease.
- R1.3 Per-session sibling cap enforced (default 4).
- R1.4 Sibling acquisition bypasses the global `maxHeavyInFlight` count.

### R2 — Outer Loop
- R2.1 On parent release, pending siblings promoted against global budget (not unconditionally).
- R2.2 `#dispatchFair()` gives siblings priority over unrelated keys.
- R2.3 Background sweep detects orphaned siblings (parent died) and force-releases them.
- R2.4 Background sweep detects leaked siblings (held past TTL without parent).

### R3 — Observability
- R3.1 `snapshot()` returns `activeSiblings` and `siblingsBySession`.
- R3.2 New shed reasons: `sibling_orphan`, `sibling_leak`.
- R3.3 `/monitoring/health` exposes live sibling counts.

### R4 — Safety
- R4.1 Feature flag `OMNIROUTE_CHAT_SESSION_SIBLINGS_ENABLED` (default: false).
- R4.2 Configurable cap `OMNIROUTE_CHAT_SESSION_MAX_SIBLINGS` (default: 4).
- R4.3 Configurable TTL `OMNIROUTE_CHAT_SESSION_SIBLING_TTL_MS` (default: 0 = disabled).
- R4.4 Sibling leases do not extend `retryAfterSeconds()` occupancy math.

## Test Plan

| Suite | Tests | Evidence |
|-------|-------|----------|
| Unit: sibling acquisition | 5+ | sibling grants, cap rejects, parentless rejects |
| Unit: parent release reconciliation | 3+ | promote, queue, fairness |
| Unit: orphan/leak detection | 4+ | parent death, TTL expiry, sweep |
| Integration: mixed workload | 3+ | multi-session fairness, no starvation |
| Load: 113k-token repro | 1 | zero sheds under concurrent fan-out |

## Estimation

| Slice | Effort | Files |
|-------|--------|-------|
| Inner loop (tryAcquireSibling) | 3 days | chatBodyAdmission.ts |
| Outer loop (release + sweep) | 3 days | chatBodyAdmission.ts |
| Observability | 1 day | chatBodyAdmission.ts, health route |
| Tests | 3 days | chat-body-admission.test.ts |
| **Total** | **~2 weeks** | |

## Acceptance Criteria

- [ ] Same-session requests no longer shed when parent holds the slot
- [ ] Cross-session fairness preserved (one session can't starve others)
- [ ] Orphaned siblings force-released within TTL window
- [ ] Feature flag defaults to off; no behavior change without opt-in
- [ ] All existing admission tests pass unchanged
- [ ] New test suite: 20+ tests covering sibling paths
