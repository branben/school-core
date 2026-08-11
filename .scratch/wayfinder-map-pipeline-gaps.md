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
**Verdict:** CONFIRMED (disk) — STILL OPEN (U1, audit 2026-08-11). Fix = bridge threads a per-cycle session_id + seed `data/consolidation/`.

### F2 — Layer 2 (Engram trajectories) empty on fresh checkouts
**Hypothesis:** `data/trajectories/` is not tracked in git, so each school-loop fresh
checkout sees zero trajectory history → file-RAG no-ops.
**Evidence:** `git ls-files data/trajectories/` → empty; `data/trajectories/*.json` exists locally only.
**Verdict:** CONFIRMED (disk) — STILL OPEN (U2, audit 2026-08-11). Fix = checkpoint-commit sanitized trajectories (same pattern as scores/retry files).

### F3 — Verify gate (compiler-before-critic) — historical baseline: silently no-op
**Hypothesis (before the 2026-08-11 reconciliation):** `_run_verify_gate` requires Nix (`flake.nix#verifyShell`); Nix was not installed on the Mac runner → the gate returned None (non-blocking) every cycle.
**Evidence at discovery time:** `which nix` → not-installed; `verify_gate.py` subprocessed `nix develop`; school-loop.yml had no Nix setup step.
**Verdict:** RESOLVED + IMPLEMENTED (2026-08-11) — Determinate Nix installed on the runner, the production preflight is hard-fail on missing infrastructure, and the reusable gate is loud + proven live with soft-skip/strict-escalation modes. See audit section.

### F4 — FirstMate crew dispatch not wired into the issue path
**Hypothesis:** campus.md claims "✅ Wired" but the bridge's `run_task → call_model`
path never invokes `fm-spawn`; crew works standalone (proven 2026-08-11) but isn't
in the flow.
**Evidence:** zero `firstmate`/`fm-spawn` refs in `director.py`/`issue_bridge.py`;
live crew drill succeeded standalone; campus.md table row overclaims.
**Verdict:** PARTIAL (2026-08-11) — crew proven + fm_doctor preflight live, but dispatch NOT wired into the issue path (U4 still open). See audit section.

### F5 — campus.md status table overclaims
**Hypothesis:** "FirstMate dispatch ✅ Wired" and "Verify gate ✅ Wired" are wrong.
**Evidence:** campus.md Operational Reality table; confirmed F3/F4 above.
**Verdict:** CONFIRMED (disk) — STILL OPEN (U5, audit 2026-08-11). Fix = correct the two rows + note Layer 3 dormant.

## Delegated findings (2026-08-11)

### F3 → RESOLVED: production preflight requires Nix; reusable gate remains explicit soft-skip
- Repo side: `flake.nix` defines `verifyShell` (hermetic mkShell, no network, repo
  mounted read-only at runtime). NO `uv.lock`/`pyproject.toml` — only `requirements.txt`.
- Determinate Nix is installed and proven live on the self-hosted runner. The
  school-loop execute preflight now fails clearly when Nix or `verifyShell` is
  unavailable, while the hosted board job still publishes committed state.
- `verify_gate.py` keeps a visible soft-skip for direct/manual callers when Nix
  is missing or commands are undiscoverable; `VERIFY_GATE_STRICT=1` escalates
  that result to an issue-level failure.
- A uv fallback remains future work, not current policy: it would require a
  separate `pyproject.toml`/lockfile and acceptance decision.

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

## Policy decisions (reconciled 2026-08-11)
- **Nix policy:** the production school-loop preflight blocks the execute job when Nix or `flake.nix#verifyShell` is unavailable; the hosted board job still publishes committed state. The reusable `verify_gate.py` API remains visible soft-skip by default for direct/manual callers, and `VERIFY_GATE_STRICT=1` escalates it to an issue failure.
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

---

## Audit 2026-08-11 — post-implementation status (U1–U6)

> Second pass, grounded in code + git + beads (not assumption). Bead tracker:
> 7 open units; `school-core-21l` is a separate OmniRoute P2.5 (out of scope).
> Frontier verdicts above are updated to match; this section is the per-unit ledger.

### Resolved since the map was written

| Item | Evidence |
|---|---|
| **F3 — Verify gate (compiler-before-critic)** | Determinate Nix 3.21.9 installed on the Mac runner (daemon live, encrypted APFS volume, 62+ store entries). `flake.nix` fixed + pinned (**commit `44f5152`**): `nodejs_22` (20 was EOL/insecure), `pkgs.pnpm` (26.05 removed `nodePackages`). The school-loop preflight hard-fails when Nix or `verifyShell` is unavailable; the hosted board job still publishes committed state. `verify_gate.py` remains reusable and loud: `_find_nix()` discovery, explicit soft-skip verdict for direct/manual callers, strict escalation via `VERIFY_GATE_STRICT=1`, dir-form flake ref. `flake_path` pinned to the checkout (CI-parity footgun, regression-tested). `project_verify.yaml` added — 1 hermetic stdlib command, shadows the 9 doomed orca/mobile npm commands. **Proven live** through the real Nix shell: 1 cmd, PASS, no warning. |
| **U6 ops half — `entire enable`** | Run in school-core: `.entire/` exists (logs/metadata/settings.json), gitignored + committed (`6705ae5`). `entire review` (labs) verified working via claude-code agent on 2026-08-11. |

### Still open (by plan unit)

| Unit | Bead | Status | Remaining work |
|---|---|---|---|
| **U1** — session_id threading | 0x9.1 OPEN (notes) | **DONE (code, uncommitted)** | Bridge derives `loop-YYYYMMDD-HHMMSS` per cycle → passes to `run_task` → forwards to `enrich_prompt` (director.py:714) → Layer 3 gate fires (proven live: planted consolidation YAML produces archival context). `data/sessions/consolidation/index.md` seeded (needs `git add -f` at commit — data/ is gitignored). KNOWN LIMIT: nothing writes consolidations under `loop-*` ids yet (sleep uses `ses_*`), so Layer 3 fires but finds its own cycles' archives only once the write side is hooked in. |
| **U2** — trajectory durability | 0x9.2 OPEN (notes) | **DONE (code, uncommitted)** | `sanitize_data.py` gains `trim_trajectories(keep=60)` + `--trim-trajectories` flag; school-loop.yml checkpoint now trims → sanitizes → `git add -f data/trajectories`. Layer 2 survives fresh checkouts, capped at newest 60. Real-file tests made corpus-robust (`_require_domain` skip). |
| **U3** — verify gate loudness + toolchain | 0x9.3 OPEN (notes) | **DONE (code, uncommitted)** | R6 built: `VERIFY_GATE_STRICT=1` — `verify_gate._skipped_verdict` escalates can't-run verdicts to `skipped:False + strict_escalated:True`; `issue_bridge._strict_gate_failure` escalates ImportError/exception paths; merge treats `strict_escalated` (even `ran==0`) as a real FAIL. The school-loop execute preflight hard-fails when Nix or `verifyShell` is unavailable; the hosted loop still publishes committed state. Tests cover reusable soft-skip/strict escalation plus workflow hard-fail structure. Close bead after commit. |
| **U4** — FirstMate dispatch | 0x9.4 OPEN (umbrella) | **SPLIT 2026-08-11** | Plan U4 section is now a pointer; claimable sub-beads under epic 0x9: **U7** `crew_dispatch.py` = `school-core-efa` (P1), **U8** bridge `crew_enabled` wiring = `school-core-d4v` (P1, blocked by U7), **U9** surfacing+docs = `school-core-1ae` (P2, blocked by U8). `fm_doctor.sh` preflight wired + committed; crew drill proven standalone; `crew_dispatch.py` still doesn't exist. |
| **U5** — campus.md truth | 0x9.5 OPEN (notes) | **DONE (uncommitted)** | Operational Reality table reconciled 2026-08-11: duplicate verify-gate rows merged; principal verify-gate row distinguishes hard production preflight from reusable soft-skip/strict escalation; Layer 2 row notes git durability; **Layer 3 row added** (⚠️ Partial — gate fires, write-side hook pending); pre-merge row → `src/entire_review.py`; FirstMate row → ⚠️ Partial (spawn proven + preflight live, issue-path dispatch NOT wired = U4). |
| **U6** — Entire in issue path | 0x9.6 OPEN (notes) | **DONE (code, uncommitted)** | R13: `src/qodo_pre_merge.py` → `src/entire_review.py` (`run_entire_review`, `EntireFinding`, output files `entire_review.md`/`entire_findings.json`/`entire_review_summary.md`); conductor + school_mail renamed `qodo_*` → `entire_*`; R12: `_get_entire_path` falls back to `~/.local/bin/entire`/`~/bin/entire`; R11: `issue_bridge._run_entire_sensor` wired into the sync path (non-blocking — surfaced on result + last_run, never overrides verdict). Tests: `tests/test_entire_review.py` (7) + 2 bridge sensor tests. |

### Map's "Not yet specified" — now decided

| Open question | Decision | Built? |
|---|---|---|
| Verify gate policy | Production preflight hard-fails missing infrastructure; reusable gate soft-skips visibly by default; strict is opt-in | Production hard preflight: **yes**. Reusable soft-skip: **yes**. Strict (`VERIFY_GATE_STRICT`): **yes** (R6, uncommitted) |
| FirstMate replace-vs-wrap | Wrap behind `crew_enabled` flag (KTD-4), direct-Orca fallback | **No** (U4) |
| Trajectory size/PII | Checkpoint sanitized, cap last N cycles (KTD-2) | **Yes** (U2) |

### Net remaining

**1.0 unit of real work remains: U4** — now split into three claimable
sub-beads (U7 `crew_dispatch.py` module = `school-core-efa`; U8 bridge
`crew_enabled` wiring = `school-core-d4v`; U9 surfacing+docs =
`school-core-1ae`; U4 `0x9.4` is the umbrella only). **Deepened 2026-08-11**:
U8 adds `provided_student_output` to `run_task` (review lives inside it,
KTD-7); U7 models the five-state status protocol + `data/crew_runs.json`
in-flight registry + next-spawn sweep (KTD-8); U9 adds `concurrency: group`
and `CREW_MAX_PER_CYCLE` for the 5-min-cadence overlap. U1, U2, U3, U5, U6
code halves are DONE (uncommitted working tree); next step is committing the
whole batch and closing beads 0x9.1/0x9.2/0x9.3/0x9.5/0x9.6, then claiming
U7 → U8 → U9 in order.

---

## Refactor frontier (R-series, audit 2026-08-11)

> Third pass. Pipeline-gap units (U1–U9) are functional work; this frontier is
> the **code-health debt** left behind — none of it blocks the issue path, all
> of it taxes the next agent that touches those files. Sized for independent
> claimable beads (R1…). Grounded in current metrics: 1046 tests collected,
> 874 bare `except Exception:` outside tests/, 30/30 CI runs green.

### R1 — board.py embedded HTML/CSS/JS (517 lines, 5 inline blocks)
**Hypothesis:** the live dashboard mixes Python logic with ~40KB of inline CSS/JS
and string-concatenated DOM, making styling changes require editing Python and
creating an XSS-shaped innerHTML pattern (`card-title` + c.t).
**Evidence:** `board.py` = 517 lines with 5 `HTML =`/`_CSS =`/`_JS_` blocks; `_JS_POLL`
builds cards via `html += ... c.t ...` (unsanitized issue titles).
**Verdict:** CONFIRMED — OPEN (R1). Fix = extract to `docs/site/*.css|js` static
assets served by activity_server, escape titles or use textContent.
**Effort:** ~0.5 day. **Risk:** low (visual-only).

### R2 — escalation_log.py global mutable LOG_PATH
**Hypothesis:** `global LOG_PATH` mutated in `__init__` means two instances with
different paths fight; `_load_log` silently returns `[]` on corruption (history loss).
**Evidence:** `escalation_log.py:31` (`global LOG_PATH`); `_load_log` swallows
JSONDecodeError → `[]`.
**Verdict:** CONFIRMED — OPEN (R2). Fix = instance-path attr (no global),
corruption → rename/backup not silent discard.
**Effort:** ~2h. **Risk:** low (single consumer — director/leaf escalation calls).

### R3 — bare `except Exception` sweep (874 non-test)
**Hypothesis:** broad exception handling masks root causes and made the real
failures of 2026-08-11 (dispatch.sh set -e bug, TCC exec block, activity-server
plist) silent for weeks.
**Evidence:** 874 matches outside tests/ across ~40 modules (conductor 25+,
orca_executor 9, director 13, teacher 10). Pattern is deliberate for
resilience-critical loops (bridge, notify) but unlabeled noise elsewhere.
**Verdict:** CONFIRMED — OPEN (R3). Fix = policy: annotate intentional ones
(`# noqa: BLE001 — reason`), narrow or log-with-context the rest; sweep module by
module, highest-noise first (conductor, director). Do NOT blanket-replace (many
are correct degrade-and-continue).
**Effort:** 2-3 days spread. **Risk:** medium (behavior-preserving requires care).

### R4 — stale doc drift (README test count, env contract)
**Hypothesis:** README claims "942 tests" while the suite collects 1046; the
notify env contract now lives in `docs/notification-style-guide.md` but README's
Quick Start doesn't mention it.
**Evidence:** README Live Stats row (942) vs `pytest --collect-only` (1046);
notify env section added 2026-08-11.
**Verdict:** CONFIRMED — OPEN (R4). Fix = one-line count bump + link. 5 minutes.

### R5 — test_orca_execution.py hang (pre-existing)
**Hypothesis:** the full `pytest tests/` run hangs in `test_orca_execution.py`
(live-Orca daemon tests, gated `ORCA_LIVE_TESTS=1`) even when skipped — so a
plain local `pytest tests/` never completes, which hides regressions in the rest
of the suite.
**Evidence:** targeted subsets pass (95-147 tests); full-suite run times out;
skipif gate exists but something in the file still blocks collection/runtime.
**Verdict:** CONFIRMED — OPEN (R5). Fix = find the blocking fixture/import;
ensure skip is collection-safe. **Effort:** ~2h. **Risk:** low.

### Refactor frontier ordering

R4 (doc truth) → R2 (global state) → R1 (board assets) → R5 (suite hang) → R3
(bare-except policy sweep, largest). R1-R4 are small enough to batch behind the
U-batch commit; R3 is its own epic. None blocks U7→U9; all five reduce the
next-agent tax on this codebase.
