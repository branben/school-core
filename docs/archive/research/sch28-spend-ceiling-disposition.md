# SCH-28 — Hosted adapter spend ceiling: disposition

**Date:** 2026-10-04
**Reviewer/author:** phymora (adversarial)
**Branch:** `school/sch-20-seam-hygiene` (unpushed)
**Verdict:** fix-then-ship → the ceiling is real and proven against the live API.

## Intent

Make the adapter actually enforce a spend stop. Interaction `640135be` told the
operator the `$5` stop was "already coded into the adapter"; it was not — the
constant existed only in the probe test. Close that gap with an enforced bound.

## What was false (confirmation)

`HARD_STOP_MICROS` existed only at `tests/test_smol_cloud_hosted_probes.py:54`.
`smol_cloud_runner.py` had **no** spend bound: `_delete` type/negativity-checked
`totalMicros` and discarded it; there was no accumulator and no comparison to any
ceiling. The runner is not wired into dispatch, so nothing else capped spend.

## What the ticket got right, and the deeper bug it did not know about

The ticket proposed: *accumulate settled `totalMicros` from each delete's
`includeUsage=true` response*. Directionally correct, but **the field path was
wrong in a way that would have shipped another dead guard.**

Verified against the live API (bounded, real machine, deleted):

```
DELETE /v1/machines/<id>?includeUsage=true  ->  HTTP 200
{"machineId":"mach-…","from":"…","to":"…",
 "usage":{"totalUptimeSeconds":5,"cpuHours":0.0166…,"machineCount":1,…},
 "cost":{"cpuMicros":833,"memoryMicros":31,"baseMicros":56,
         "totalMicros":920,"amountDueMicros":920,…}}
```

The settled amount is **nested under `cost`**, not top-level. The adapter read
`usage.get("totalMicros")` (top level), which the live API never returns — so a
guard built on the ticket's literal wording would have read `None` forever and
enforced nothing, exactly like the constant it was replacing.

Two more verified facts:

- `cost.totalMicros` is **per-machine settled cost for that machine**, not a
  cumulative window total. Proven with two machines: a ~60s machine settled
  **921** micros; a ~0.5s machine settled **185** micros (nested `from` was the
  billing-period start in both, i.e. not a running window sum). Accumulation is
  therefore sound.
- `usage.machineCount` is `1` on a single-machine delete; a stale earlier probe
  had recorded `2` and was not a reliable discriminator.

## Change (minimal, fail-closed)

`smol_cloud_runner.py`:

- `DEFAULT_MAX_SPEND_MICROS = 5_000_000` (module constant) — now the single
  source of truth for the stop.
- `SmolCloudRunner(..., max_spend_micros=DEFAULT_MAX_SPEND_MICROS)` — validated
  as a positive integer alongside the existing ceilings.
- `self._settled_spend_micros = 0` — cumulative settled cost for this instance.
- `_execute` refuses **before the credential read and before any create** when
  `_settled_spend_micros >= max_spend_micros`. Fail-closed with a
  `StudentVMBlocked("spend ceiling reached: …")`.
- `_delete` now returns the settled micros (`int | None`), reading
  `cost.totalMicros` (falling back to a top-level `totalMicros` for flattened
  deployments). Absent amount → `None` (never fabricate spend); present-but-
  invalid → hard failure (unchanged fail-closed behavior). The teardown path
  adds the returned amount to the accumulator.

`tests/test_smol_cloud_hosted_probes.py`: `HARD_STOP_MICROS` is now
`DEFAULT_MAX_SPEND_MICROS` imported from the adapter (single source of truth),
not a redeclared constant.

## Honest limitation (not a claim of a hard stop)

This is an **accumulator, not a projection**. Settled cost is only known after a
machine is deleted, so the ceiling is enforced **between runs**: a runner that
reuses one instance across tasks refuses to start the task that would exceed the
cap. It cannot pre-price a single task before it runs. A truly hard *session*
stop needs provider-side account spend limits (the account's
`lowBalanceThresholdMicros=5,000,000` is an alert, not a stop). What is fixed is
the false claim that the adapter bounded spend at all.

## Evidence

Hermetic (`tests/test_smol_cloud_runner.py`): **66 passed**, including 7 new
ceiling tests — nested-field read, refuse-after-cap (asserts exactly one create),
no-request-when-already-over-cap, absent-cost does not accumulate or wedge,
legacy flat field accepted, constructor validation, invalid nested cost rejected.

Seam suite (adapter + local seam + verifier + probes, probes skipped):
**120 passed, 16 skipped**.

Live (real billable machines, deleted, no orphans):

```
RUN1 ok exit 0 guest sc-sch28-e2e-1-f81e01e8
SETTLED_MICROS 908 CAP 1
RUN2 BLOCKED: spend ceiling reached: settled 908 micros >= cap 1 micros;
             refusing to create another machine
CREATES_TOTAL 1  CREATES_ON_RUN2 0  DELETES 1
```

Run 2 issued **zero** creates — the guard is before the billable call.

P0 live (no machine): **1 passed** with the imported constant.

## Incidental fix

`tests/test_smol_cloud_runner.py::test_create_payload_pins_blocked_network_ttl_ephemeral_and_resources`
was already failing on the working tree: the uncommitted `source_type` default
flip (`image` → `smolmachine`) left its `{"type": "image"}` assertion stale. The
test now passes `source_type="image"` explicitly so it still pins the payload it
is named for. Not caused by this change; fixed so the suite is green.

## Not claimed

No provider/GitHub write beyond the bounded probe machines (all deleted); branch
not pushed; no PR; dispatch still not wired, production student coding unchanged
(disabled). SCH-29 (error body discarded) and SCH-27 (bundle_sha256 write-only)
remain open.
