# Agent School — school-core

A **developmental framework for AI agents** built around pedagogy, not task
execution. school-core routes work to the best-qualified model, scores
outcomes, runs adversarial review, and grows agents through measured practice.
Not a chatbot. Not a task runner. A school.

> Identity and behavioral core live in [`campus.md`](campus.md). Read it first
> if you want the philosophy; read this if you want to run or extend it.

## Why a school, not a swarm

Most multi-agent frameworks are **orchestration graphs**: a planner breaks a
task, workers execute, a reducer merges. They optimize *throughput*. school-core
optimizes *competency* — every task is a practice opportunity that leaves a
durable, measured trace of what an agent can and cannot do.

The three ideas that make it different:

1. **Roles over prompts.** Each agent inhabits a *role* (Student / Teacher /
   Principal) with versioned standards loaded before every task. The role is the
   persistent identity; the prompt is just the current assignment.
2. **The compiler runs before the critic speaks.** Student output is *executed*
   (typecheck / test / build in a hermetic Nix shell) and the real failures are
   fed into adversarial review. LLM-as-judge is the *last* tier, never the
   first.
3. **Semantic anchors.** Senior-engineer knowledge is compressed into bracket
   tokens (`[Fagan Inspection]`, `[TDD London School]`, `[Compile-Before-Critic]`)
   that activate whole methodologies in the model. Anchors are data, not code —
   addable in YAML, versioned, auditable.

## The pedagogy loop

```
Principal routes  ──►  Student executes (Orca worktree, scored by EFC gate)
        │                      │
        │                      ▼
        │              Adversarial review (CTO: correctness+security,
        │                                COO: completeness)  ← two-judge
        │                      │
        ▼                      ▼
Bookbag (durable verdict)  ──►  ScoreStore EMA update  ──►  growth tracked
```

A task is *accepted* only when **both** judges PASS **and** each score ≥ 50
**and** no CRITICAL finding exists. Otherwise it is declined and the agent's
score is penalized — so the next routing decision is measurably better.

## The roles — what each one does, and what it uses

school-core is a three-tier loop. Each tier is a real code path, not a metaphor.

### Principal (`conductor.py`)
**Job:** Route work by *measured* competency (EFC — Expected Fraction of
Correct), own the durable verdict record, and notify humans. Runs as a
persistent **Orca automation** (`--serve`), so Orca owns the schedule — no
`while True` pane, no zombie processes.

- **Does:** fetches issues (GitHub / `github_fetcher.py`), picks the
  best-qualified student via `director.run_task`'s gate logic, boots the two
  teacher worktrees, waits for verdicts, persists acceptance to the bookbag, and
  emails the human via `school_mail.notify_verdict` (best-effort AgentMail).
- **Tools/skills it uses:** the `bd` (beads) CLI for issue tracking,
  `gh`/`github_fetcher` for repo state, `orca_executor.OrcaExecutionManager`
  for worktree + terminal lifecycle, `school_mail` (AgentMail `/v0`, no SDK),
  and the `principal` soul from `campus.md`.

### Teachers (`teacher.py` — two judges)
**Job:** Adversarially review the student's bookbag. There are exactly two,
each with a fixed lens — this is deliberate, not configurable sprawl:

| Teacher | Lens | Asks |
|---------|------|------|
| **CTO** | `CORRECTNESS` + `SECURITY` | "Does this code actually work? Is it safe?" |
| **COO** | `COMPLETENESS` | "Does this fully address the issue? Edge cases?" |

- **Does:** each runs in its own **persistent Orca worktree** (`TeacherWorktree`
  — `create_worktree_persistent` handles rediscover-or-create so re-serves
  never mint `cto-2`/`cto-3`). `review_cycle()` polls the bookbag, runs the
  `AdversarialReviewer` with its lens, and writes the verdict back.
- **Tools/skills it uses:** `adversarial_reviewer.AdversarialReviewer` (text
  review), the `lenses/` definitions, `bookbag` read/write, and
  `wait_for_verdicts` for the async handoff. The CTO+COO two-judge contract is
  enforced centrally in `conductor._persist_acceptance` — both PASS ∧ score≥50
  ∧ no critical → accept.

### Students (`leaf.py` — `StudentLeaf`)
**Job:** Do the work. Lives in an Orca worktree, executes the task through
`director.run_task`, and writes a **bookbag** (the durable contract between
student and teachers).

- **Does:** `boot()` → `write_brief()` → `run_task()` (which calls
  `director.run_task`, executing the model via **OmniRoute** at
  `localhost:20128` and writing the bookbag per-repo) → `signal_ready()`.
  `run_leaf(role, domain, repo=)` is the entry point used by the principal.
- **Tools/skills it uses:** `orca_executor.OrcaExecutionManager` (worktree +
  Hermes execution), `director.run_task` (execution + scoring + review
  orchestration), `bookbag` (durable verdict record), `scoring.ScoreStore`
  (EMA), and the semantic-anchor context from `director._anchor_context`.

### Cross-cutting: scoring & memory
- **`scoring.py`** — `ScoreStore` tracks per-domain EMA scores, namespaced
  **per repo** (`data/scores-<repo>.json`) so multi-repo runs don't collide.
- **`trajectory.py` / `engram_adapter.py`** — optional trajectory persistence
  to Engram (auto-detected; skipped if absent). Memory without consolidation is
  noise; this is the consolidation layer.

## Semantic anchors — the uniqueness

This is the part no other framework does. school-core does not prompt engineers
with "be a good reviewer." It loads **semantic anchors**: data-defined tokens
that activate whole methodologies the model already understands.

```yaml
- name: "Fagan Inspection"
  tier: methodology
  domain: code-review
  activation_pattern: "Conduct a structured, systematic code review with defined roles and checklists"
- name: "TDD London School"
  tier: methodology
  domain: python-testing
  activation_pattern: "Outside-in testing with mocking, top-down development"
- name: "Compile-Before-Critic"
  tier: principle
  domain: code-implementation
  activation_pattern: "Execute the code before judging prose"
```

Defined in [`config/anchors.yaml`](config/anchors.yaml) (currently ~30 anchors
across three tiers: **methodology**, **principle**, **technique**), loaded by
[`anchor_loader.py`](anchor_loader.py) and injected per-role via
`director._anchor_context`. Anchors are:

- **Versioned & auditable** — they live in YAML, not in prompt strings buried
  in code. You can see exactly which methodology a given run activated.
- **Addable without code changes** — drop a new anchor in the registry and it
  is queryable by `domain` / `tier` / `difficulty_gate` immediately.
- **Competency-gated** — `difficulty_gate` (easy/medium/hard) means a harder
  methodology (e.g. Fagan Inspection) is only loaded for harder tasks.
- **Role-scoped** — `ROLE_ANCHOR_DOMAINS` in `director.py` maps each role to
  the methodology/principle anchors it should inherit, so a Student and a
  Principal get different toolkits.

The payoff: instead of teaching the model "review carefully" every time, you
activate `[Fagan Inspection]` once and the model *inhabits* a structured,
checklist-driven review process. Knowledge is compressed into tokens the model
already weights highly — cheaper, more consistent, and inspectable.

## How it compares to other multi-agent frameworks

| Dimension | Typical orchestration graph (AutoGen, CrewAI, LangGraph) | **school-core** |
|-----------|--------------------------------------------------------|-----------------|
| Success metric | Task completed / tokens used | **Measured competency growth** (EFC, difficulty-adjusted) |
| Review | Optional LLM critic, or none | **Mandatory two-judge** (correctness+security / completeness) + executed-code findings |
| Ground truth | Often prose-only judgment | **Compiler/test runs before the critic speaks** (Nix hermetic shell) |
| Agent identity | A role *string* in a prompt | **Versioned role + loaded soul + anchor toolkit** |
| Knowledge transfer | Re-prompted each run | **Semantic anchors** + trajectory consolidation (Engram) |
| Routing | Hand-authored graph or planner | **Competency-gated** — students only get tasks at/below their proven level |
| State | In-graph, ephemeral | **Bookbag** (durable per-repo verdict record) + per-repo ScoreStore |
| Multi-repo | Usually single-repo | **Namespaced bookbags/scores per target repo** out of the box |

school-core is not trying to replace a planner. It is a *training and
certification* layer: it tells you not just what got built, but whether the
agent that built it is *getting better*, and at what.

## Quickstart

```bash
pip install -r requirements.txt        # stdlib-only core; OmniRoute/Orca are external runtimes

# Point it at your repos (multi-repo namespacing):
cp -f config/github.example.yaml config/github.yaml
#   edit: target_repos: [owner/repo-a, owner/repo-b], orchestrator_repo: owner/school-core

python conductor.py --serve             # Principal as a persistent Orca automation
python conductor.py --task "fix the flaky auth test" --repo owner/repo-a
python conductor.py --list-bookbags     # inspect durable verdicts (per repo)

# Or via the MCP server (Hermes):
#   register in ~/.hermes/config.yaml → mcp_servers.agent-school → mcp_server.py
```

## MCP surface (`mcp_server.py`)

Pure-stdlib JSON-RPC over stdio — no external deps. Exposes:

- `school_route` — route a task to the best-qualified agent (no execution)
- `school_execute` — execute a prompt against a specific agent
- `school_evaluate` — submit an evaluation to update scores
- `school_run` — route + execute + auto-evaluate (convenience)
- `school_list_agents` — list agents with scores and gate levels
- `school_list_domains` — list known domains and gate counts
- `school_get_trajectory` — read a saved trajectory by path
- `school_get_leaderboard` — read the agent competency leaderboard
## Tests

```bash
pytest -q        # 490+ tests (CI runs on Python 3.9 / 3.11 / 3.12)
```

CI runs `.github/workflows/ci.yml` — a pytest gate on every push and PR. The
regression suite specifically guards the multi-repo namespacing seams
(`tests/test_two_judge_repo_namespace.py`, `tests/test_orca_executor_repo_path.py`,
`tests/test_escalation.py`).

## Layout (what's actually in this repo)

```
campus.md              identity + behavioral core (the "soul")
conductor.py            Principal — orchestrates the pipeline, owns verdicts
director.py             execution + evaluation + two-judge review + escalation
leaf.py                 StudentLeaf — the student's worktree lifecycle
teacher.py              TeacherWorktree (CTO/COO) — persistent review worktrees
orca_executor.py        Orca worktree/terminal lifecycle (REPO_PATH resolution)
bookbag.py              durable per-repo verdict record (write/read/update)
scoring.py              ScoreStore — per-domain EMA, namespaced per repo
adversarial_reviewer.py + lenses/   the two-judge review engine
anchor_loader.py       semantic-anchor registry loader
config/anchors.yaml     the anchor registry (methodology/principle/technique)
school_mail.py          best-effort AgentMail verdict notify
mcp_server.py           stdio MCP server for Hermes
github_fetcher.py       target-repo / issue ingestion
tests/                  regression + unit suite
```

## License

MIT — see [LICENSE](LICENSE).
