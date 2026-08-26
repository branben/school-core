---
name: school-core-workflow
description: Use when working on school-core (crew, beads, issues, PR, adversarial, SEA…
triggers:
  - school-core
  - crew
  - bead
  - issue
  - pr
  - adversarial
  - seam
---

# School-Core Workflow

Durable workflow convention for all agents touching the `school-core` repo.
10 rules the room converged on after the PR-67 tree-divergence failure.

## Standing Workflow Loop

```
Dispatcher names checkout + authoritative surface
  → Worker reports diff + ref
    → Second-source reads that ref
      → Convergence is signal
      → Divergence is where the bug lives
```

## The 10 Rules

1. **Name the checkout before the claim.** Lead every verification with "I read `<path>` at `<commit>`" or "canonical working tree." No anonymous "verified in tree."

2. **Verify against the authoritative shared surface**, not the worker's self-report. Second-source reads the ref the worker named. For school-core: `origin/main` + `origin/board-publish`. Local worktrees don't count until they land on a shared ref.

3. **Fix one class at a time.** Each bead = one failure mode. Don't bundle. Each gets its own bead, its own verification, its own commit.

4. **Negative assertions need a named surface more than positives.** "This line doesn't exist" and "this file is 223 lines" were the false-block class from an unnamed checkout.

5. **The dispatcher names the checkout too.** Every agent making a claim states the surface — not just the worker.

6. **Commit the uncommitted diff before layering more on top.** Working trees become stacks of unverified local edits. Get it onto `origin/main` first, then start the next bead from a clean shared state.

7. **Landed fix updates the vault note with the commit SHA** before the next dispatch. Otherwise the vault becomes a stale surface — same class as the PR-67 tree, but for documentation.

8. **Second-source reads only a shared ref.** If the worker's diff is local-only, status is **"deferred until push."** Verify the artifact independently (`git cat-file -t` / `ls` on the named ref). Self-reports don't count.

9. **Fail-open gates are worse than failing ones.** First question on any new gate: "what happens on parse/check failure?" This is exactly why SEAM enforcement stays HELD, not bundled.

10. **Distinguish "not running" from "not working"** before declaring a subsystem dead. A stopped daemon and a broken integration look identical from a failed command.

## Crew-Silence Root-Cause Stack (learned 2026-08-24)

A silent crew (`silent_agent` / no `.status` file) has FOUR possible layers, in order of how deep they sit. Fix top-down, but **VERIFY each with a real probe before concluding** — the room burned 12 hours because three config fixes were "necessary but not sufficient" and the real cause was the deepest layer.

1. **Key/credential provider mismatch** — `crew_dispatch.py` injected `OPENROUTER_API_KEY` while the system uses OmniRoute. Symptom: resolver returns `""`, export prefix never added. Fix: rename resolver + inject `OMNIROUTE_API_KEY` (commit `0ec61c3`).
2. **Wrong/broken model in the crew's Hermes profile** — profile `config.yaml` pointed at `tencent/hy3:free` (400 "Unknown model") or a Nous model with no Nous credential (401). Fix: repoint profile to `auto/best-coding` @ `http://localhost:20128/v1`. (All 17 profiles needed this; crews dispatch different profiles per task-role, not just `student-coder`.)
3. **Wrapper never passed `-p $PROFILE`** — `scripts/hermes-fm-wrapper` ran Hermes with no `--profile`, so crews fell back to the *default* global profile, not the bridge-selected one. Fix: add `-p "$PROFILE"` (commit `b743781`). **File-target trap:** `crew_dispatch.py` prefers the REPO `scripts/hermes-fm-wrapper` over `~/.local/bin/hermes-fm-wrapper` — editing the latter is a no-op.
4. **Provider-layer response corruption (deepest, the real root cause this session)** — OmniRoute's `stream:true` path returns SSE deltas with random alphanumeric junk spliced between tokens (`STATUSn8Xd9ZoU3z_OK`), so the crew's terminal `done:` line arrives shredded and FirstMate's verb-parser sees no match → `silent_agent`. `stream:false` is ALWAYS clean. The corruption is **routing-dependent**: `auto/best-coding` is an alias resolving to a different upstream per request (gpt-4o vs gemini-3.6-flash); some routes corrupt, some are empty, some clean. Pinning to a "reliable" alias is NOT a safe fix — it still routes to a corrupting backend sometimes. **Verified-safe fix: force `stream:false` for crew calls** (the lever lives in Hermes's `auto` provider request shape, not `executor.py`).

### Verify, don't assume (the discipline that caught layer 4)
- **A/B the gateway directly:** `curl .../v1/chat/completions` with `"stream":true` vs `"stream":false`, same model+key+prompt. Reconstruct streamed deltas in Python and check for inter-token noise. Recipe: `references/omniroute-streaming-debug.md`.
- **Alias ≠ model.** Sampling through `auto/best-coding` measures one backend per request; do NOT generalize a gateway-wide claim from alias samples. Record the `upstream=` field per run — a clean sample and a corrupt sample may have hit different routes.
- **`curl -m` makes the prober the failure.** `http=000` is a client-side abort, not a server response. Set the ceiling above worst-case cold start (OmniRoute cold start is 11–20s, variable). Four agents independently misread `000` as "gateway wedged."
- **Manually instrument one crew** to see its actual last words: set `FM_AGENT_*` + `FM_STATUS_FILE` + `FM_HOME` env, run `scripts/hermes-fm-wrapper "$(cat brief.md)"` capturing stdout. Recipe: `references/crew-manual-spawn.md`. This proved the harness/pipeline works and isolated the corruption to streamed text.
- **Confirm via two independent agents before a load-bearing root-cause claim enters the vault.** One agent's A/B is a hypothesis; a second agent reproducing it (as happened here) is evidence. The `obfuscation`-field direction (injection vs stripping) stayed UNPROVEN and was explicitly not filed.

### Support files (this skill's `references/`)
- `references/omniroute-streaming-debug.md` — the A/B curl + SSE-reconstruction recipe that proves layer-4 corruption and the `stream:false` fix.
- `references/crew-manual-spawn.md` — the instrumented `hermes-fm-wrapper` spawn that captures the crew's stdout to see its last words.

### The false-premise trap
"Firstmate has no Hermes harness adapter → upstream a harness PR" was a FALSE premise. A registered harness would watch the same corrupted stream. Gate any upstream-PR claim behind a clean post-fix cycle where a *fully-configured* crew still fails — and even then verify the provider layer first. The harness-PR premise was killed twice: first by contamination argument (config defects), then by evidence (the corruption is provider-layer, not spawn-contract).

## Verification Checklist

Before reporting any fix as "done":
- [ ] Which checkout did you read? (path + commit or "canonical working tree")
- [ ] Which ref did you write to? (`origin/main`, `origin/board-publish`)
- [ ] Is the diff committed and pushed, or local-only?
- [ ] Has the second-source read the named ref and confirmed?
- [ ] Has the vault note been updated with the commit SHA?
