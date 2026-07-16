# school-core

Agent School — a developmental framework for AI agents. It routes work to the
best-qualified model, scores outcomes, runs adversarial review, and grows
agents through measured practice. Not a chatbot. Not a task executor. A school.

> Identity/soul lives in [`campus.md`](campus.md). Read it first if you want the
> philosophy; read this if you want to run it.

## What it does

- **Routing** (`routing.py`) — pick the best-qualified agent for a task by domain/gate.
- **Execution + evaluation** (`director.py`, `scoring.py`) — run a prompt, score it, update gates.
- **Adversarial review** (`adversarial_reviewer.py`, `lenses/`) — correctness / security / completeness review axes. Stateless lenses, merged by a reviewer.
- **Verify gate** (`verify_gate.py` + `flake.nix`) — run untrusted student code (typecheck/test/lint) in a **hermetic Nix shell** with no network. The compiler runs before the critic speaks.
- **Trajectories + memory** (`trajectory.py`, `engram_adapter.py`) — optional persistence to Engram (auto-detected; skipped if absent).
- **Autonomous loop** (`autonomous_loop.py`, `issue_bridge.py`) — poll a GitHub repo, bridge issues into tasks, run them.

## Quickstart

```bash
# 1. Install deps (or use the Nix flake: `nix develop`)
pip install -r requirements.txt

# 2. Configure
cp config.example.yaml config.yaml
#   edit repo: and labels: to point at your own repo

# 3. Run the CLI
python3 cli.py --help
python3 cli.py list-agents
python3 cli.py route "fix the flaky test in auth.py" --domain code-implementation

# 4. (Optional) MCP server for Hermes
#   Register in ~/.hermes/config.yaml:
#     mcp_servers:
#       agent-school:
#         command: python3
#         args: ["/abs/path/to/school-core/mcp_server.py"]
#         enabled: true
```

## Tests

```bash
pytest -q      # 377 tests, ~15s
```

## The visibility layer (what gets rendered)

school-core ships both **competency** visualization and a **task-board** (kanban) view:

| Artifact | Module | What it shows |
|----------|--------|---------------|
| **Agent leaderboard** | `leaderboard.py` | HTML cards of agents with per-domain scores + gate status. This is the primary visibility surface. |
| **Activity dashboard** | `generate_activity_dashboard.py` | Timeline of school activity. |
| **Weekly report** | `docs/weekly_report.py` | Per-week gate-crossing summary (`docs/weekly/*.html`). |
| **Architecture review** | `architecture-review.html` | Static module/seam map (generated). |
| **Task Board** | `board.py`, `activity_server.py` | Kanban of GitHub issues -> agent tasks -> review -> done. 4 columns (To Do / In Progress / In Review / Done). Self-contained HTML, vanilla fetch poll, no JS framework. Public view: https://9c438bcc.ht-ml.app/ |

school-core NOW ships a [Task Board](#task-board-durable-cloud-board) (kanban) alongside the competency views.

## Task Board (durable cloud board)

A self-contained kanban board that visualizes the GitHub Issue → Task → Review → Done pipeline.

- **Local preview:** Run `python activity_server.py` then open http://localhost:8765/board.
- **Regenerate + publish:** Run `board.py` to produce `board.html`, then publish via `lavish-axi share board.html`.
- **Durability model (one line):** State in Git (`data/`), execution in CI (`.github/workflows/school-loop.yml`), view hosted on [ht-ml.app](https://9c438bcc.ht-ml.app/).

## Layout

```
campus.md            identity + behavioral core
cli.py               entrypoint
routing.py           task → agent routing
scoring.py           gates + score store
director.py          execution + evaluation
adversarial_reviewer.py + lenses/   adversarial review
verify_gate.py + flake.nix          hermetic code-verify shell
issue_bridge.py      GitHub issue → task
engram_adapter.py    optional memory (auto-detected)
leaderboard.py       agent leaderboard (visibility)
config/              anchors, roles, github, escalation config
tests/               377 tests
```

## License

MIT — see [LICENSE](LICENSE).
