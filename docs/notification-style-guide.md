# Notification Style Guide (tone spec)

> Goal of this doc: make every message the Agent-School sends read like it
> came from one careful operator — plain English, a clear "what happened",
> a clear "what to do", and a consistent vocabulary. This is the contract the
> notify layer (`school_mail.py` + `agentmail_client.py`) follows. If a new
> card is added, it must pass the checklist at the bottom.

## 1. Vocabulary (one word per thing)

The system has accumulated three names for the same object. **Stop that.**
New messages must use the canonical terms below; old references may be
migrated over time, but never introduce a new alias.

| Canonical | Meaning | Former aliases (avoid) |
|-----------|---------|------------------------|
| **issue** | A GitHub issue flowing through the school | "task", "bead #…" (in human-facing text) |
| **bead**  | The internal tracker id for a piece of work (used in commands + bookbag) | — (keep for `/fix <bead>`, bookbag) |
| **review** | The two-judge check (CTO/COO) a student's work passes | "verdict", "rubber-stamp" |
| **accepted / rejected** | The human's final decision | "APPROVED / REJECTED" (subject still uses ✅/❌ marks) |
| **retry / school-failed** | A transient failure (will retry) vs. exhausted budget (needs human) | "error", "blown", "needs review" |
| **pre-merge check** | The `entire review` pass | "Entire pre-merge", "entire output" |
| **the school** | The agent system, as a collective actor | "Agent-School", "principal", "bridge" (human-facing) |

Rules:
- Never write **bead** to a human unless a `/fix <bead>` command needs it.
- Never write internal identifiers the human can't act on (`job_id`, `thread_id`,
  `repo`, `commit_sha` in *subjects* — fine in the body as context).
- **Subject format** stays machine-findable: `[school] <short thing> — <STATE>`.

## 2. Card anatomy (every message has 3 beats)

Every notification is built from the same skeleton:

```
[line 1]  Who + what + state          e.g. [Agent-School] Issue #45 — ❌ SCHOOL-FAILED
[beat 1]  Context                     Repo, title, the two verdicts — facts, no spin
[beat 2]  What happened               ONE plain sentence (the ELI5 line)
[beat 3]  What to do                  Next step, or the reply commands
[footer]  Reply path / link
```

### The ELI5 line
The single most important sentence. Written for a smart non-expert.

- Good: `What happened: a temporary failure — the school will try again automatically. No action needed.`
- Bad: `What happened: A2A connection refused; bridge escalated to retry budget state machine.`

Rules:
- Start with `What happened:`.
- One sentence. No acronyms unless defined in the same card.
- Say **who acts next** (you / the school / the system).

### The reply path (footer)
Every actionable card ends with a footer that tells the human exactly how to
respond. Commands on their own lines, in the form the poller parses:

```
Reply with one of:
/approve — accept this work and merge it
/reject — mark it rejected
/fix <note> — send it back with your note
```

- Verdict cards: the three-command footer.
- Issue alerts: the **next step** (`Next step: open the issue (link below)…`).
- CI alerts: the **next step** + run link.

> IMPORTANT: never let a footer line *begin* with a parseable command token
> unless it IS a real command option. The poller skips the known footer lines
> (`_FOOTER_LINES`), but the invariant is: **footer = instructions, never votes.**

## 3. Failure messages

Failures get their own tone: no blame, no jargon, always a next step.

| Situation | What happened (ELI5) | What to do |
|-----------|---------------------|------------|
| Retry (transient) | "a temporary failure … will be retried automatically" | "No action needed" |
| School-failed (budget out) | "the school could not finish this issue after N attempts" | "Open the issue and decide" (link) |
| CI red on main | "the automated checks failed on the main branch" | "Open the failing run" (link) |
| CI + integration job red | add: "this is usually infrastructure (the gateway/Orca on the Mac), not code" | same as CI |

- **Truncate errors** at 500 chars with a hard cut — never mid-token panic.
- **Never say "error" without saying "who fixes it".** The human decides;
  the system retries; both must be stated.

## 4. Logs (not user-facing, but same discipline)

- Every log line carries a **timestamp** (`configure_logging()` in
  `agentmail_client.py`). A 2-minute cron without timestamps is
  indistinguishable from a dead one.
- Use `logging`, not `print`/`sys.stderr.write` — levels, timestamps, module
  names. The notify layer migrated; keep it that way.
- Log the **outcome** (`delivered`, `skipped (no inbox)`, `send failed`) not
  just the attempt.

## 5. Checklist for new cards

1. [ ] Has all 3 beats: context → What happened → What to do.
2. [ ] Vocabulary matches §1 — no new aliases, no exposed internals.
3. [ ] One of the reply footers (or an explicit `Next step:`) is present.
4. [ ] Footer lines can't be misparsed as commands (see §2 warning).
5. [ ] Failure text says who acts next, error ≤500 chars.
6. [ ] Uses the shared client (`agentmail_client`) — no new copy of `_req`/inbox logic.
7. [ ] Uses `logging` with timestamps.
8. [ ] Test asserts the ELI5 line and the footer (see `tests/test_school_mail.py`).

## 6. Env contract (kept here so it doesn't drift)

| Var | Purpose |
|-----|---------|
| `AGENTMAIL_API_KEY` | Required. User-scoped key (`am_us_…`); falls back to `~/.hermes/config.yaml`. |
| `AGENTMAIL_SCHOOL_INBOX` | The Agent-School control-plane inbox (where verdict cards + human replies live). Current: `vault-synthesis@agentmail.to`. |
| `AGENTMAIL_INBOX` | Legacy/override destination inbox (falls back after SCHOOL). |

Resolution order: `AGENTMAIL_SCHOOL_INBOX` → `AGENTMAIL_INBOX` → first inbox
on the key. Set it at every entry point (hermes `.env`, crontab, CI workflow).
