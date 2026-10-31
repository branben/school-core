# SCH-13 — post-SCH-20 disposition (hosted qualification)

Run: 98c76bd7-59a7-47f8-8cbf-968b9bcf38eb (phymora)
Date: 2026-10-04
Trigger: `issue_blockers_resolved` — SCH-20 (seam commit + hermetic suite) moved to done.
Branch: `school/sch-20-seam-hygiene` (not pushed).

VERDICT: **blocked** — on operator credential delivery only. Production student coding stays disabled.

## What changed this heartbeat (verified, not asserted)

Re-ran on the current tree:

- Clean checkout (`git archive HEAD` -> empty dir): `smol_cloud_runner.py`,
  `student_vm_runner.py`, `tests/test_smol_cloud_runner.py`,
  `tests/test_smol_cloud_hosted_probes.py` all present; `python3.9 -c "import smol_cloud_runner, student_vm_runner"` -> OK.
- `tests/test_smol_cloud_runner.py` -> **52 passed** (py3.9.6).
- Whole seam suite, env-neutralized (5 files) -> **107 passed, 10 skipped** (py3.9.6).
  The 10 skips are the 8 hosted probes + 2 real-guest probes gated on `SCHOOL_CORE_SMOLVM_PACK`.
- `tests/test_smol_cloud_hosted_probes.py` -> **8 skipped** (`SCHOOL_CORE_HOSTED_PROBES != 1`).
- `.env` `SMOL_CLOUD_TOKEN` is **empty** (len 0); `SCHOOL_CORE_SMOL_CLOUD_IMAGE` **absent** from `.env` and env.

SCH-20's own claims (seam files tracked, clean-checkout import, hermetic suite) reproduce.

## Acceptance-item coverage map (the important part)

Acceptance: *"tests prove no fallback, host file/credential isolation, forbidden-egress denial,
artifact/identity validation, timeout/process-death/cleanup behavior and quarantine."*

| Acceptance item | Covered by a RUNNING test | Only by a SKIPPED probe |
|---|---|---|
| no host-execution fallback | yes — `test_student_vm_runner.py:708` (positive-control spy), `:376` timeout, `:405` process-death | p4 (hosted) |
| artifact/identity validation | yes — `test_smol_cloud_runner.py:268,639,652,690,704`; identity at `student_vm_runner.py:800-807` | — |
| timeout / process-death / cleanup / quarantine | yes — `test_student_vm_runner.py:376,405,531,617,688`; `test_smol_cloud_runner.py:490,538,750,799,859` | — |
| **host file/credential isolation** | **no** — only argv-absence proxies: `test_student_vm_runner.py:182,184` (`--net`/`--ssh-agent`/`--docker-socket`/`--secret-*` not in create argv) | **p3** (`test_smol_cloud_hosted_probes.py:360`) |
| **forbidden-egress denial** | **no** — `--net`-absent-from-argv is a config proxy, not observed denial | **p2** (`:337`) |

**Finding (new, this heartbeat):** two explicit acceptance items — host file/credential isolation
and forbidden-egress denial — have **no executable test** on the current tree. Their only proof is
skipped hosted probes p2/p3, which have never run. The suite is green without them. The hosted
adapter requests `network: {mode: blocked}` (`smol_cloud_runner.py:207`) but that is a request
parameter, not a verified denial.

This does not change the disposition — both are gated on the same operator action — but it means
the acceptance is further from met than the "52 passed" line suggests.

## Still open

1. **BLOCKER (operator).** The 8 hosted probes need `SMOL_CLOUD_TOKEN` (`.env` value empty) and a
   digest-pinned `SCHOOL_CORE_SMOL_CLOUD_IMAGE`. Owner: operator. Action: deliver to `.env`
   (never in chat) + set the digest, then authorize the run. Interaction `9b6c5797` (pending,
   wake_assignee) collects the delivery choice.
2. **Delegated (agent).** Finding 4 — `bundle_sha256` write-only. Filed as **SCH-27**.
3. Finding 5 — retracted; no defect (see prior doc). Only an unreachable length nit remains.

## Not claimed

No billable machine created; hosted qualification not attempted; no provider/GitHub write; branch
not pushed; no PR opened; production dispatch unchanged (disabled).
