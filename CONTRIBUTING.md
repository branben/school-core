# Contributing to school-core

Thank you for your interest in contributing. school-core is a developmental
framework for AI agents — think of it as a school where every task is a
practice opportunity that leaves a measured trace of what an agent can and
cannot do.

## How to contribute

### 1. Fork and clone

```bash
git clone https://github.com/YOUR_USERNAME/school-core.git
cd school-core
```

## 2. Set up the environment

```bash
pip install -r requirements.txt
```

Core is mostly Python 3.9+ stdlib. The only third-party dependencies are
PyYAML and pytest.

> **⚠️ Critical:** Before your first reading, read
> [`docs/STATE-MAP.md`](docs/STATE-MAP.md). It documents where truth lives,
> the git diff convention, Python floors, directory-name coupling, and pipe
> discipline — traps that cost real time in this session.

### Python version

| Environment | Python | Notes |
|---|---|---|
| CI (`ci.yml`) | 3.9 (compileall) + 3.12 (pytest matrix) | `str \| None` only safe with `from __future__ import annotations` |
| Hermes venv | 3.11 | Where most developers run tests |
| Runtime | varies | Orca worktrees use agent profile pins |

### Git diff convention (NEVER use moving tip)

```bash
# WRONG — renders main's gains as branch deletions
git diff main..HEAD

# CORRECT — diff against the fork point
git diff $(git merge-base main HEAD)..HEAD
```

### Directory naming

Tests assert `REPO_PATH.endswith("school-core")`. Cloning to `/tmp/wk-ci`
fails. Clone to a directory named `school-core` or relax the test assertion.

### Pipe discipline

```bash
# WRONG — pipe reports the LAST element's exit code
python -m compileall -q . | head

# CORRECT — run bare, read the real exit code
python -m compileall -q .
echo "exit=$?"
```

### Test isolation

`tests/conftest.py` monkeypatches `crew_dispatch.FM_HOME`, `STATE_DIR`,
`DATA_DIR`, `CREW_RUNS_FILE`, and all `data/*.json` writers. A test that
creates its own `.venv*` in the workspace will break `compileall`.

### State locations

| State | Location | In repo? |
|---|---|---|
| Producer status files | `~/.hermes/school-core-fm-config/state/*.status` | ❌ Outside |
| Consumer ledger | `school-core/data/crew_runs.json` | ✅ Inside |
| Issue tracker | `school-core/.beads/dolt/` (Dolt DB) | ✅ Inside |

### Producer/consumer invariant

Every status file with a terminal verb (`done`/`failed`) MUST have a ledger
record. Audit: `python crew_ledger_reconcile.py`

### 3. Configure

```bash
cp config.example.yaml config.yaml
# Edit target_repos and labels for your own repo
```

### 4. Run the tests

```bash
python -m pytest -q
```

Tests run on Python 3.9, 3.11, and 3.12 in CI (`.github/workflows/ci.yml`).

### 5. Make your change

- **Bug fix?** Add a failing test first, then fix it (TDD).
- **Feature?** Update the README if user-facing behavior changes.
- **Refactor?** Run the full suite to confirm nothing breaks.

### 6. Open a pull request

- Use a descriptive title that explains the *motivation* (why), not just
  what changed.
- Link to the relevant issue or bead ID if applicable.
- Include evidence: test results, screenshots, or benchmark numbers.

## Development guidelines

### Architecture

school-core is a three-tier loop:

- **Principal** (`conductor.py`) — routes work by measured competency (EFC),
  own the durable verdict record, notifies humans. Runs as a persistent
  Orca automation.
- **Teacher** (`teacher.py`) — performs adversarial review with two axes
  (CTO: correctness + security, COO: completeness). Stateless lenses.
- **Student** (`leaf.py`) — executes tasks with optional doubt-driven
  development (DDD) gate, scored by the EFC gate.

### Key concepts

- **EFC (Expected Fraction of Correct)** — the single metric that drives
  routing decisions. Measures how often an agent succeeds at a given task
  type.
- **EMA scoring** — per-domain exponential moving averages namespaced per
  repo so multi-repo runs don't collide.
- **Semantic anchors** — bracket tokens (`[Fagan Inspection]`, `[TDD London
  School]`) that activate whole methodologies. Defined in YAML, not prompt
  strings.

### Adding a new role or gate

1. Update the score schema if the gate adds new dimensions.
2. Add the lens to `lenses/` if it's a new adversarial axis.
3. Update the README roles section.
4. Write a test in `tests/` that covers the new path.

### Code style

- Python 3.9+ compatible.
- stdlib-first — the core has no heavy dependencies.
- Functions over classes when there's no shared state.
- Docstrings on all public functions.

## Reporting issues

Use GitHub issues with a clear reproduction:

1. What you expected to happen
2. What actually happened
3. How to reproduce it
4. Environment (OS, Python version, school-core commit SHA)

## License

MIT — see [LICENSE](LICENSE).
