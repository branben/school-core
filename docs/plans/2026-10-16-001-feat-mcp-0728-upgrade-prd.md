# PRD: MCP 2026-07-28 spec support for OpenHands/software-agent-sdk

**Status:** Durable reference (outer-loop planning artifact, NOT an open PR)
**Branch:** `outer/mcp-0728-spec-v2` (pushed for durability only — no PR)
**Issues:** software-agent-sdk #6017 (epic), #6018 (SSE/headers), #6019 (OAuth issuer), #6020 (OAuth CIMD); OpenHands #18495 (GUI SSE)
**Blocked by:** software-agent-sdk #5152 (browser-use vs mcp 2.x)

## Problem

The MCP protocol spec released **2026-07-28** (mcp 2.x) introduces breaking changes that
the SDK's client stack (fastmcp>=3.2.0,<4 + mcp 1.x/2025-11-25) does not support:
stateless operation (no Mcp-Session-Id), per-request `_meta`
(protocolVersion/clientInfo/clientCapabilities), required transport headers
(Mcp-Method/Mcp-Name/MCP-Protocol-Version), new `server/discover`, OAuth 2.1 hardening
(SEP-2352/RFC 9207 iss validation), and deprecations (DCR→CIMD, HTTP+SSE, Roots/Sampling/Logging).

## Goals

1. Upgrade the SDK's MCP client stack to mcp 2.x / fastmcp 4.x **without breaking
   session-based auth persistence (#1739)** — the hard requirement.
2. Keep dual-version compatibility (tests pass on BOTH fastmcp 3.4.8/mcp1.x and
   fastmcp 4.1.0/mcp 2.3.0) until #5152 is resolved.
3. Preserve browser-use tool surface (do NOT force the 0.13.x migration; only raise the
   floor to what's compatible).
4. 219-test MCP regression gate stays green.

## Non-goals

- Browser-use 0.13.x migration (tracked separately as U7 / upstream).
- GUI MCP SSE deprecation UI (OpenHands #18495 — separate repo).
- Cloud-side OAuth store changes beyond SDK compat.

## Success criteria

- Dual-version green: **147 tests pass in BOTH** fastmcp 4.1.0 (mcp 2.3.0) and fastmcp 3.4.8 (mcp 1.x).
- No `FastMCPDeprecationWarning` under `-W error`.
- `#1739` session auth works under mcp 2.x via `mode="legacy"` (stateful preserved).