# SCH-9 — Adversarial review of the golden-path premises

Author: phymora (adversarial review specialist). Every claim below was produced by
running the artifact, not by reading it. Repo: `/Users/brandonbennett/school-core`,
`origin/main` 455c9ae (working tree dirty), target repo `branben/sound-royale-ny`.

## Intent (restated)

The proposition under test: *the loop runs green but processes no real issues, and
four named premises explain why.* I was asked to attack all four and name the
file:line that would prove each refutation.

## Headline

**The premise list is materially wrong in two places and dangerously understated in
one.** The upstream cause is not `school-failed` — it is that **the workflow's
execution step has been failing hard since 2026-09-25**, and separately that
**`processed_issues.json` is a terminal burn list with no inverse**, which makes
`school-failed` a permanent stamp rather than a retryable state.

Empirical grounding:

| Fact | Evidence |
|---|---|
| 20 of the last 30 School Loop runs failed | `gh run list --workflow "School Loop" --limit 30` |
| Every recent failure dies at step 5 "Register checkout with Orca" | run 36793992510, job 110153119884 |
| Error: `{"code":"runtime_unavailable"}` from `orca repo add` | job log, `.github/workflows/school-loop.yml:355-362` |
| Gate steps 6-11 skipped, incl. "Run bridge loop (executes issues)" | run 36793992510 job list |
| Last green run printed `No new issues to bridge.` | run 36625071223, job 109599887117, log line 426 |
| `data/last_run.json` frozen at 2026-08-31 in the checkout | `git log -1 -- data/last_run.json` = 991c31a, 2026-08-30 |
| Board state newest entry = 2026-09-24T10:39:31Z (#339 school-failed) | `git show origin/board-publish:data/last_run.json` |
| Newest eligible work item: #339, `ready-for-agent`, unprocessed | `data/processed_issues.json` (339 absent) |

## Premise (a) — "school-failed is the blocker" — **REFUTED (last symptom, not cause)**

Two independent disproofs.

1. `school-failed` is not what stops dispatch. The drop happens at classification:
   `github_fetcher.py:185` (`if state != "ready-for-agent": continue`) and the
   processed gate at `issue_bridge.py:1342` (`if num in processed: continue`).
   The label itself is never consulted anywhere on the fetch path.
2. On the live target repo, **no open issue carries both `bug` and `enhancement`**
   (`gh issue list --repo branben/sound-royale-ny --limit 100`), so the config
   filter at `config/github.yaml:11` (`labels: ["bug","enhancement"]`) matches
   **zero** issues. Even with `school-failed` erased, the config-driven path
   (`bridge_poll` → `issue_bridge.py:2418`) fetches nothing.

**What actually blocks it, in order:** (1) the Orca registration step failing the
whole execute job since 2026-09-25; (2) `processed_issues.json` burning issues on
infra failures; (3) `school-failed`; (4) the zero-match config filter.

## Premise (b) — "a real pass clears the stamp" — **REFUTED, and the wedge is worse than stated**

`school-failed` is terminal. `issue_bridge.py:579` adds the label and **no code
path anywhere removes it** — the only remover in the tree is
`scripts/school_inbound.py:102-106`, which is a legacy inbound handler not wired to
the loop (`grep -rn school_inbound` finds only its own docstring).

Worse: the burn is permanent and independent of the label.
`issue_bridge.py:1342` skips any number in `processed_issues.json`, and that file
only ever grows (`_save_processed` at `:181`, `processed.add` at `:1679/:1805/:2275/:2394`).
Live state: #339 absent, but **#340, #341, #342, #415, #419 are all present** —
i.e. `ready-for-agent`, still `OPEN` on GitHub, and permanently unprocessable.

The retry-budget guard at `issue_bridge.py:2387-2399` only protects
`status not in ("done","error")`. A crew that *did* run and returned `error` takes
the `processed.add` branch. Infra failures that surface as a completed-but-failed
task burn the issue.

**Decisive timeline — #342 is the proof (the previous claim in the plan is incomplete):**

```
2026-08-30T17:50:25Z  labeled school-done
2026-08-30T17:50:27Z  closed          <-- a real success DID clear the stamp
2026-08-30T18:56:34Z  unlabeled school-done / unlabeled school-failed
2026-08-30T18:56:42Z  reopened
2026-08-31T02:11:42Z  labeled school-failed   <-- reprocessed anyway, and failed again
```

A genuine `success` (score 66.5, trajectory exists on disk) fired the full
`_mark_github_issue(repo, num, "success")` path at `issue_bridge.py:2262` — label
added, issue closed. It was **reopened and re-failed within 5 hours**. Note the
`unlabeled school-done / unlabeled school-failed` pair *before* the reopen: both
labels were being stripped, which no bridge code does. That is consistent with the
verify gate having overwritten the student's work in the shared checkout and a
human/CI repair reverting it — not proven, flagged as the one unexplained seam.

So the plan's framing is wrong in both directions: `school-failed` is not why
issues stop, and a real pass does not keep the issue fixed.

## Premise (c) — "20 needs-triage issues are invisible; the LABEL FILTER is the bug" — **MOSTLY REFUTED**

Correct in count, wrong in mechanism. Live classification of `branben/sound-royale-ny`
via `triage_classifier.classify_issue`: **13 `ready-for-agent`, 11 `needs-triage`**.
The 11 are dropped at `github_fetcher.py:185`, not by the label filter.

More importantly, the three issues the plan implies would be rescued are **genuine
product work**, not classifier noise: #137 "E5: Re-verify full CI green",
#134 "E2: Wire Django+Redis backend into E2E Full Suite", #136 "E4: Stand up deploy
target". Widening the filter feeds these into an unattended loop.

The real filter defect is the opposite of the stated one: `config/github.yaml:11`
is an **AND** match. `gh issue list --label bug --label enhancement` returns issues
carrying *both* (verified against `cli/cli`: 1 result vs 5 for one label), and
`github_fetcher.py:158-160` appends one `--label` per entry. It yields **0** on the
live repo — but only the config path (`bridge_poll`, `:2418`). The workflow calls
`--once` (`school-loop.yml:453`), which passes `labels=None` (`issue_bridge.py:2463-2465`)
and so is unaffected. The filter is a live landmine, not today's cause.

## Premise (d) — "swift needs verifyShell; right fix is a per-repo hermetic flake" — **REFUTED**

`sound-royale-ny` contains **zero** `.swift` files and no `Package.swift`
(`gh api repos/branben/sound-royale-ny/git/trees/main?recursive=1`). There is no
Swift work item in the golden-path target repo. `swift-sdk` is a separate Paperclip
project (`5f6758ae-...`) and `SCH-7` is its blocker.

`SCH-7`'s own description already answers this: a per-repo flake is **VIABLE** and
swift-sdk is **VERSION-CAPPED** (floor is a chain: root 6.1, alt manifest 6.0,
`mattt/eventsource` 1.1.0, all above 5.10.1). Adding Swift to school-core's
`verifyShell` (`flake.nix:28-47`) would buy nothing for the golden path and would
hand every student a toolchain no target repo uses.

Additional defect: the shared flake is load-bearing but **is not** shared.
`issue_bridge.py:824` and `director.py:531` both pass
`flake_path=Path(__file__).resolve().parent` — school-core's own root. Verified
students run in the target repo, so every target repo is gated by school-core's
`flake.nix`. `project_verify.yaml:1-5` documents this as a hard constraint.
The gate is structurally unable to verify Swift regardless of what the flake exposes.

## The simpler alternative (mandatory pass)

There is no simpler architecture. The build is ~180 lines across two files. The
smaller change is to **delete premises (a), (c), and (d) from the work queue** and
fix three concrete defects:

1. **Stop the burn.** `processed_issues.json` must be an inverse-aware ledger keyed
   on `(issue, outcome_class)`, where only `PASS` and `REJECT` (quality verdicts)
   are terminal and `INFRA` is retryable. Fixes `issue_bridge.py:1342` + `:2394`.
2. **Stop the hard fail on Orca.** `.github/workflows/school-loop.yml:355-362` uses
   `set -euo pipefail` with no recovery when `orca repo add` returns
   `runtime_unavailable`; the `orca status` guard does not catch it because status
   *succeeds* while repo add cannot connect. Either repair the actual precondition
   or emit `BLOCKED_ENV` — but do not skip the bridge loop silently for 6 days.
3. **Make the config filter a no-op.** `config/github.yaml:11` — empty the label
   list, or OR the labels at `github_fetcher.py:158-160`.

Do **not** widen the classifier to `needs-triage`. Do **not** add Swift.

## Verdict

**rework.** Biggest reason: the plan is hunting a labeling bug while the pipeline
has been **hard-failing upstream of the bridge since 2026-09-25** — and the
`processed_issues.json` burn list means the five issues that *are* correctly
labeled and eligible can never be retried. Premises (a) and (d) are wrong,
(c) is wrong about the mechanism, and (b) is right for the wrong reason and
understates the damage.

## Corroborated (verified myself, not taken on trust)

- All 24 historical `success` entries predate 2026-08-30; 14 point at trajectories
  that do not exist and 10 carry a null path. The only success with a live
  trajectory is #342 (2026-08-30). The fixture-success claim holds.
- The loop's hard failure is recent: 10/30 green including 2026-09-25..29, then
  20/30 failed from 2026-09-30. `SCH-4`'s premise ("green run that did nothing")
  is true for the 2026-09-25..29 window only; today it fails loud, just not
  informatively.
- Test linkage: `tests/test_github_fetcher.py` + `tests/test_issue_bridge.py`
  = 131 passed in 76.6s (`/usr/bin/python3 -m pytest`). No test asserts that a
  processed issue can be re-admitted, and no test asserts that an infra `error`
  must not burn the retry budget. That is why this shipped.

## Unproven — I do not have evidence

- Who or what reopened #342 and stripped both school-* labels at 2026-08-30T18:56:34Z.
  It is not bridge code. A verify-gate overwrite plus a repair revert is consistent
  with the timeline but I did not prove it.
- Whether `orca status` returning 0 while `orca repo add` fails is an Orca-side
  regression or a token/port state issue. I verified the *symptom*, not the cause.
