# Merge-state state journal

`state_journal.py` is the local transactional boundary for approval consumption
and external-operation bookkeeping.

## Storage and concurrency

- The store is a local SQLite database, not a JSON read/modify/write file.
- Each call opens a connection with foreign keys enabled, WAL journaling, and
  `synchronous=NORMAL`.
- Writes use `BEGIN IMMEDIATE`; approval state and the corresponding operation
  record commit in one transaction.
- SQLite's write lock serializes competing workers. The approval update is
  conditional on `state = 'issued'`, so only one worker can consume it.
- An already-consumed approval is replayable only by the same operation ID.
  A different operation receives `ConflictError` and performs no write.
- Operation idempotency keys are unique. Replaying a start returns the existing
  operation rather than creating a second logical side effect.
- Operation events are append-only. A retry may add another observation; it
  never rewrites prior event rows.

## Failure semantics

- Invalid identities, malformed SHAs, unknown records, and mismatched candidate
  identities fail before any external side effect.
- `failed` events set the operation status to `failed`; later `confirmed` or
  `failed` events are preserved in order.
- The store does not claim that a provider write succeeded. The coordinator must
  verify provider state before advancing to `merge_confirmed`.
- A crash before commit leaves no approval consumption or operation record. A
  crash after commit leaves both records, allowing an idempotent replay.

This primitive is provider-independent. It does not call GitHub, merge a PR,
approve anything, or close a Bead.
