# SCH-13 — hosted qualification disposition: 8/8 probes pass

**Verdict: fix-then-ship (code) → hosted qualification PASSES.** Production student
coding stays disabled pending a separate enable decision (see "Remaining").

**Wake:** `issue_blockers_resolved` was a stale descriptor wake (linked blocker
SCH-20 done). The real unblock was interaction `cc1dd015` (operator, resolved
2026-10-04T14:22:24Z): **image_delivery = `preload_smolmachine`** — preload the
guest image as a tenant `.smolmachine`, keep `network: blocked`.

---

## Root cause, corrected by experiment

The prior disposition attributed the P1–P5 start failures to
"registry image pulled at start vs blocked network". That is directionally right
but **the wrong discriminator**. Cross-matrix runs against the real provider
(bounded, ephemeral, deleted):

| ref | type | START |
|---|---|---|
| `alpine:3.21@sha256:ce64758a…` (docker.io) | image | **400** — "must be pulled from a registry, no network" |
| `alpine:3.21@sha256:ce64758a…` | smolmachine | **400** — "invalid registry reference" (rewritten under tenant ns) |
| `registry.smolmachines.com/library/alpine@sha256:3aae895c…` | image | **200** |
| `registry.smolmachines.com/library/alpine:3.21` (tag) | image | **400** — "must be pulled… no network" |
| `registry.smolmachines.com/library/alpine@sha256:3aae895c…` | smolmachine | **200** |
| `registry.smolmachines.com/library/ubuntu:latest` | smolmachine | **200** |
| `registry.smolmachines.com/library/python:3.12-alpine` | smolmachine | **404** — blob not in registry |

The real variable is the **artifact delivery type**: a provider-hosted
`.smolmachine` is resolved by the node without a guest pull; an OCI `image` is
pulled **inside the guest at start**, which blocked egress denies. Image-vs-type
was conflated in the prior note.

**Digest pinning is preserved.** Trial A proves a `smolmachine` source accepts a
`@sha256:` reference and boots. The operator's option text ("relax the
registry-digest requirement for that path") is **not** needed — the digest pin
stays.

**Second correction — the operator's pinned image cannot work on this path.**
`.env` had `SCHOOL_CORE_SMOL_CLOUD_IMAGE=alpine:3.21@sha256:ce64758a…`, a
docker.io reference. On the `smolmachine` path the provider rewrites a bare
reference under the tenant namespace and rejects it (`invalid registry
reference`). The provider-hosted namespace is `registry.smolmachines.com/library/*`
(the registry advertises `repository:library/alpine:pull` and serves
`library/alpine`). `.env` is now updated to the verified digest-pinned reference:

```
SCHOOL_CORE_SMOL_CLOUD_IMAGE=registry.smolmachines.com/library/alpine@sha256:3aae895c8728baa26ceea6939339a84bb83d6f041a019a7a6f48038f6280365d
SCHOOL_CORE_SMOL_CLOUD_SOURCE_TYPE=smolmachine
```

## Change

Minimal, fail-closed, isolation invariant intact:

- `smol_cloud_runner.py` — new `source_type: str = "image"` constructor knob,
  validated against `{image, smolmachine}`; `create_payload["source"]["type"]`
  now uses it. `network: {mode: blocked}` is unchanged and still not
  caller-overridable. Digest-pin requirement unchanged for both types.
- `tests/test_smol_cloud_runner.py` — +7 hermetic tests (source_type validation;
  `smolmachine` create payload keeps `network:blocked`). **59 passed**.
- `tests/test_smol_cloud_hosted_probes.py` — probe fixture/runner use
  `SCHOOL_CORE_SMOL_CLOUD_SOURCE_TYPE` (default `smolmachine`); P6 uses it too.
  Fixed a never-exercised P1 assertion: the guest exports with `tar -C /workspace .`
  so members are `./`-prefixed (`./vm-result.txt`), not bare.
- `.env.example` — documents the new variable and the smolmachines registry ref.

## Evidence — 8/8 hosted probes on the real substrate

`SCHOOL_CORE_HOSTED_PROBES=1 pytest tests/test_smol_cloud_hosted_probes.py`
→ **8 passed** (138s). Real billable machines created and deleted; no orphans
(provider list unchanged before/after). Evidence file:
`data/hosted-qualification/hosted-probes-20261004T143824Z.json` (3681 bytes,
token-free).

| probe | acceptance item | result |
|---|---|---|
| P0 | spend/plan preflight | prepaidCreditMicros=99,065,460; plan ceilings; active |
| P1 | create→start→run→export→verify→destroy | `vm-result.txt` round-tripped; DELETE 200/204 |
| P2 | forbidden-egress denial | ping/getent/wget **blocked**; curl/python3 absent |
| P3 | host file/credential isolation | all 5 canaries **absent** (file, home, SSH_AUTH_SOCK, docker.sock, env secret) |
| P4 | no host fallback on guest failure | blocked "exit status 3"; host commands = `git` only |
| P5 | oversized export refused | blocked "exit status 1" |
| P6 | provider TTL sweep | machine `mach-838b69…` swept after 91s (ttl 60) |
| P7 | evidence bounded/sanitized/no provider write | 3681 B ≤ 256 KiB; no token; zero publish calls |

Full seam suite (adapter + probes + local seam + verifier): **102 passed, 2 skipped**.
Opt-in gate intact: probes skip 8/8 without `SCHOOL_CORE_HOSTED_PROBES=1`.

## Not claimed / remaining

- **Production dispatch stays disabled.** This qualifies the substrate; it does
  not enable it. Enabling is a separate operator decision (the adapter is still
  not wired into dispatch).
- **SCH-28** — the adapter still has **no spend ceiling** (`totalMicros` is only
  type-checked; `HARD_STOP_MICROS` lives in the probe file, not the adapter).
  With ~$99 prepaid and real machines now billable, this should close before
  dispatch is enabled.
- **SCH-29** — `_CloudApiStatusError` still discards the provider error body
  (`smol_cloud_runner.py:486`); every hosted failure needs a manual curl to
  diagnose.
- **SCH-27** — `bundle_sha256` computed, never verified by a consumer.
- No provider/GitHub write; branch not pushed; no PR; nothing committed by this
  run beyond working-tree edits.
