# MEMORY

## OpenHands Cloud (user brandonbennett@pursuit.org)
- Account: `user_c61808ce-9eb0-4d9a-8055-efd4fa84967e_org` (owner). LLM: `openhands/deepseek-v4-flash` (managed key via `openhands/` provider).
- Prior outage root cause: compound-engineering-plugin auto_load crash (PluginManifestError). Mitigated via `auto_load: false`. See daily log 2026-10-11.
- Managed key refresh endpoint: `POST /api/keys/llm/managed/refresh`.
- **Orca** = user's self-hosted agent runtime (worktrees/terminals/automations; Hermes + OmniRoute). Code in `branben/school-core`. Past OpenHands Cloud sessions connected to it via a sandbox pointed at that repo, NOT SSH. How to reconnect: see daily log 2026-10-12.

## wde-mcp-server (this repo)
- Rust MCP server (rmcp fork branch `feat/sep-2640-skills` from `branben/rust-sdk`) exposing the Worst Day Ever skill as skill/tool/resources. HTTP on `127.0.0.1:3001/mcp`, stdio bin `wde-mcp-stdio`.
- Install/verify commands + HTTP-vs-stdio smoke-test details: see daily log 2026-10-12.
- Rust installed via rustup to `$HOME/.cargo`; source `$HOME/.cargo/env` before cargo in new shells. NOTE: env resets may wipe the toolchain; reinstall with `sh rustup-init.sh -y --profile minimal`.
- MCP Inspector v2.10.1: web UI on :6274 (SPA, hard to drive headlessly) or scriptable `--cli --config <f> --server wde`; `--config` must use `{"mcpServers": {...}}` shape. See daily log 2026-10-12.
- SEP-2640 `--verify` conformance: FIXED at the parser level — `serde_yaml` (deprecated, strips trailing `\n` from bare `|` literal blocks) replaced with `yaml-rust2`, fully YAML 1.2 compliant. SKILL.md stays at bare `|` (matches spec). `skills/list --verify` exits 0. PR #1.
- Branch protection on `main` is NOT possible with the `GITHUB_TOKEN` here (GitHub App user token, no branch-protection permission → 403); needs a classic PAT or an App granted the permission.

## rust-sdk upstream PR review (2026-10-14)
- Reviewed upstream `modelcontextprotocol/rust-sdk#1286` (SEP-2640 skills impl by branben). Don't run `git rev-parse --is-shallow-repository` here — that git doesn't support `--is-shallow`; use `git rev-parse --is-shallow` instead. Full findings + spec location in daily log 2026-10-14.

## school-core (branben/school-core) audit remediation (2026-10-14)
- 8/8 audit findings remediated, commit `3838b76` on `openhands-workspace-42FTExBqUGJehgGkuMQKzG` branch. Suite: 2372 pass / 28 skip / 16 deselect. Beads all closed except `21l` (pre-existing OmniRoute feature) + `ky1` (governance files added; git tags deferred).
- Lint: `ruff.toml` (F-class only, py39). CI gate lints 4 core modules (issue_bridge/director/conductor/repo_reader) + PR-changed `.py`. Gotcha: ruff F401 flags module-level imports that are ALSO function-scope imported later (conductor `list_bookbags`) — keep only the function-scope one.
- bd gotchas: `bd comment <id> text...` (positional, no `--message`); `bd close` takes multiple ids; root `.beads.gate.lock` now gitignored; `.beads/` chmod 700. Diagnostics: `python scripts/obsidian_client.py doctor`.
- Utility scripts `regenerate_budgets.py`/`patch_papers.py`/`school_md_generator.py` are WORKING, documented in README. `patch_papers.py` rewrites tracked `docs/site/leaderboard.html`.
- codex-security CI (`codex-security.yml`): output dir + state dir MUST live outside the scanned worktree (use `runner.temp` per-step — it's invalid in workflow-level `env`); output dir needs `chmod 700`; MUST run on `ubuntu-22.04` (24.04 AppArmor blocks bwrap userns). Scan currently fails with "Quota exceeded" — OpenAI billing on the `OPENAI_API_KEY` secret, not a workflow bug. Full debugging trail in daily log 2026-10-14.