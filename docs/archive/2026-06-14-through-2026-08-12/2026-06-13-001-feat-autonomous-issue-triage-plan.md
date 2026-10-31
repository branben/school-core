---
title: "feat: Autonomous Issue Triage — GitHub Fetch, Task Bridge, and PR Creation"
created: 2026-06-13
status: draft
author: Sisyphus
project: agent-school
tags: [github, issues, triage, autonomous]
---

# Autonomous Issue Triage: GitHub Fetch, Task Bridge, and PR Creation

## Problem Frame

The Agent School currently generates only synthetic curriculum tasks (e.g., "Write a pytest test for a palindrome function"). It has no connection to real-world work. The architecture supports it — `triage_classifier.py` classifies issues by category/state, `director.py` routes tasks to scoring-gated agents, `executor.py` runs models — but three bridges are missing:

1. **No issue source**: No code to pull issues from a GitHub repository
2. **No issue→task conversion**: Classified issues are never turned into Director-compatible task prompts
3. **No output channel**: Agent solutions are captured as trajectories but never pushed back as PRs

This plan adds all three bridges so the school can be pointed at any GitHub repo and autonomously triage, solve, and PR issues.

## Scope

### In Scope
- Fetch open issues from a GitHub repo via `gh` CLI (authenticated, paginated)
- Filter through `triage_classifier.py` to find `ready-for-agent` issues
- Convert each qualifying issue into a Director task prompt with domain/difficulty mapping
- Wire the issue source into `autonomous_loop.py` as a new task source
- After a successful task run, create a PR with the agent's solution
- CLI commands for manual fetch/solve/PR as well as autonomous mode

### Out of Scope
- GitHub webhook listener (polling for now; webhook can be added later)
- Multi-repo support (one repo at a time, configured via config)
- PR review/merge (agent creates the PR; human reviews and merges)
- Issue commenting or status updates on the original issue
- Authentication for private repos beyond `gh` CLI auth

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| GitHub access method | `gh` CLI subprocess | No API key to manage; inherits `gh` auth state; works with `GITHUB_TOKEN` or browser login |
| Issue polling strategy | Interval-based (configurable, default 5min) | Simpler than webhooks; adequate for autonomous loop |
| Issue→domain mapping | Title/label keyword rules in `github_fetcher.py` | `triage_classifier` handles category/state; we need a separate mapping from issue type to Director domain (`bug` → `debugging`, `enhancement` → `code-implementation`) |
| PR creation approach | `gh pr create` via subprocess | Same auth reasoning as fetch; accepts title, body, branch, and diff |
| Branch strategy | `school/issue-<number>-<slug>` | Namespaced so human PRs and school PRs are visually distinct |
| Config format | YAML file at `config/github.yaml` | Matches existing convention; stores repo, poll interval, domain overrides |

## Implementation Units

### U1. GitHub Issue Fetcher

**Goal:** Fetch open issues from a GitHub repo, classify them, and return a list of actionable items.

**Dependencies:** None

**Files:**
- `github_fetcher.py` — new: issue fetching, classification, and domain mapping
- `config/github.yaml` — new: repo config and domain mapping overrides
- `tests/test_github_fetcher.py` — new: unit tests with mock `gh` output

**Approach:**
- `fetch_issues(repo: str, labels: list[str] = None) -> list[dict]`:
  1. Run `gh issue list --repo <repo> --state open --json number,title,labels,body --limit 50` via subprocess
  2. Parse JSON output
  3. For each issue: call `triage_classifier.classify_issue(title, labels, body)` to get (category, state)
  4. Filter to state=`ready-for-agent`
  5. Map category + labels to a Director domain via `_map_domain(category, labels)`:
     - `bug` → `debugging`
     - `enhancement` with code keywords → `code-implementation`
     - `enhancement` with test keywords → `python-testing`
     - Default → `_default`
  6. Build a task prompt from the issue title + body
  7. Return list of `{issue_number, title, body, domain, difficulty, prompt}`
- Difficulty mapping: default `medium`; override via `difficulty_overrides` in config for specific issue numbers or label patterns
- `load_config()` reads `config/github.yaml`:
  ```yaml
  repo: owner/repo
  poll_interval_seconds: 300
  labels: ["bug", "enhancement"]
  difficulty_overrides:
    p0: hard
    p1: medium
  domain_overrides:
    security: code-review
  ```
- `list_repos()` convenience — `gh repo list` to show available repos

**Patterns to follow:** `_omniroute_call()` in `executor.py` for subprocess JSON parsing pattern; `triage_classifier.py` for classification calls

**Test scenarios:**
- `fetch_issues()` returns correct number of issues from mock `gh` output
- Issue with `bug` label maps to `debugging` domain
- Issue with `enhancement` + `test` labels maps to `python-testing` domain
- Issue with state `needs-info` is excluded from results
- No issues returned when `gh` CLI is not installed → logged warning, empty list
- Pagination: issues beyond 50 are not fetched (explicit limit, documented)
- `load_config()` returns defaults when config file is missing

### U2. Issue→Task Bridge

**Goal:** Wire fetched issues into the Director's task execution pipeline and integrate into the autonomous loop as a task source.

**Dependencies:** U1 (needs `fetch_issues()`)

**Files:**
- `issue_bridge.py` — new: converts issue to task, runs via Director, returns result
- `autonomous_loop.py` — modify `AutonomousScheduler` to accept issues as a task source
- `tests/test_issue_bridge.py` — new: tests for issue→task conversion

**Approach:**
- `IssueBridge` class:
  ```python
  class IssueBridge:
      def __init__(self, repo: str, store: ScoreStore = None):
          self.repo = repo
          self.store = store or ScoreStore()
          self.seen_issues: set[int] = set()  # track already-processed issue numbers

      def poll(self) -> list[dict]:
          """Fetch new ready-for-agent issues since last poll."""
          issues = fetch_issues(self.repo)
          new_issues = [i for i in issues if i["issue_number"] not in self.seen_issues]
          for i in new_issues:
              self.seen_issues.add(i["issue_number"])
          return new_issues

      def solve(self, issue: dict) -> dict:
          """Run an issue through the Director and return the result."""
          return run_task(
              prompt=issue["prompt"],
              domain=issue["domain"],
              difficulty=issue.get("difficulty", "medium"),
              store=self.store,
          )
  ```
- `autonomous_loop.py` changes:
  - Add `--github-repo` CLI argument
  - In `run_round()`, before curriculum task selection, check the issue bridge for new issues
  - Issues take priority over curriculum tasks (real work > synthetic)
  - Track `seen_issues` in the scheduler to avoid re-processing
  - After a successful solve, store the result for PR creation (U3)

**Patterns to follow:** `run_task()` usage in `autonomous_loop.py` lines 357-363; `ActivityLog` usage pattern

**Test scenarios:**
- `poll()` returns only issues not in `seen_issues`
- `poll()` returns empty list when all issues already seen
- `solve()` calls `run_task()` with correct domain/difficulty
- Bridge logs activity via `get_log()` on each successful solve
- Scheduler picks issue tasks before curriculum tasks when both are available
- Scheduler handles `--github-repo` flag correctly

### U3. PR Creator

**Goal:** Take a solved issue and create a GitHub PR with the agent's solution.

**Dependencies:** U2 (needs the solve result from the bridge)

**Files:**
- `pr_creator.py` — new: create PRs from agent solutions
- `issue_bridge.py` — modify to call PR creation after successful solve
- `tests/test_pr_creator.py` — new: unit tests

**Approach:**
- `create_pr(issue: dict, solution: str, repo: str) -> dict`:
  1. Determine branch name: `school/issue-<number>-<slug>` (slug from title, max 30 chars)
  2. Check if repo is cloned locally at a configurable path (default: `~/.school/workspace/<repo>`)
     - If not cloned: `gh repo clone <repo> <path> -- --depth 1`
  3. Create branch: `git checkout -b <branch>`
  4. Write solution to appropriate file(s): heuristic to determine file path from issue labels/title
     - If issue references a file path → use it
     - Otherwise → `school-agent-fix-<number>.<ext>` or user-configured pattern
  5. `git add` and `git commit -m "school: fix #<number> - <title>"`
  6. `git push origin <branch>` (or `gh` equivalent)
  7. `gh pr create --repo <repo> --title "school: <title>" --body "<body>" --head <branch>`
  8. Return PR URL + number
- PR body template:
  ```
  ## 🤖 School Agent PR

  This PR was autonomously created by the Agent School.

  **Issue:** #{number} — {title}
  **Agent:** {agent_name}
  **Domain:** {domain}
  **Score delta:** {old_score} → {new_score}

  ### Summary

  {solution_summary or first 500 chars of solution}

  ---
  *Created by Agent School autonomous loop*
  ```
- `config/github.yaml` additions:
  - `workspace_path: ~/.school/workspace`
  - `auto_push: true` (disable for dry-run)
  - `file_path_pattern: "school-fix-{number}{ext}"`

**Patterns to follow:** `_a2a_call()` subprocess pattern in `executor.py` for git command execution

**Test scenarios:**
- `create_pr()` constructs correct branch name from issue number and title
- `create_pr()` generates valid PR body template with issue references
- Dry-run mode (`auto_push: false`) does not execute git commands
- Missing `gh` CLI → `ExecutorError` with clear message
- Workspace directory is created if it doesn't exist
- PR URL is returned on success

## Dependencies & Sequencing

```
U1 (GitHub Issue Fetcher)
 └── U2 (Issue→Task Bridge) — needs fetch_issues()
      └── U3 (PR Creator) — needs solve result from bridge
```

All three are sequential — each depends on the previous. No parallelism possible.

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `gh` CLI not authenticated | Medium | Check `gh auth status` before operations; log clear error |
| Agent solution doesn't compile | Medium | PR is a draft — human reviews before merge; add optional compile check |
| Issue can't be solved autonomously | High | Auto-evaluate task score; if < 40, don't create PR, log as failed attempt |
| Branch/workspace conflicts | Low | Namespace with `school/` prefix; workspace per-repo |
| Token/rate limits on `gh` API | Low | `gh` handles auth; pagination limit of 50 prevents excessive calls |

## Future Work (Post-Plan)
- GitHub webhook listener for real-time issue detection
- Issue status updates (comment on issue when PR is created)
- Multi-repo support (configurable list)
- Compile/lint check before PR creation
- PR description including diff summary
