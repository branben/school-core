# Student VM Boundary Contract

Status: design constraints for the approved School-Core goal; **not evidence that a VM exists or is qualified**. No runtime/provider is selected by this document.

## Scope and authority

A coding task may run only when a disposable guest and the independent trusted-verification path are available. Failure to create, start, supervise, collect from, verify, or destroy the guest blocks the task. There is no host, Orca, or direct-model coding fallback for a task assigned to this boundary. Until qualification evidence exists, production coding dispatch remains disabled for this goal.

A task is scoped to one configured target repository and issue. The operator-controlled service owns task identity, repository identity, base identity, trusted check policy, resource limits, credentials, and lifecycle decisions. Student-controlled repository content, model output, guest output, and repository verification configuration are untrusted data.

## Assets and trust boundaries

| Boundary | Protected assets | Required rule |
|---|---|---|
| Operator host / control plane | Host files, host credentials, service availability, candidate records | Student tools cannot read arbitrary host files or write outside disposable staging. Never mount the host home directory or Orca control socket into the guest. |
| Repository ingress | Private repository contents, selected base commit, issue context | Clone only the configured repository at the recorded base. Validate repository slug and commit before launch. Treat issue/repo text as data, not policy. |
| Student guest | Ephemeral clone, shell, agent, toolchain, task-scoped model access | Guest is disposable and resource-bounded. No host GitHub credential, SSH agent, ambient cloud credential, or unrestricted host filesystem access. |
| Model relay | Model credential, task confidentiality, spend/quota | Student agent receives only a task-scoped relay capability, never the upstream API key. Relay enforces allowed operations, request/response limits, deadlines, quotas, and revocation. |
| Candidate export | Source changes, candidate identity, bounded evidence | Export only a bounded, validated artifact. Reject path traversal, symlinks escaping the artifact root, malformed or oversized archives/patches, and identity mismatch. |
| Trusted verifier | School-controlled checks and result integrity | Run separately from the student guest against the exact immutable exported candidate. Repository-provided checks are supplemental, explicitly untrusted evidence; they cannot replace school policy. |
| Teacher / publication boundary | Decision authority and target-repository write capability | Persist review against task, repository, candidate ID, and head SHA. Only an explicitly authorized approval under configured policy can request PR creation. Merge remains human-controlled. |

## Provider-neutral task contract

### Request

The control plane supplies a versioned request containing:

- `task_id`, `issue_id`, and `repository` (canonical `owner/name`);
- `base_ref` and full `base_sha` resolved by the control plane;
- bounded issue prompt and selected repository context, both marked untrusted;
- an operator-controlled guest image/toolchain identifier;
- a task-scoped relay handle, not its upstream credential;
- wall-clock, CPU, memory, storage, process, output-size, and network policy limits;
- a correlation/operation ID used for lifecycle and recovery.

The guest does not choose the trusted verification manifest, its own privileges, resource limits, allowed egress, PR destination, or approval rules.

### Result

The guest may return only bounded structured data:

- `task_id`, `repository`, `base_sha`, and a guest execution ID;
- terminal guest status and bounded, redacted diagnostics;
- a source candidate (patch or archive) plus digest and declared changed paths;
- a student report and explicitly supplemental repository-check evidence.

The control plane validates the response and imports it into a clean, operator-owned candidate repository. It then creates the immutable candidate manifest from observed Git state, not guest assertions. Candidate identity includes task/issue, canonical repository, base ref/SHA, head SHA, branch, changed-file set, and diff digest. Any disagreement or unbounded/malformed output is a failure.

Trusted verifier output binds task ID, repository, base SHA, candidate ID/head SHA, trusted-manifest digest, and the exact checks actually run. A missing, skipped, stale, or mismatched trusted check is not a pass. Teacher review binds to that same candidate and trusted evidence.

### Candidate checkpoint and rehydration (settled 2026-09-30)

A candidate checkpoint is the durable record plus the frozen, bounded candidate artifact. The durable record carries identity and evidence only — task/repository identity, base and candidate identity, head SHA, disposition, check results, and the bound artifact digest — and never embeds guest source; candidate content stays in the content-addressed artifact (archive digest plus per-file digests) validated at import. The record's head SHA already commits to the content through the commit tree, so rehydration need not trust the artifact blindly: it rebuilds the exact recorded head from base plus artifact under fixed commit metadata, and fails closed if the rebuilt head differs from the record. A record alone cannot rehydrate; without the artifact there is no candidate content to rebuild.

## Task-scoped model relay (threat model and capability contract)

**Assets.** The upstream model credential, operator spend/quota, task confidentiality (prompt and repository content), and the integrity of student work (model output feeds the candidate).

**Adversary.** A hostile student agent holding the relay handle inside the guest; hostile repository/issue text attempting prompt injection through the task payload; a compromised or malformed upstream response. The handle itself may leak to any of these.

**Attack vectors denied by the capability.** Using the relay as an arbitrary proxy (SSRF toward internal endpoints); extracting or replaying the upstream credential; quota exhaustion or oversized request/response as a memory or spend attack; reusing a captured handle after the task ended (replay); using one task's handle for another task (cross-task); and treating a relay failure as permission to fall back to host or direct-model execution.

**Capability contract.** The relay is a narrow, task-scoped capability, not a proxy:

- **Explicit allowed operations.** Only operator-listed operations (e.g. `complete`) are callable. Anything else is refused before the transport is touched. The request surface carries no URL, endpoint, method, or header parameter — the upstream endpoint is operator-pinned inside the transport, so the capability cannot be steered at arbitrary targets.
- **Per-task quota and time limit.** A bounded request count and total byte budget per task, plus a wall-clock deadline from capability creation. The total byte budget is a hard cap across requests and responses: request bytes are charged at admission, and a response that would exceed the remaining budget is refused fail-closed (never truncated or silently accepted). Exhaustion or expiry refuses further calls and revokes the capability.
- **Bounded request and response.** Requests above the configured size are refused before the transport is called; responses above the configured size are rejected fail-closed (never truncated into a misleading partial answer).
- **Task binding.** Every call names the task it belongs to; a handle scoped to task A refuses calls for task B.
- **Revocation on terminal state.** Revocation is idempotent and permanent for the capability. The runner revokes the attached capability on every terminal path — success, blocked, or cleanup failure — and a revoked capability refuses all further calls.
- **Fail closed.** Transport failure blocks the task with a visible relay error. There is no host, Orca, or direct-model fallback after a relay failure.
- **Credential isolation.** The upstream credential lives only in the operator-owned transport callable. Neither the guest, the request payload, nor the capability's evidence snapshot can observe it.
- **Bounded, redacted evidence.** The capability exposes a bounded usage snapshot (counters, limits, revoked state and reason) that never contains payload or response content.

## Lifecycle and recovery states

`queued → provisioning → running → collecting → imported → verifying → review_pending → approved | rejected | changes_requested → pr_pending → pr_open | pr_failed`

Any infrastructure or integrity failure transitions to visible `blocked` or `failed`, with a bounded reason and operation identity. Destruction transitions to `destroyed`; failed destruction transitions to `cleanup_quarantined`, retains only the minimum recovery metadata, and requires operator action before resource reuse. No failure state is interpreted as success. PR provider timeouts or ambiguous outcomes remain pending until provider state is reconciled by candidate identity; retries must not create duplicate PRs.

Required recoverable record fields are task/issue/repository identity, operation ID, base and candidate identity when known, last committed lifecycle state, attempt/error summary, verifier-manifest/result identity, teacher decision identity, PR reconciliation state, and cleanup/quarantine state. Secrets, relay tokens, private keys, raw credentials, unrestricted environment dumps, and unredacted sensitive logs are prohibited from durable evidence.

## Security and resource requirements

- Default-deny guest egress. Any model or package access must go through explicitly scoped endpoints or a separately reviewed broker; repository configuration cannot expand network access.
- Never pass raw model or GitHub credentials into the guest. PR credentials remain in the control plane and are used only after policy approval.
- Enforce hard task deadlines and CPU, memory, disk, process-count, request/response, patch, report, and log limits. Limits are operator-owned, not guest supplied.
- Treat filenames, symlinks, archive entries, patch headers, reports, and all guest output as hostile. Canonicalize and validate paths before import; reject escaping paths and unsupported file types.
- Destroy guest compute and revoke relay capability on every terminal path. Cleanup failure is observable and quarantined; it must not trigger host execution or silent reuse.
- Keep durable control/evidence state separate from ephemeral VM storage and include it in the reviewed backup/checkpoint policy.

## Qualification evidence required before production enablement

1. Demonstrate create → start → execute → bounded export → clean candidate import → separate trusted verification → destroy on a disposable target. Record exact image, host, candidate, and check identities.
2. Prove no host fallback on VM create/start/runtime/relay/collect/verify/destroy failures by asserting zero host executor calls.
3. Use host-canary files and credentials to prove they are unreadable/unavailable to the guest; prove no host home mount, Orca socket, SSH agent, or write path is exposed.
4. Probe forbidden egress and verify the guest cannot bypass the relay or reach arbitrary package/network endpoints.
5. Reject path traversal, escaping symlinks, malformed and oversized patches/archives/logs, candidate identity mismatch, wrong repository/base, and student-controlled trusted-check configuration.
6. Inject timeout, process death, relay failure, verifier failure, destroy failure, and restart between lifecycle transitions; prove visible recoverable state, cleanup/quarantine behavior, and no false pass or duplicate PR.
7. Verify all durable artifacts are bounded and sanitized while preserving audit identity. Never use a real provider write in these qualification tests.

## Unresolved gates

- Select and qualify a disposable Linux VM substrate for local developer proof; separately evaluate hosted support. This design does not approve any vendor or imply that an existing macOS Seatbelt profile is a VM.
- Decide the package/toolchain strategy and exact egress allowlist under the threat model.
- Define deployment authentication, teacher/operator roles, secret delivery, persistence/backup, capacity and cost limits, monitoring, and recovery before a hosted pilot.
- Define the exact source artifact format and import procedure in the runner implementation, then use that contract to resume candidate-bound publication integration.
