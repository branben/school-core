# Wayfinder Map — School-Core Pipeline Gaps (2026-08-11)

> Reconnaissance canvas. Parent-owned; subagents read-only. No commits unless asked.
> Destination: the school-loop issue path exercising the full pedagogical stack —
> 4-layer context, compiler-before-critic, FirstMate crew dispatch — not just the
> direct-Orca path that works today.

## Frontiers

### F1 — Layer 3 (archival) never fires
**Hypothesis:** `enrich_prompt` only includes archival context when `session_id` is
passed; the bridge calls `director.run_task(...)` without one.
**Evidence:** `issue_bridge.py:475` (`run_task(prompt, domain, difficulty, force_agent, store)` — no `session_id`);
`context_orchestrator.py` `_archival_context` gated on `if session_id:`; `data/consolidation/` absent.
**Verdict:** CONFIRMED (disk). Fix = bridge threads a per-cycle session_id + seed `data/consolidation/`.

### F2 — Layer 2 (Engram trajectories) empty on fresh checkouts
**Hypothesis:** `data/trajectories/` is not tracked in git, so each school-loop fresh
checkout sees zero trajectory history → file-RAG no-ops.
**Evidence:** `git ls-files data/trajectories/` → empty; `data/trajectories/*.json` exists locally only.
**Verdict:** CONFIRMED (disk). Fix = checkpoint-commit sanitized trajectories (same pattern as scores/retry files).

### F3 — Verify gate (compiler-before-critic) silently no-ops
**Hypothesis:** `_run_verify_gate` requires Nix (`flake.nix#verifyShell`); Nix is not
installed on the Mac runner → gate returns None (non-blocking) every cycle.
**Evidence:** `which nix` → not-installed; `verify_gate.py` subprocesses `nix develop`;
school-loop.yml has no Nix setup step.
**Verdict:** RESOLVED (delegated research) — see findings below.

### F4 — FirstMate crew dispatch not wired into the issue path
**Hypothesis:** campus.md claims "✅ Wired" but the bridge's `run_task → call_model`
path never invokes `fm-spawn`; crew works standalone (proven 2026-08-11) but isn't
in the flow.
**Evidence:** zero `firstmate`/`fm-spawn` refs in `director.py`/`issue_bridge.py`;
live crew drill succeeded standalone; campus.md table row overclaims.
**Verdict:** RESOLVED (delegated research + parent disk grounding) — see findings below.

### F5 — campus.md status table overclaims
**Hypothesis:** "FirstMate dispatch ✅ Wired" and "Verify gate ✅ Wired" are wrong.
**Evidence:** campus.md Operational Reality table; confirmed F3/F4 above.
**Verdict:** CONFIRMED (disk). Fix = correct the two rows + note Layer 3 dormant.

## Delegated findings (2026-08-11)

### F3 → RESOLVED: verify gate needs Nix or a uv fallback
- Repo side: `flake.nix` defines `verifyShell` (hermetic mkShell, no network, repo
  mounted read-only at runtime). NO `uv.lock`/`pyproject.toml` — only `requirements.txt`.
- Web research recommendation, in order:
  1. **Install Determinate Nix on the self-hosted runner** via
     `DeterminateSystems/determinate-nix-action@v3` (or pre-install on the Mac) —
     makes the existing flake gate actually run; ~30-60s cold start, needs sudo.
  2. **uv fallback** (`astral-sh/setup-uv@v5` + `uv run pytest` in an isolated venv)
     when Nix is absent — but requires adding a `pyproject.toml`/lockfile.
  3. **Sandboxing**: macOS native `sandbox_init_with_parameters` via ctypes, or
     Podman for real isolation of student code (heavier).
- Decision pending: block-on-Nix vs warn-and-fallback (policy, not research).

### F4 → RESOLVED: FirstMate ship-task contract (grounded in real source)
- Lifecycle: brief (`fm-brief.sh`) → spawn (`fm-spawn.sh`) → crewmate executes in
  worktree → signals via **status file** `$STATE/$ID.status` (append lines:
  `working:` / `blocked:` / `done:` / `failed:`) → meta written to `$STATE/$ID.meta`
  (backend, worktree, terminal, window) → teardown (`fm-teardown.sh`).
- Ship modes (from fm-brief.sh templates): **no-mistakes** = agent commits to its
  branch, appends `done:`, then firstmate runs the no-mistakes pipeline to validate
  and ship a PR; **direct-PR** = agent raises the PR itself, appends `done: PR <url>`;
  **local-only** = agent commits to branch `fm/<id>`, firstmate merges locally.
- Scout mode: deliverable is `data/<id>/report.md`, append `done:` when complete.
- Minimal parent-bridge interface to wire this: scaffold brief → `fm-spawn.sh`
  (backend orca, hermes-fm-wrapper) → poll status file for a terminal state →
  read meta + report → teardown (with the known orca teardown bug workaround).

## Not yet specified
- Whether the verify gate should block or warn when Nix is unavailable (policy decision).
- Whether FirstMate replaces the direct-Orca path or wraps it (architecture decision).
- Whether the school-loop's checkpoint should commit trajectories (size/PII tradeoff).

## Out of scope
- Async teacher-worktree lifecycle + AgentMail rubber-stamp loop (conductor mode) — the
  school-loop is sync-inline by design today.
- spec-gate DOD / CE-loop / complex-task modes — flag-gated, not pipeline defaults.

## Connect-the-dots verdict
The five gaps collapse into **two work streams**:
1. **Context + memory stream** (F1+F2, ~small): thread a session_id through the
   bridge → Layer 3 fires; checkpoint-commit trajectories → Layer 2 works across
   cycles. No architecture change, no new deps.
2. **Execution-rigor stream** (F3+F4, larger): make the verify gate real
   (Determinate Nix on the runner, or uv fallback) AND wire firstmate dispatch into
   the execution path. Both are self-contained; F4 subsumes the previously proven
   crew drill. F5 is a 5-minute doc fix that should land with whichever stream ships first.
