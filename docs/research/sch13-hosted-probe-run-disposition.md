# SCH-13 — First real hosted-probe run: disposition

**Date:** 2026-10-04
**Reviewer:** Phymora (adversarial)
**Branch:** `school/sch-20-seam-hygiene` (unpushed)
**Verdict:** fix-then-ship / blocked. Production student coding stays disabled.

## Intent

Produce the "full qualification evidence" the acceptance requires by running the 8
hosted probes against the operator-approved SmolCloud substrate, and decide whether
production student dispatch may be enabled. It may not.

## What ran

`SCHOOL_CORE_HOSTED_PROBES=1 python3 -m pytest tests/test_smol_cloud_hosted_probes.py`

Result: **3 passed, 5 failed** (P0, P6, P7 pass; P1–P5 fail).

- P0 account preflight — PASS (after the fix below)
- P1 lifecycle export/import/destroy — FAIL
- P2 forbidden egress blocked — FAIL
- P3 host canaries invisible — FAIL
- P4 guest failure / zero host fallback — FAIL
- P5 oversized export refused — FAIL
- P6 provider TTL sweep — PASS (real machine `mach-54bebf44984944c9a100688ff387361f`
  swept after 91s, TTL 60s)
- P7 evidence bounded/sanitized/no provider write — PASS

Real machines were created and deleted; the run left no orphans.

## Finding A (BLOCKER) — the adapter cannot start any machine on this substrate

The adapter requests `network: {"mode": "blocked"}` on create
(`smol_cloud_runner.py:207`) and a registry-pinned image
(`alpine:3.21@sha256:ce64758a…`, the operator-supplied
`SCHOOL_CORE_SMOL_CLOUD_IMAGE`). SmolMachines accepts the create (HTTP 201) but
rejects the **start** (HTTP 400):

```
create failed: smolvm create failed (400 Bad Request):
{"error":"config operation failed: create machine: image
 'alpine:3.21@sha256:ce64758a…' must be pulled from a registry, but this machine
 has no network, so the pull can never succeed. Add --net (or publish a port with
 -p, or set an egress policy with --allow-cidr/--allow-host). To keep the machine
 network-isolated, supply the image locally instead:
 `docker save alpine:3.21@sha256:ce64758a… | smolvm machine create --image -`",
 "code":"BAD_REQUEST"}
```

Root cause, reproduced by hand outside pytest: a registry image is pulled at
**start**, but `network: blocked` denies that pull. The two create parameters are
mutually incompatible. Every P1–P5 probe creates then starts a machine, so all
five fail at the same line (`smol_cloud_runner.py:235`, the `/start` call).

This is a **design contradiction, not a credential problem**. The 52 hermetic
adapter tests use a fake transport and cannot see it; the first real run caught it.

### Provider API surface (verified by probing)

- `source.type` ∈ `image` | `smolmachine` (the latter is a tenant-preloaded image;
  the provider error above is how it is populated).
- `network.mode` ∈ `blocked` | `open` | `allowCidrs` (not `enabled`).
- `allowCidrs` requires at least one CIDR/host.

## Finding B (MAJOR) — the adapter hides the provider's error body

`_json_request` raises `_CloudApiStatusError` carrying only the status and path
(`smol_cloud_runner.py:486`); the response body — which contained the entire
explanation above — is discarded. Every hosted failure is opaque without a manual
curl. The transport journal records only `request_bytes`/`response_bytes`
(`test_smol_cloud_hosted_probes.py:100-110`), so no run artifact captured the 400
reason either. Add a bounded, sanitized error-body excerpt to the exception.

## Finding C (MAJOR) — P0 asked the operator to accept a stop that does not exist

Interaction `640135be` told the operator:

> "The $5 hard stop is already coded into the adapter (`HARD_STOP_MICROS =
> 5_000_000` in `smol_cloud_runner.py`)."

**False.** `HARD_STOP_MICROS` exists only in the probe test
(`tests/test_smol_cloud_hosted_probes.py:54`), never in `smol_cloud_runner.py`.
The adapter has **no spend bound at all**: `totalMicros` is only type/negativity
checked (`smol_cloud_runner.py:560-562`), never compared to a ceiling; there is no
cumulative-spend accumulator. The runner is not wired into dispatch, so nothing
else enforces a cap either. Accepting "the self-imposed stop" would have accepted
nothing. Interaction withdrawn.

## Finding D (MAJOR) — P0 read the wrong field and was unpassable on a funded account

The probe gated on `budgetRemainingMicros`, which this account returns as `null`
(budget policy is `soft`). But the account **does** expose the real spend surface:

- `prepaidCreditMicros` = **99,112,236** (~$99.11)
- `lowBalanceThresholdMicros` = 5,000,000
- `periodCost.totalMicros` = 891,665 (period to date)
- `plan.freeCreditMicros` = 100,000,000

None of these fields were in the probe's `_DETAIL_KEYS` allowlist. The gate failed
on a funded account. Fixed: P0 now records the spend surface and falls back to
`prepaidCreditMicros` when `budgetRemainingMicros` is absent (fail-closed only when
neither is observable). Re-run: **P0 passes**, evidence written to
`data/hosted-qualification/`.

## What is genuinely proven (ran this heartbeat)

- Adapter hermetic suite: **52 passed** on py3.9.6.
- P0: account reachable (HTTP 200), spend surface observable, headroom ≫ $5.
- P6: a created machine is swept by provider TTL (real id, 91s).
- P7: evidence file bounded (≤256 KiB), token-free, no provider write.
- Fail-closed lifecycle, no-host-fallback, relay revocation, no-net/no-secret
  create, bounded I/O, artifact/identity, quarantine — proven **hermetically only**.
- No orphan machines from this run.

## What is NOT proven

- Lifecycle create→start→run→export→verify→destroy on the real substrate (P1).
- Forbidden-egress denial inside a real guest (P2).
- Host file/credential isolation inside a real guest (P3).
- Oversized-export refusal in a real guest (P5).
- Any spend ceiling — the adapter has none.

Production student coding stays disabled; the acceptance is not met.

## Decision needed (operator)

The adapter's create config must change to make the substrate usable. Options:

1. **Preload the image as a tenant `smolmachine`** (operator runs
   `docker save <image> | smolvm machine create --image -`), then the adapter uses
   `source: {type: "smolmachine", …}` and keeps `network: {mode: "blocked"}`.
   Strongest isolation — the guest truly has no network. Requires an adapter change
   to accept a `smolmachine` reference and to drop the registry-digest requirement
   for that path.
2. **Allow registry egress for the pull** (`network: {mode: "allowCidrs", …}`).
   Weaker: the guest then has network, and network-level task-egress denial is lost
   (it would have to move to exec-level). Not recommended.
3. **Defer** — leave production student coding disabled; SCH-13 stays blocked.

Recommended: **1**. It is the only option that preserves the isolation invariant
the whole seam exists to provide.

## Not claimed

No provider/GitHub write; branch not pushed; no PR; production dispatch unchanged
(disabled). The operator credential delivery was successful and is not the blocker.
