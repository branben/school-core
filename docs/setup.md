# school-core Setup

Two tiers. **Tier A is required** to run what actually works today (the
verify-gate). **Tier B is optional** — it enriches context but is NOT needed
for verification, and is not wired in the current run.

## Tier A — required (verify-gate)

### 1. Install Determinate Nix
The verify-gate runs student code inside a hermetic Nix shell. If you use the
`kunchenguid/dotfiles` repo, `bootstrap.sh` installs it. Otherwise:

```sh
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
```

### 2. Enter the verify shell
From the `school-core` repo root:

```sh
nix develop .#verifyShell
```

This drops you into a shell with `node`, `pnpm`, `python`, `ripgrep`, `fd`,
`jq` and **no network provisioned**. The repo is mounted read-only at runtime
by `verify_gate.py`; outputs go to a temp scratch dir.

### 3. Run the pipeline
```sh
python issue_bridge.py --repo owner/repo --once
```
After each student task, the pipeline now calls `verify_gate.run_verify_gate`
on the cached clone, then merges any compile/typecheck/test failures into the
adversarial review as CRITICAL findings (see `config/anchors.yaml` →
`[Compile-Before-Critic]`). A broken build cannot earn a PASS.

### 4. Verify commands discovered
`verify_gate` finds commands from, in priority order:
1. `project_verify.yaml` at the repo root (explicit manifest), or
2. inferred from `package.json` scripts (`typecheck`/`lint`/`test`/`check`),
   including **sub-projects** (e.g. a `mobile/` dir with its own package.json
   that typechecks independently), and `pyproject.toml` (`pytest`/`ruff`).

Example `project_verify.yaml` (with the Orca per-subproject-motivation):
see `verify_gate.example.yaml` at the repo root.

## Tier B — optional (context enrichment)

These are *designed* but not required for the verify-gate. Skip unless you want
Layer 0/1 context enrichment.

- **CocoIndex** (`ccc`) — for `context_orchestrator._cocoindex_context`.
  Requires a built vault. Install + index per the CocoIndex docs.
- **Engram** — for `engram_adapter.search_trajectories` (Layer 2). Requires an
  Engram instance + credentials.

Neither is needed to execute or verify code. They only add retrieval context
to prompts. See `campus.md` → *Operational Reality* for the full status table.

## Safety notes
- `verify_gate` runs **only the repo's own declared** verify commands. It never
  executes free-form student scripts.
- The cached clone is copied to a writable scratch dir; the original cache is
  never mutated.
- Every command is timeout-bounded. A gate failure is reported as a finding,
  never a crash.
- v1 uses a Nix devShell (user-space isolation). If you later need to run
  arbitrary untrusted *binaries* (not just the repo's tests), graduate to a
  microVM boundary (Firecracker/Linux) for a kernel-level boundary — user-space
  sandboxing alone is escapeable.
