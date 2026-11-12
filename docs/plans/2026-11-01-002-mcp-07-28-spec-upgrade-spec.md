---
title: "spec: MCP 2026-07-28 upgrade — implementation plan"
created: 2026-11-01
status: proposed
author: lucas (school-core outer loop)
project: agent-school
prd: docs/plans/2026-11-01-001-mcp-07-28-spec-upgrade-prd.md
upstream: https://github.com/diegosouzapw/OmniRoute/pull/15983
repo: branben/omniroute @ feat/mcp-v2-sidecar
measured: 2026-11-01 · PR #15983 inspected: 3 commits, 21 files, +2761/-14; base release/v3.8.53
tags: [spec, omniroute, mcp, 2026-07-28, implementation, sdlc]
---

# SPEC — MCP 2026-07-28 spec upgrade

## Decisions locked before any code

| # | Decision | Alternative rejected | Why |
|---|---|---|---|
| D1 | **Parallel v2 sidecar** under `open-sse/mcp-server/v2/` | Migrate v1 in-place | v1 is live with 37 tools and production clients. The spec's compatibility clause supports a parallel surface. Changing v1 risks every existing client. |
| D2 | **Opt-in via `OMNIROUTE_MCP_V2` feature flag** (DB-overridable) | Always-on v2 | Existing v1 clients must see zero behavior change. The flag is registered in `featureFlagDefinitions` (registry slot 85) so operators can toggle per-environment, not just via env. |
| D3 | **Scope enforcement resolved through v1's DB-aware switch** | Read env at module load | v1's dashboard toggle is DB-backed. If v2 read env at module load, an env-only toggle would let v2 stay open while v1 enforced — a security gap. |
| D4 | **Scopes from authenticated request only** — `withScopeEnforcement` passes only `{authInfo, sessionId}` | Pass full `_meta` from client | A caller that self-grants a scope in `_meta` must still be refused. The shared function's client-supplied `_meta` branch is unreachable from v2. |
| D5 | **No identity reflection on denial surface** — use `buildScopeDenialMessage` | Echo caller/source like v1 did pre-S-04 | `callerId` is caller-influenced, so it stays in the audit payload only. The denial message itself does not echo it. |
| D6 | **Separate HTTP route** at `/api/mcp/v2` with v1's exact auth classification | Share v1's `/api/mcp` route | v2 is stateless (no `Mcp-Session-Id` handshake). Sharing the route would force v1's session lifecycle onto v2. |
| D7 | **Stateless per-request server+transport** | Persistent v2 server | 2026-07-28 drops the initialize handshake. A fresh server+transport pair per request matches the spec and avoids state leakage between clients. |
| D8 | **Honest `server/discover`** — tools only, no unregistered resources/prompts | Expose full catalog via discover | The v2 sidecar has exactly 8 tools. discover must answer truthfully. |
| D9 | **8 tools only**, all calling `omniRouteFetch()` against real endpoints | Add more tools in this PR | Bounded contribution toward #17131. Full catalog migration is separate work. |
| D10 | **Sanitized transport/tool errors** — no stack traces or internal paths leak to client | Pass errors through raw | Production safety. |
| D11 | **Return stdio cleanup handle** from `startMcpStdioV2()` | Fire-and-forget stdio | Allows the caller to close the transport cleanly. |
| D12 | **Tests reuse repository isolation fixture + explicit empty plugin dir** | Spin up a real plugin directory | Keeps tests hermetic. Child transports are closed after each test, including timeouts. |

## Milestone map → tickets

| Milestone | Ticket | Title | Dependencies |
|---|---|---|---|
| M1 | `school-core-emb` (U1) | browser-use unblock for mcp2 (cap browser-use<0.12 + shim decorators) | none |
| M2 | `school-core-58y` (U2) | fastmcp 4.x stateless support in SDK | U1 |
| M3 | `school-core-77r` (U3) | Mcp-* headers + _meta passthrough | U2 |
| M3 | `school-core-4qs` (U4) | server/discover + SSE-only deprecation | U2 |
| M3 | `school-core-ven` (U5) | OAuth issuer-keyed RFC 9207 | U2 |
| M3 | `school-core-te6` (U6) | OAuth CIMD/application_type | U2 |
| M4 | `school-core-rxp` (U7) | upstream browser-use 0.13.x (tracked) | U2 |
| M5 | `school-core-hr6` | Epic: close when all U-items ship | all |

## Milestone 1 — v2 sidecar scaffold (`open-sse/mcp-server/v2/`)

**New module:** `open-sse/mcp-server/v2/server.ts`

- Imports pinned `@modelcontextprotocol/sdk` 2.3.1 server/client
- Exports: `createMcpServerV2`, `startMcpStdioV2`, `handleMcpV2Http`
- 8 tool definitions, each calling `omniRouteFetch()` against real OmniRoute
  endpoints
- Scope enforcement via `withScopeEnforcement` (opt-in, mirrors v1)
- `server/discover` registered as a real request handler (not a stub)
- Barrel re-export from `open-sse/mcp-server/index.ts`

**New route:** `src/app/api/mcp/v2/route.ts`

- `POST`/`GET`/`DELETE` at `/api/mcp/v2`
- Auth: `requireManagementAuth` with `acceptMcpConnectScope`
- Gated on `settings.mcpEnabled` AND `OMNIROUTE_MCP_V2`
- Stateless: no `Mcp-Session-Id`; fresh server+transport per request
- Calls `handleMcpV2Http()`

**Feature flag:** `OMNIROUTE_MCP_V2` added to `featureFlagDefinitions.ts`
(registry slot 85). Read at module load, like `OMNIROUTE_MCP_ENFORCE_SCOPES`.

## Milestone 2 — protocol compliance details

**Headers + routing:**
- Accept `MCP-Protocol-Version: 2026-07-28`; route to v2 handler
- Reject unknown versions with clear error (not a 400 fallback to v1)
- `Mcp-*` headers passed through transport layers

**`server/discover`:**
- Returns `{ tools: [...] }` only — no unregistered resources or prompts
- Each tool includes `outputSchema` where applicable
- `resultType` field on every JSON-RPC response

**List caching:**
- `ttlMs` and `cacheScope` on list-tool results per spec
- `cacheable list results` flag in discover metadata

**`_meta` passthrough:**
- Extract `_meta` from tool results
- Surface at protocol level without exposing internal fields
- Client-supplied `_meta` cannot grant permissions (D4)

**MRTR contract:**
- Tool calls return `model-request-tool-result` shape
- Errors follow MRTR error envelope

## Milestone 3 — security and hardening

**Scope enforcement (D3):**
- v2 resolves `isMcpScopeEnforcementEnabled()` through v1's DB-aware function
- Not read from env at module load

**Identity hygiene (D5):**
- `buildScopeDenialMessage` replaces any caller-echo
- `callerId` stays in audit payload only

**Error sanitization (D10):**
- Transport errors: no stack traces
- Tool errors: no internal paths or SQL strings
- Stdio cleanup handle returned (D11)

## Milestone 4 — tests

**11 test files under `tests/unit/mcp/v2/`:**

| File | What it proves |
|---|---|
| `v2-server.spec.ts` | Core v2 server behavior, tool dispatch, scope enforcement |
| `v2-discover-honesty.spec.ts` | discover returns tools only, no fake resources/prompts |
| `v2-esm-link.spec.ts` | ESM import of v2 server works (link resolution) |
| `v2-feature-flag.spec.ts` | `OMNIROUTE_MCP_V2` gating; DB-overridable |
| `v2-gaps.spec.ts` | Specific 2026-07-28 gaps that v1 failed |
| `v2-http-route.spec.ts` | Route gating, stateless discover/list/call over real HTTP |
| `v2-parity.spec.ts` | v2 parity with v1 for shared behaviors |
| `v2-scope-enforcement.spec.ts` | Scope enforcement ≥ v1; `_meta` cannot grant |
| `v2-tool-smoke.spec.ts` | 8 tools smoke test against real endpoints |
| `v2-modern.spec.ts` | Modern (2026-07-28) stdio behavior |
| `v2-legacy.spec.ts` | Legacy stdio still works unchanged |

**Isolation:** repository isolation fixture + explicit empty plugin directory.
Child transports closed after each test, including timeouts.

**Vitest config:** `vitest.mcp.config.ts` — isolated MCP test runner so MCP
tests don't pollute the main suite.

## Verification commands

```bash
# Focused MCP suite (Vitest)
npm run test:vitest -- --config vitest.mcp.config.ts

# Expected: 92/94 pass (2 known Node ESM startup probes exceed 60s deadline)
# Do NOT increase the deadline — that would mask the real issue.

# ESLint on changed files
npm run lint -- open-sse/mcp-server/v2 src/app/api/mcp/v2 tests/unit/mcp

# Typecheck (API scope)
npm run typecheck:core

# Changelog integrity
scripts/check/check-commit-evidence.sh

# Test discovery
scripts/check/check-test-discovery.mjs
```

## Measured evidence from PR #15983

- **92/94** MCP tests passed across 11 files on head `62937d3`
- **46/46** Node feature-flags suite passed
- **Total distinct focused cases: 138/140 passed**
- Only 2 real Node ESM startup probes exceed unchanged 60s deadline
- ESLint clean; API typecheck passed
- Changelog integrity, test discovery, scoped file-size, isolation-test ESLint passed
- No timeout was increased; interrupted earlier runs not counted as passes

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| SDK 2.3.1 API mismatch with current `@modelcontextprotocol/sdk` | low | Pinned in `package.json`; verified in tests |
| v2 flag accidentally on in production | low | Off by default; DB-overridable; Diego review required before merge |
| Scope enforcement weaker than v1 | low | Resolved through v1's DB-aware switch (D3); `_meta` grant blocked (D4) |
| Client confusion between `/api/mcp` and `/api/mcp/v2` | medium | Separate route, separate path; v1 unchanged |
| Node ESM startup probes flaky | medium | Known, measured, not masked — separate ticket if needed |

## Out of scope

- Full catalog migration to v2
- Version routing on main `/api/mcp` endpoint
- Cancellation propagation
- Production acceptance / canary
- SSE deprecation timeline
- Browser-use / fastmcp SDK school-core tickets (U1-U2)
- Merging without maintainer review
