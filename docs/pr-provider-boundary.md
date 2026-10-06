# PR provider boundary — threat model and capability contract

Scope: `pr_provider.py` (publication state machine + provider adapter) and its
consumption of `candidate_pr.publish_candidate_pr`. This is the seam that turns
a validated candidate into a provider PR. Update this file first for any
boundary change.

## Assets

1. The provider's PR surface — no duplicate PRs for one candidate, no forged
   or unverifiable success claims.
2. Durable school state — the run record, the issue close/label, and the
   processed checkpoint. These must never claim success that the provider did
   not confirm.
3. Candidate identity — `(candidate_id, head_sha)` binding of everything that
   is published.

## Failure model (the actual adversary here is the provider, not a hostile user)

| Failure | Example | Classification |
|---|---|---|
| Pre-write refusal | stale head, dirty tree, changed base, digest mismatch, empty diff, branch ownership lost | `pr_failed` — **no provider write happened** |
| Write attempted, result untrustworthy | `publish()` raised (timeout mid-create), returned `None`, returned mismatched `candidate_id`/`head_sha`, or no valid PR URL | `pr_pending` — **outcome unknown** |
| Crash between journal and write | process dies after `pr_pending` is recorded | `pr_pending` — safe: replay reconciles |

## Fail-closed rules

1. **Journal before write.** `pr_pending` is durably recorded *before* the
   provider call. A crash can only leave `pr_pending`, never a silent gap that
   invites a duplicate write.
2. **Ambiguity never upgrades itself.** Only a provider answer bound to
   `(candidate_id, head_sha)` (or a recorded `pr_published` row) turns a
   publication into success. A guess is not a reconciliation.
3. **Reconcile before retry.** A `pr_pending` row is resolved by asking the
   provider for the PR bound to that candidate/head. Found → adopt the URL, no
   second write. Definitively absent → a fresh write is safe. Unknown (no
   `find_pr`, provider error) → **block**; we do not risk a duplicate PR.
4. **Idempotent replay.** A `pr_published` row with the same `head_sha` returns
   the recorded URL without touching the provider.
5. **Pre-write refusals are terminal for that candidate** (`pr_failed`); a
   retry must be a new candidate, not a re-press of a refused one.

## Non-goals

- Merge is human-owned. This seam only opens PRs.
- Provider selection and live external writes are a standing human gate; the
  production adapter (`GitHubCliPublisher`) is exercised only via captured-argv
  tests here. No live GitHub write is part of unit tests.

## Production enablement (issue_bridge.py)

- Candidate-bound publication is **disabled by default**. `issue_bridge` refuses
  a manifest-bearing task unless `CANDIDATE_PR_ENABLED` is explicitly truthy
  (1/true/yes/on); an absent, false, or unparseable value is OFF. A disabled
  seam REFUSES rather than falling back to the legacy patch-blob `pr_creator`
  path, and the issue stays retryable/unprocessed with no provider write.
- This is the operator opt-in required before the bridge routes an exact
  candidate manifest through the gate and provider. Production coding dispatch
  stays disabled until the disposable-VM phase qualifies.
- Label/color policy and comment style stay in `issue_bridge`/`pr_creator`.

## Capability contract

- `publish_candidate_pr` (candidate_pr.py) validates the candidate against its
  store and extracts the exact `base_sha..head_sha` diff before any provider
  call. Provider-write failures raise `ProviderWriteError` (a
  `CandidatePublicationError`); everything else is a pre-write refusal.
- `publish_candidate_pr_idempotent` (pr_provider.py) wraps it with the durable
  `PrStateStore` journal and the reconcile-first retry rule above.
- A `CandidatePublisher` supplies `publish(**request) -> dict`. Optional
  `find_pr(repository=, candidate_id=, head_sha=, branch=) -> str | None`
  enables reconciliation; without it, a `pr_pending` row can never clear
  (fail-closed, see rule 3).
