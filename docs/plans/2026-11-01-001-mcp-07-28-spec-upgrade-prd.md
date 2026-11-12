---
title: "feat: upgrade OmniRoute MCP surface to 2026-07-28 spec"
created: 2026-11-01
status: proposed
author: lucas (school-core outer loop)
project: agent-school
upstream: https://github.com/diegosouzapw/OmniRoute/pull/15983
fork: branben/omniroute @ feat/mcp-v2-sidecar
tags: [omniroute, mcp, 2026-07-28, sidecar, spec-upgrade, prd]
measured: 2026-11-01 · PR #15983 inspected: 3 commits, 21 files, +2761/-14
---

# Upgrade OmniRoute MCP surface to 2026-07-28 spec

## The one-line ask

Bring OmniRoute's MCP server from its legacy v1 surface to the **2026-07-28
specification** by adding an opt-in native sidecar that speaks the new wire
format (`MCP-Protocol-Version: 2026-07-28`, `server/discover`, `resultType`,
cacheable lists, `_meta` extraction, header-based routing, `outputSchema`,
MRTR contract) while keeping the existing v1 server, sessions, CLI entry, and
full tool catalog untouched for legacy clients.

## Why this is brownfield, not greenfield

The v1 MCP server is live and production-used. `open-sse/mcp-server/` already
exposes 37 tools over stdio, HTTP (SSE), and streamable HTTP. The v1
implementation **rejects** the 2026-07-28 header with HTTP 400 and does not
expose modern discovery. This is not a greenfield MCP server — it is a
parallel, opt-in surface that runs alongside v1, exactly as the spec's
compatibility clause intends.

## The gap, measured

| Capability | v1 today | 2026-07-28 required | Gap |
|---|---|---|---|
| Protocol version header | Rejects `2026-07-28` → 400 | Accept + route | **Hard block** |
| `server/discover` | Absent | Required for v2 | **Hard block** |
| `resultType` field | Absent | Required on responses | **Hard block** |
| Cacheable list results (`ttlMs`, `cacheScope`) | Absent | Required | **Hard block** |
| `_meta` extraction + passthrough | Absent | Required | **Hard block** |
| Mcp-* request headers | Absent | Required | **Hard block** |
| `outputSchema` on tool definitions | Absent | Required | **Hard block** |
| MRTR (model-request-tool-result) contract | Absent | Required | **Hard block** |
| Stateless initialize (no handshake) | v1 requires handshake | v2 drops it | **Hard block** |
| Scope enforcement | v1 has it | Must not be looser in v2 | **Verification** |
| Client metadata → permission grant | v1 had a path | Blocked in v2 | **Verification** |

**Source:** PR #15983 body + diff (`open-sse/mcp-server/v2/server.ts` 979 lines,
`src/app/api/mcp/v2/route.ts`, `tests/unit/mcp/v2-*` 11 test files).

## Scope

| In scope | Out of scope (now) | Explicitly refused |
|---|---|---|
| v2 sidecar under `open-sse/mcp-server/v2/` | Full catalog migration | Replacing v1 |
| 8 tools calling `omniRouteFetch()` against real endpoints | Version routing on main `/api/mcp` | Removing v1 |
| Opt-in gated by `OMNIROUTE_MCP_V2` feature flag | Cancellation propagation | Changing v1 behavior |
| `server/discover`, `resultType`, `_meta`, cacheable lists | Production acceptance testing | Merging without Diego review |
| `src/app/api/mcp/v2/route.ts` (stateless HTTP) | | |
| Scope enforcement ≥ v1 (resolved via v1's DB-aware switch) | | |
| Sanitized transport/tool errors + stdio cleanup handle | | |
| 11 test files (unit + integration) under `tests/unit/mcp/v2/` | | |
| `vitest.mcp.config.ts` for isolated MCP test runner | | |

## GF / BF alternatives

- **GF:** Migrate v1 in-place to 2026-07-28. Rejected: v1 is live, has
  37 tools, and changing it risks every existing client. The spec's
  compatibility clause explicitly supports a parallel v2 surface.
- **BF:** Do nothing — keep rejecting the header. Rejected: OmniRoute would
  remain non-compliant with the current MCP spec, and clients that negotiate
  2026-07-28 would fail at the handshake.
- **BF:** Drop MCP entirely. Rejected: MCP is a first-class protocol for
  OmniRoute; 37 tools are exposed through it.

## What the user sees

A client that sends `MCP-Protocol-Version: 2026-07-28` gets:
- `server/discover` over stdio and HTTP, listing only the 8 v2 tools
  (no unregistered resources/prompts — honest discovery)
- `resultType` on every JSON-RPC response
- `_meta` extracted from tool results and surfaced at the protocol level
- `ttlMs` + `cacheScope` on list-tool results
- `Mcp-*` headers passed through the transport
- `outputSchema` on tool definitions for client-side validation
- MRTR contract on tool calls

A legacy client (no version header or `2024-10-07`) sees v1 unchanged.

## Outcomes we are buying

- OmniRoute is **spec-compliant** for clients that negotiate 2026-07-28
- v1 clients see **zero behavior change** (opt-in switch)
- Scope enforcement in v2 is **at least as strict** as v1
- Client-supplied `_meta` **cannot grant permissions** it didn't have
- The sidecar is a **bounded, reviewable** 8-tool surface, not a full rewrite
- Regression tests prove both the new behavior and the legacy preservation

## Out of scope

- Full catalog migration to v2
- Version routing on the main `/api/mcp` endpoint (only `/api/mcp/v2`)
- Cancellation propagation from v2 transport to v1 sessions
- Production acceptance / canary rollout
- SSE deprecation timeline
- Browser-use / fastmcp SDK integration in school-core (those are U1-U2
  downstream tickets)
