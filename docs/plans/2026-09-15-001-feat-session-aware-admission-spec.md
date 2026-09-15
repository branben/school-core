---
type: spec
title: Session-Aware Admission Slots — SPEC
status: active
date: 2026-09-15
plan: docs/plans/2026-09-15-001-feat-session-aware-admission-plan.md
project: OmniRoute
tags: [admission, 13648, session, slot]
updated: 2026-09-15
---

# SPEC — Session-Aware Admission Slots

**References:** Plan `docs/plans/2026-09-15-001-feat-session-aware-admission-plan.md`

## 1. Interface Changes

### `ChatAdmissionController`

```typescript
// NEW: Constructor option
interface ChatAdmissionControllerOptions {
  // existing fields...
  siblingMaxPerSession?: number;  // default: 4
  siblingTtlMs?: number;          // default: 0 (disabled)
}

// NEW: Sibling acquisition result
interface SiblingLease {
  sessionId: string;
  token: symbol;
  release(): void;
}
```

### New Public Methods

```typescript
class ChatAdmissionController {
  // NEW: Sync sibling acquisition — bypasses global count if parent held
  tryAcquireSibling(parentLease: ChatAdmissionLease): SiblingLease | null;

  // NEW: Background sweep — call from setInterval or release hook
  sweepOrphans(now?: number): { orphans: number; leaks: number };
}
```

### Snapshot Extension

```typescript
interface ChatAdmissionSnapshot {
  // existing fields...
  activeSiblings: number;
  siblingsBySession: ReadonlyArray<{ sessionId: string; count: number }>;
}
```

## 2. Inner Loop Contract

| Step | Input | Output | Side-effect |
|------|-------|--------|-------------|
| `tryAcquireSibling(parentLease)` | parent token, session ID | `SiblingLease` or `null` | Increment `#sessionSiblings[session].siblings` |
| Cap check | `#sessionSiblings[session].siblings.size` | boolean | Reject if ≥ `siblingMaxPerSession` |
| Parent validation | parent token exists in `#heavyLeaseStartedAt` | boolean | Reject if parent already released |

**Invariant:** `tryAcquireSibling()` never awaits. Never throws. Returns `null` cleanly.

## 3. Outer Loop Contract

### Release-triggered

On `lease.release()`:
1. Check if releasing lease is a parent (exists in `#sessionSiblings`).
2. If yes: promote first pending sibling to full heavy lease (global count check).
3. If promoted: wake sibling waiter via `#dispatchFair()` priority path.
4. Remove parent entry from `#sessionSiblings`.

### Background sweep

Every `siblingTtlMs` milliseconds (if enabled):
1. Iterate `#sessionSiblings`.
2. For each entry: check if parent still exists in `#heavyLeaseStartedAt`.
3. If parent gone: force-release all siblings, increment `shedTotal` with reason `sibling_orphan`.
4. If entry age > `siblingTtlMs`: force-release, increment with reason `sibling_leak`.

## 4. State Machine

```
Session States:
  IDLE → (tryAcquireHeavy) → HOLDER
  HOLDER → (tryAcquireSibling) → PARENT(+1 sibling)
  PARENT → (sibling release) → PARENT(n-1 siblings)
  PARENT → (parent release) → pending promotion → IDLE
  PARENT → (parent dies) → ORPHAN → sweep → IDLE
```

## 5. Fairness Guarantees

1. **Cross-session:** Siblings only bypass the global count when same session holds. Other sessions still contend via `#dispatchFair()` round-robin.
2. **Promotion:** Siblings promoted on parent release must pass `tryAcquireHeavy()` — they don't unconditionally grab a slot.
3. **No starvation:** `#dispatchFair()` still rotates keys. Siblings only get priority for the slot freed by their parent.

## 6. Observability

| Metric | Type | Source |
|--------|------|--------|
| `admission.siblings.active` | Gauge | `#sessionSiblings` total |
| `admission.siblings.promoted` | Counter | Successful promotions |
| `admission.siblings.orphan` | Counter | Orphan sweeps |
| `admission.siblings.leak` | Counter | Leak sweeps |
| `shed.reason=sibling_orphan` | Log | `recordShed()` |
| `shed.reason=sibling_leak` | Log | `recordShed()` |

## 7. Feature Flags

| Flag | Default | Effect |
|------|---------|--------|
| `OMNIROUTE_CHAT_SESSION_SIBLINGS_ENABLED` | `false` | Master switch — no sibling acquisition |
| `OMNIROUTE_CHAT_SESSION_MAX_SIBLINGS` | `4` | Cap per session |
| `OMNIROUTE_CHAT_SESSION_SIBLING_TTL_MS` | `0` | Background sweep interval (0 = disabled) |

## 8. Error Handling

| Error | Response |
|-------|----------|
| Parent released during sibling acquire | Return `null` (fall through to queue) |
| Parent dies (orphan) | Force-release siblings, log warn |
| Sibling held past TTL (leak) | Force-release, log error |
| CAS race on promotion | Retry once, then queue normally |

## 9. Test Vectors

### Unit — Inner Loop
- `sibling_grant_when_parent_held` → returns `SiblingLease`
- `sibling_reject_when_no_parent` → returns `null`
- `sibling_reject_at_cap` → cap reached, returns `null`
- `sibling_sync_no_await` → no async, no race
- `sibling_release_decrements` → release cleans up set

### Unit — Outer Loop
- `parent_release_promotes_sibling` → sibling becomes heavy
- `parent_release_respects_global_budget` → promotion blocked when busy
- `orphan_detected_and_released` → sweep force-releases
- `leak_detected_and_released` → TTL triggers release

### Unit — Fairness
- `cross_session_no_starvation` → other sessions served via dispatchFair
- `sibling_priority_only_on_parent_release` → no priority otherwise
- `sibling_bypass_does_not_inflate_global_count` → snapshot unchanged

### Integration
- `multi_session_fairness` → 3 sessions, verify round-robin
- `113k_token_no_shed` → reproduce original bug, verify fix
- `feature_flag_off_no_change` → all behavior identical

## 10. Migration

No migration needed. New state is additive. Existing deployments unaffected when flag is off.
