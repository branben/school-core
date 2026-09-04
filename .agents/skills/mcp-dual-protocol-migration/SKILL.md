---
name: mcp-dual-protocol-migration
description: Patterns for migrating MCP servers between protocol versions with dual support, contract pinning, and avoiding cross-protocol breakage. Use when supporting multiple MCP protocol versions simultaneously, doing stateless migrations (2025-11-25 to 2026-07-28), or dealing with SDK v1/v2 splits.
---

# MCP Dual-Protocol Migration with Contract Pinning

When supporting multiple MCP protocol versions on the same endpoint (dual support), contracts can become ambiguous. The pin-per-surface approach prevents cross-protocol breakage.

## The Problem

Serving different tool surfaces/capabilities based on negotiated protocol version on the same endpoint makes "the server's contract" undefined. Changes can break one protocol version while "fixing" another.

## The Solution: Pin Per Surface

For each protocol version, capture and pin its contract separately:

1. **Snapshot contracts per protocol version** - Capture exact tool schemas, capabilities, server info as seen by clients of each protocol version
2. **Diff independently** - Compare pins to understand changes per protocol version
3. **Track dependency provenance** - Record schema-dependency versions (SDK versions). Breaking changes might come from SDK updates, not your code
4. **Enforce isolation** - Ensure changes to 2026 surface don't silently affect 2025 surface and vice versa

## Implementation Pattern

```python
# Pseudocode
for each protocol_version in negotiated_versions:
    pin = capture_contract(server_state, protocol_version, sdk_versions)
    store_pin(pin_path_for(protocol_version, sdk_versions))
    diff_against_previous(pin)
```

## Key Considerations

- **Same endpoint, different contracts**: If version routing happens on the main endpoint, pinning is critical
- **Separate endpoints are easier**: Sidecar approaches (like #15983's opt-in v2) avoid this ambiguity initially
- **Cancellation semantics differ**: 2025-11-25 uses explicit `notifications/cancelled` or timeouts; 2026-07-28 treats closing request stream as cancellation
- **Session model differs**: 2025-11-25 is session-based; 2026-07-28 is stateless
- **SDK splits**: 2026 support often lives in v2 packages (`@modelcontextprotocol/server`), not v1 SDK

## Recent Work Summary

### 2025-11-25 → 2026-07-28 Migration (Deterministic)

- Converted session-based operations to stateless request/response pairs
- Replaced session IDs with stable stream IDs for cancellation tracking
- Added `--stream-id` to all fetch-mode commands (fetch_tasks, fetch_issues, fetch_activity, etc.)
- Updated tests to use stream IDs instead of session IDs

### 2026-07-28 Specific

- **VerifierGate class**: Added dual-construction support (string config vs. builder pattern)
- **VerifyGateFactory**: Registered class-based singletons for extension
- **Explicit endpoints**: v2 protocol enabled only on `/v2/` paths; v1 surface on `/`

### 2026-07-29 Specific

- **Restore session semantics**: Added `session_id` and `session` parameters back to `list_workspaces`, `list_tasks`, `list_issues`
- **Analyze vs. AnalyzeWorktree**: `AnalyzeOrchestrator` (stateless, request-id-based) vs. `TeacherWorktree` (session-based)
- **Additional params**: `working_directory` and `resolved_from_scripts` passed to every function

### 2026-07-30 Specific

- **Container cooldown**: Removed parent's container cooldown to allow per-worktree reuse
- **VENV_PATH env var**: Override `VENV_PATH` in container entrypoint for custom site-packages
- **Remove legacy code**: Confirmed v1/condense-pipeline cleanup can safely remove session-based code

### 2026-07-31 Specific (master branch)

- **ContainerSpec injection**: `ContainerManager` delegates to `icm.ContainerManager(container_spec=...)`
- **Session-aware containers**: ContainerManager now needs `container_spec`, `type`, `layer`, `commit`, `branch`, `sku`
- **Two classes**: `ContainerOwner` (old) and `ContainerManager`/`ManagerContract` (new)

## When to Apply

- Migrating between MCP protocol versions
- Supporting dual protocols during transition
- Debugging cross-protocol compatibility issues
- When same endpoint serves different contracts by version
