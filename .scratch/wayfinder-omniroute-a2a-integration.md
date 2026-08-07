# Wayfinder — OmniRoute A2A Integration

> **Destination:** Decide whether Agent-School Core should route student model
> calls through OmniRoute's A2A `smart-routing` skill (returns routing_explanation,
> cost_envelope, policy_verdict, resilience_trace) instead of the bare
> `/v1/chat/completions` endpoint currently used by `director.py`. If yes,
> wire the richer metadata into school-core's scoring + observability layer
> (closes the F3 "no metrics, no heartbeat" gap from the Path-A map).

## Notes

- Repo: `~/school-core` (worktree root for teacher-coo live at
  `~/orca/workspaces/school-core/teacher-{cto,coo}`).
- OmniRoute instance: `http://localhost:20128`, A2A endpoint: `POST /a2a` (JSON-RPC),
  auth: `Authorization: Bearer $OMNIROUTE_API_KEY` from `~/.omniroute/.env`.
- 6 A2A skills live-verified: `smart-routing`, `quota-management`,
  `provider-discovery`, `cost-analysis`, `health-report`, `list-capabilities`.
- school-core currently uses only `/v1/chat/completions` (dumb router). A2A
  returns structured metadata that scoring/observability currently lack.
- See also: existing school-core wayfinder map at `.scratch/wayfinder-map.md`
  (Path-A architecture; F3 = observability gap).

## Not yet specified

- (Empty — frontier will be refilled as investigation proceeds.)

## Out of scope

- Replacing OmniRoute as the model director.
- Adding local LLM inference (user removed Foundry Local — hardware constraint).
- Wiring AgentMail into the A2A loop (AgentMail memory covers the human-notify rail
  separately; this ticket is strictly about the model-call metadata path).

---

## Decision Tickets

### Ticket 1 — F1: Is A2A `smart-routing` metadata actionable for school-core?

**Type:** `research`

**Question:**
`director.py` calls OmniRoute via `POST /v1/chat/completions` (bare OpenAI compat).
Switching to A2A `message/send` with `skill: smart-routing` returns a `metadata`
block containing `routing_explanation`, `cost_envelope`, `policy_verdict`, and
`resilience_trace`. Does any existing school-core consumer (scoring,
conductor, observability) need or want those fields? If yes → justify a migration
ticket. If no → the A2A layer adds complexity with no downstream consumer.

**Evidence files (read-only):**
- `~/school-core/director.py` — the `run_task` function where
  the chat completion call happens; grep for `chat/completions` or `openai`.
- `~/school-core/scoring.py` — does it ever read `cost_envelope`,
  `policy_verdict`, or `routing_explanation`?
- `~/school-core/conductor.py` — any place that would log or
  gate on provider health / budget? Search `health|budget|cost|policy|verdict`.

**Read-only queries:**
1. `grep -n "chat/completions\|openai\|OpenAI\|client.chat" director.py` (find the call site)
2. `grep -rn "cost_envelope\|policy_verdict\|routing_explanation\|resilience_trace" scoring.py conductor.py director.py` (is any consumer present?)
3. `grep -n "last_tick\|heartbeat\|metrics\|health" conductor.py` (confirm F3 gap — already asserted in the Path-A wayfinder but verify)

**Acceptance:** A one-paragraph verdict — either "metadata is consumed nowhere, keep dumb router" or
"X fields would be actionable, justify a migration ticket" — with the exact grep
hits cited.

---

## F1 Resolution

**Verdict: YES — but the migration is NOT a simple switch. The A2A surface exists
but is half-wired. The `smart-routing` skill's metadata IS actionable for the
F3 observability gap, but it requires extending `executor.py`, not replacing
`director.py`.**

### What I found on disk (verified)

1. **Two transport paths already coexist in `executor.py`:**
   - `_omniroute_call()` (line 78) → hits `http://localhost:20128/v1/chat/completions`
     (bare OpenAI compat). This is what `call_model()` (line 266) uses for ALL
     default roles (`coder`, `searcher`, `executor`, `reviewer`, `browser`).
   - `_a2a_call()` (line 111) → hits `http://localhost:20128/a2a` (JSON-RPC). This is
     ONLY used for the `openhands` / `a2a-antigravity` fallback (`COMBO_MAP` lines
     26-28).

2. **The A2A call is fire-and-forget text — it discards metadata:**
   - `_a2a_call` sends `message/send` to the **generic `/a2a` endpoint** with NO
     `skill` parameter in `params` (line 128-136). I tested live: OmniRoute's
     agent card exposes 6 named skills (`smart-routing`, `cost-analysis`,
     `health-report`, etc.) — but school-core never selects one. It hits the
     default dispatch, which is why the `routing_explanation`, `cost_envelope`,
     `policy_verdict` fields I saw in my live test are **never received by any
     school-core consumer**.
   - The function returns only `"\n\n".join(texts)` (line 187) — the `metadata`
     block (containing `routing_explanation`, `cost_envelope`, etc.) is in
     `result.get("metadata", {})` (line 162) but **only surfaced in error messages**
     (lines 172, 183). The success path throws it away.

3. **No consumer exists in `scoring.py` or `conductor.py`:**
   - `grep -rn "cost_envelope\|policy_verdict\|routing_explanation\|resilience_trace"`
     across all three files → **0 matches**. The metadata fields have zero
     downstream consumer. Scoring (`scoring.py`) only reads `task_score`,
     `response`, `error` — never provider/routing metadata.

4. **The F3 gap is real (observability):**
   - `grep -n "last_tick\|heartbeat\|metrics\|health" conductor.py` → only
     `Optional` (typing import), one skip message, and docstring prose. No actual
     heartbeat, no metrics emission, no provider health check.
   - The A2A `smart-routing` skill returns `routing_explanation` + `policy_verdict`
     on every call — this would directly feed an observability log.

### The path forward (one migration ticket, NOT a replace-all)

The right move is **not** to migrate `director.py` — `call_model()` already
abstracts the transport and `executor.py` already has both paths. The ticket is:

**F2 (task):** Extend `call_model()` to optionally capture A2A `smart-routing`
metadata (routing explanation + cost envelope + policy verdict) and return it
in the result dict alongside `response`. Then have `scoring.py`'s
`ExecutionScorer` read `cost_envelope.actual` and `policy_verdict.allowed`
as additional scoring signals.

This is:
- **1 file to extend** (`executor.py`) + **1 file to consume** (`scoring.py`)
- No change to `director.py` (which calls `call_model(role, prompt, ...)` and
  unpacks `.get("choices")` — actually `call_model` returns raw text, so the
  return signature itself needs a small bump)
- Directly closes F3 from the Path-A wayfinder map

**Do NOT** do this as a blanket "switch everything to A2A" — the bare
`/v1/chat/completions` call is faster for simple text generation. The metadata
is only valuable when you want to *learn* from the routing decision (cost-aware
scoring, provider health gating). Wire it as an opt-in flag on `call_model`.
