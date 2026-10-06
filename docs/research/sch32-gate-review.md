# SCH-32 — gate review (2026-10-04, phymora)

**Reviewer:** phymora (adversarial). **Surface:** issue SCH-32
(`f5621df5-699e-40c1-922d-2d5a65e547ee`), its declared prerequisite SCH-31,
and the hosted evidence both cite.

**Verdict: not startable — BLOCKED.** SCH-32's own description gates on
SCH-31 ("Only after SCH-31 (hosted verification probe) passes"). SCH-31 has
not passed.

## B1 — prerequisite gate unmet (blocker)

SCH-31 is `in_review`, not `done`. Its review interaction
`50d2f47c-2a96-43a6-9f37-8c5676eab8c1` (`request_confirmation`,
`continuationPolicy: none`) is **pending** since 2026-10-04T17:11:32Z, and my
SCH-31 review verdict is **fix-then-ship**, not ship:

- M1 — `SmolCloudRunner.verify()` duplicates ~100 lines of create/start/exec/
  delete from `_execute()` (`smol_cloud_runner.py:690-835` vs `:242-404`).
- M2 — `verifier=runner,  # type: ignore[arg-type]`
  (`tests/test_smol_cloud_hosted_probes.py:378`) masks a real `VerifierSeam`
  structural mismatch instead of declaring conformance.

## B2 — the gating probe has never executed hosted (blocker for the gate)

SCH-31's new probe `test_p1_full_dispatch_and_verify_flow`
(`tests/test_smol_cloud_hosted_probes.py:352-406`) drives the real
`dispatch_and_verify_student_task` flow (`verifier_vm.py:307`). It has **not
been run against real cloud**:

- Newest evidence `data/hosted-qualification/hosted-probes-20261004T153206Z.json`
  contains **only** `p0_account_preflight`. No file in
  `data/hosted-qualification/` contains `p1_full_flow`.
- The probe file's mtime is `2026-10-04 10:00:38 -0700` (= 17:00:38Z), i.e.
  authored **after** the last hosted run at 15:32:06Z.
- `pytest --collect-only` collects 9 tests (no import error), but collection
  is not execution; the probe is skipped unless `SCHOOL_CORE_HOSTED_PROBES=1`.

So SCH-31 currently asserts a flow on a path that has never produced hosted
evidence. Its `in_review` state is premature; the gate is not cleared.

## S1 — scope of the eventual integration (not started; for the record)

`grep -n "student_vm_runner\|smol_cloud_runner" issue_bridge.py` → **zero
matches**. Net-new integration confirmed. Three seams:

1. **Flag** — a fail-closed parse alongside the existing pattern at
   `issue_bridge.py:1197` (`CREW_ENABLED=garbage → off`).
2. **Dispatch branch** — the `student_generation` stage
   (`issue_bridge.py:2026-2066`) currently routes to `director.run_task`
   (`director.py:1007`). The hosted path would call
   `dispatch_and_verify_student_task` (`verifier_vm.py:307`) /
   `dispatch_student_task` (`student_vm_runner.py:1398`) instead.
3. **Fallback** — preserve the no-host / Orca / direct-model fallback; the
   hosted path must fail closed (`StudentVMBlocked`) into the existing retry
   semantics, not the host.

## Unblock path

Owner: **student-coder** (`2c4d0f25-b3a5-4a9e-8112-403859274b87`).

1. Resolve SCH-31 confirmation `50d2f47c`; fix M1 + M2.
2. Run `SCHOOL_CORE_HOSTED_PROBES=1 python3 -m pytest
   tests/test_smol_cloud_hosted_probes.py::test_p1_full_dispatch_and_verify_flow`
   against real SmolCloud; produce evidence containing `p1_full_flow`.
3. Close SCH-31 `done`. SCH-32 unblocks.

Production student coding stays disabled. No provider/GitHub write, no deploy.
