# Clean-device recovery — `recovery.py`

One executable entrypoint turns a fresh checkout into a working runtime, or
reports precise blockers. No step prints placeholder guidance as a substitute
for setup.

## Commands

```sh
python3 recovery.py bootstrap     # provision: venv, deps, context, souls, checks
python3 recovery.py doctor        # read-only inspection of the same surfaces
python3 recovery.py doctor --json # machine-readable report on stdout
python3 recovery.py smoke         # disposable candidate → verification → evidence flow
python3 recovery.py restore       # read/sync Beads; audit local durable state
python3 recovery.py restore --json
```

Useful flags: `--root PATH` (operate on another checkout/root), `--no-venv`,
`--skip-deps` on `bootstrap`.

## What bootstrap actually does

1. Checks Python (>= 3.9) and git.
2. Creates `.venv/` (pip is bootstrapped on demand — venvs are created
   `--without-pip` so creation stays fast and offline-safe).
3. Installs `requirements.txt` into the venv.
4. Materializes the pinned envit context with
   `scripts/verify_context_lock.py` into `data/context/` (repos + skill
   symlinks at exact locked commits).
5. Validates `config/profiles/*/SOUL.md`, required credentials from
   `.env.example`, external service reachability (OmniRoute gateway), target
   repository resolution, and the durable state directory.

Runs are idempotent: repeated bootstrap re-verifies and completes cleanly.

## Result model

Every check carries one status:

| status | meaning |
|---|---|
| `ok` | verified working |
| `missing` | a required thing is absent (tool, file) |
| `blocked` | present but unusable (corrupt input, failed subprocess) |
| `not_configured` | external configuration/credentials absent |
| `unknown` | deliberately skipped or not inspectable here |

Exit codes: `0` no required blockers · `1` at least one required check is
`missing`/`blocked`/`not_configured` · `2` usage error.

Checks marked `(advisory)` (deps, nix, flake-platform) never affect the exit
code; everything else does. Missing credentials and unreachable/unconfigured
services are **explicit blockers**, never silent success.

## Durable evidence

Bootstrap and doctor persist full reports to
`data/recovery/<command>-report.json`. Restore does the same for its reports.
Secret and endpoint **values are never printed or persisted** — reports carry
names and set/missing state only.

## Recovery smoke (`smoke`)

Proves a checkout can do meaningful work against a real temporary Git target
repository — a full candidate → declared local verification → exact identity →
durable evidence lifecycle:

```sh
python3 recovery.py smoke                                   # disposable target
python3 recovery.py smoke --target /path/to/repo            # existing target
python3 recovery.py smoke --evidence-dir /path/to/evidence  # evidence location
```

1. **target** — a disposable target repo is created (or `--target` is
   validated: real Git work tree, at least one commit, clean tree).
2. **candidate** — a real branch + committed change becomes an immutable
   `CandidateManifest` (base/head SHAs, diff digest) in `candidates.json`.
3. **verification** — the target's *declared* verify commands
   (`project_verify.yaml`) execute as real subprocesses, bound to the exact
   head via `run_candidate_gate`.
4. **identity** — candidate/head identity is re-validated against live Git.
5. **journal** — an append-only `StateJournal` operation + events land in
   `state.sqlite3` (`confirmed` / `failed`).
6. **evidence** — `candidate.json`, `gate-evidence.json`, `report.json` under
   the evidence dir (default `data/recovery/smoke-<timestamp>/`).

Fail-closed throughout: no declared verification commands, failing commands,
dirty or non-Git targets, and identity drift are explicit blockers (exit 1),
never silent success. The flow performs no GitHub contact and no Beads
mutation; the same path works against an authorized target repository via
`--target`.

## Durable-state restore (`restore`)

Restore reads `.beads/config.yaml` and `.beads/metadata.json`; it does not
accept remote URLs from command-line input. It is safe to rerun and uses the
configured remote only:

1. If the configured Dolt database is absent, run `bd bootstrap --yes --json`.
   The command must report a remote sync/clone/restore and produce a readable
   database before restore continues.
2. If the database exists, validate the Dolt repository state, ensure its
   `origin` matches `sync.remote`, and confirm the issue listing is non-empty.
   Corrupt, empty, or mismatched local state fails closed; restore does not
   overwrite it automatically.
3. Run `bd dolt pull --remote origin` and validate the issue listing again.
   This is a remote **read/sync**. It does not push or change public GitHub
   state. Credentials are inherited by `bd` for authentication; raw command
   output is never copied into the report.
4. Inspect `data/trajectories/` and `data/recovery/` read-only. JSON evidence
   must parse, SQLite journals must pass `PRAGMA quick_check`, and symlinks
   must remain inside their respective directories. Reports record file counts
   and content digests, not file contents.
5. Report missing local service configuration, credentials, and target repo.
   Secret values and endpoint URLs are not stored in the report.

A successful Beads pull does not recreate ignored local runtime data. If
trajectory or evidence paths are absent or corrupt, restore them from their
configured authoritative backup before claiming full recovery.

## Persistence contract

All durable stores that must survive fresh checkouts are defined in
`persistence_contract.py`. The contract specifies:

**Checkpointed stores** (committed to `board-publish` after sanitization):
- `data/compound_learning.json` — bounded post-bead learning loop
- `data/candidate_bindings.json` — verification/approval posture per candidate
- `data/state.sqlite3` — approval CAS + operation journal
- `data/recovery/` — recovery smoke evidence tree
- `data/trajectories/` — Layer 2 trajectory corpus
- `data/last_run.json`, `data/processed_issues.json`, `data/scores.json`,
  `data/retry_issues.json`, `data/crew_runs.json`, `data/grading_queue.jsonl`

**Seeded stores** (restored from `board-publish` before the bridge runs):
- All of the above, plus `data/issues_cache.json`

**Sanitization** (allowlisted, applied before checkpoint):
- Preserves audit identity: `bead_id`, `candidate_id`, `head_sha`, `actor`,
  `scope`, `approval_id`, `operation_id`, `issue_number`, `repository`,
  `branch`, timestamps, verdicts, scores
- Redacts credentials: `api_key`, `access_token`, `refresh_token`,
  `authorization`, `auth`, `secret`, `password`, `credential`, `private_key`
- Redacts home paths: `/Users/<name>/...` → `~`, `/home/<name>/...` → `~`
- Redacts tokens: `sk-...`, `gh[pousr]_...`, `bearer ...`

The school-loop workflow (`.github/workflows/school-loop.yml`) implements
this contract. The `persistence_contract.py` module is the single source of
truth for checkpoint and seed path lists.

## Clean-device flow

```sh
git clone <school-core> && cd school-core
python3 recovery.py bootstrap     # exit 0 when complete, 1 with precise blockers
python3 recovery.py doctor        # verify, then fix each reported remedy
python3 recovery.py restore       # recover Beads and audit local durable state
```

Doctor remedies point at the exact missing key, file, or command for every
blocker it reports.
