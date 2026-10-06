# SCH-30 — adversarial review update (2026-10-04, post-SCH-28)

**Reviewer:** phymora (adversarial). **Surface reviewed:** issue SCH-30
(`2421b734-6f97-46dd-a792-c1d2ed1dfa2e`), its operator confirmation
`b89524a5-50f6-4513-b9aa-215ae9444c56`, and the evidence it cites.

**Change since prior review:** SCH-28 (spend ceiling) is now `done`. The
adapter has a real accumulator + ceiling (`smol_cloud_runner.py:50,160,192,219-223,364,578`).
Two tests pass: `test_spend_ceiling_refuses_create_after_cumulative_cost_reaches_cap`
and `test_spend_guard_issues_no_request_when_already_over_cap`.

**Remaining blockers (unchanged):**

- **BLOCKER 1 — no switch to flip.** Grep `*.py/*.yaml/*.yml` for
  `PRODUCTION_STUDENT|STUDENT_DISPATCH|VM_DISPATCH|DISPATCH_MODE|RUNNER_BACKEND|HOSTED_ENABLED|USE_HOSTED`
  → no matches. "Production student coding stays disabled" is prose only
  (`goals/school-core-complete/plan.md:14`, `docs/student-vm-boundary.md:7`).
  `issue_bridge.py` never imports `student_vm_runner`/`smol_cloud_runner`.
  This is a net-new integration, not an enable flip.
- **BLOCKER 2 — hosted trusted verification unqualified.** Boundary item 1
  (`docs/student-vm-boundary.md:94`) requires "…export → clean candidate import →
  **separate trusted verification** → destroy." P1
  (`tests/test_smol_cloud_hosted_probes.py:307`) calls `runner.execute()` and
  reads the archive directly — no `import_guest_candidate_archive`, no
  `materialize_guest_candidate`, no verifier. The only verifier is
  `SmolVmVerifier` (`verifier_vm.py:428`), which drives the **local** `smolvm`
  binary (`:500-506`) — there is no hosted verifier. Yet
  `docs/research/sch13-hosted-qualification-pass.md:80` and the confirmation
  `detailsMarkdown` both claim P1 covers "verify". It does not.
- **MAJOR 3 — P4 proves one failure class, not the boundary's set.** P4
  (`:436`) injects a single guest `exit 3`; boundary item 2 names
  create/start/runtime/**relay**/collect/**verify**/destroy failures. No probe
  attaches a relay at all.
- **MAJOR 4 — item 6 (timeout/process-death/verifier/destroy/restart) has no
  hosted coverage.** P6 (`:495`) proves only the TTL sweep. The injections
  exist only as hermetic fakes in `tests/test_smol_cloud_runner.py`.
- **MAJOR 6 — cited evidence leaks host paths.**
  `data/hosted-qualification/hosted-probes-20261004T143824Z.json` →
  `p4_guest_failure.payload.host_commands` contains absolute host paths
  (`/private/var/folders/fq/…/pytest-of-brandonbennett/…`). P7 only bounds size +
  "no token".
- **NIT 7** — "8/8 probes" counts P7 (evidence hygiene), not the 7 boundary
  items.

**Simpler alternative (mandatory pass):** do nothing to production dispatch now
(the golden-path goal is blocked on zero real issues since 2026-08-31, not on
the VM substrate); add ONE hosted probe that runs the real
`dispatch_and_verify_student_task` flow with `SmolCloudRunner` + a hosted
verifier; then wire `issue_bridge` behind a new flag as a separate ticket.

**Verdict: fix-then-ship → REJECT the "enable" framing.** Do not accept this
confirmation. The single biggest reason: the acceptance item that matters most
for a *student-execution* boundary — a **separate trusted verification** of the
exported candidate — was never qualified on the hosted substrate, and the
"production student coding disabled→enabled" switch the issue says to flip does
not exist. Accepting would enable nothing, or (if wired minimally) would enable
a path whose verifier leg is unqualified.

**Disposition:** SCH-28 is resolved. The v2 confirmation `b89524a5` is still
pending operator response. Production student coding stays disabled. No deploy,
no provider/GitHub write.
