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
| `blocked` | present but unusable (corrupt lockfile, failed install) |
| `not_configured` | external configuration/credentials absent |
| `unknown` | deliberately skipped or not inspectable here |

Exit codes: `0` no required blockers · `1` at least one required check is
`missing`/`blocked`/`not_configured` · `2` usage error.

Checks marked `(advisory)` (deps, nix, flake-platform) never affect the exit
code; everything else does. Missing credentials and unreachable/unconfigured
services are **explicit blockers**, never silent success.

## Durable evidence

Both commands persist their full report to
`data/recovery/<command>-report.json` (machine-readable, inspectable after
process exit). Secret and endpoint **values are never printed and never
persisted** — reports carry names and set/missing state only.

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

## Clean-device flow

```sh
git clone <school-core> && cd school-core
python3 recovery.py bootstrap     # exit 0 when complete, 1 with precise blockers
python3 recovery.py doctor        # verify, then fix each reported remedy
```

Doctor remedies point at the exact missing key, file, or command for every
blocker it reports.
