---
title: "feat: Real Codebase Context — Agents Read the Repo Before Writing Code"
created: 2026-06-14
status: draft
author: Sisyphus
project: agent-school
tags: [codebase, context, verification, enrichment, repo-clone]
origin: docs/plans/2026-06-13-001-feat-autonomous-issue-triage-plan.md
---

# Real Codebase Context: Agents Read the Repo Before Writing Code

## Problem Frame

The Agent School processes GitHub issues but agents produce generic advice instead of real fixes. Root cause: agents receive only the issue title + body as their prompt. They have no access to the repo's file tree, existing code, or architecture. A bug report like "Room creation fails with console error" gets a response like "Step 1: Open the App. Step 2: Enter Player Name..." — generic debug steps that ignore the actual codebase.

The verification layer is equally blind. It scores based on surface structure (bullet points, confident language) rather than substance. A response saying "Step 1: Identify the Error Source" scores 90/EXCELLENT because it looks structured, even though it contains zero actual code.

Three specific failures:

1. **No repo access**: `github_fetcher.py` fetches issue metadata only (title, body, labels). No file tree, no source code. The prompt sent to agents is `title + "\n\n" + body`.
2. **Verification is context-blind**: `verify_task_output()` sends the raw prompt + agent response to the verifier. No vault context, no repo structure. The verifier can't tell if the response references real files.
3. **CURRICULUM is a toy box**: 30 static toy problems (`is_palindrome`, `factorial`, `add(a,b)`) that agents have memorized. The EMA converges to ~70 because every task is trivial.

## Scope

### In Scope
- Add repo clone + file-tree extraction to `github_fetcher.py` (or a new `repo_reader.py`)
- Enrich issue prompts with relevant source files (matched by keywords from issue title/body)
- Pass the same codebase context to the verifier so it scores substance over structure
- Wire `triage_classifier` into `issue_bridge` so unlabeled issues get auto-classified
- Delete the CURRICULUM dict from `autonomous_loop.py` — issue bridge becomes the only task source
- Delete `routing.py` (dead code, never imported)
- Clean unused staff imports from `director.py`

### Out of Scope
- PR creation (already planned in 2026-06-13-001, U3)
- LoRA training pipeline (deferred to roadmap)
- Consolidating entry points (follow-up after this works)
- Replacing the EMA scoring formula (separate concern)

## Requirements

### R1. Issue prompts include codebase context
When the issue bridge dispatches a task, the prompt must include:
- Issue title + body (existing)
- Repo file tree (new — top-level + relevant subdirectories)
- Up to 5 relevant source files matched by keyword overlap with the issue title/body (new)

### R2. Verifier receives same context as agent
The verification prompt must include the same codebase context (file tree + relevant source files) so it can evaluate whether the agent's response references real files and matches the codebase's patterns.

### R3. Repo is cloned once per bridge run, not per issue
Clone the repo to a temp directory at the start of `bridge_issues()`. Reuse the clone for all issues in the same run. Clean up after.

### R4. Triage classifier runs on every fetched issue
`issue_bridge` must call `classify_issue()` on every fetched issue and skip issues that return `needs-info` or `needs-triage` state.

### R5. CURRICULUM dict removed
Remove the 140-line CURRICULUM dict and `_pick_task_for_agent()` from `autonomous_loop.py`. The issue bridge becomes the primary task source. The autonomous loop's curriculum mode is removed entirely.

### R6. Dead code removed
- Delete `routing.py` (never imported)
- Remove unused `StaffSandbox`, `StaffLoader`, `StaffContext` imports from `director.py` (move inside `run_staff()` where they're actually used)

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Repo clone strategy | Cache clones in `~/.cache/school-core/repos/<repo_slug>/`; `git pull` on re-use, `git clone --depth 1` on first use; temp dirs cleaned up after each bridge run + stale dirs (>1h) purged on startup | Avoids re-cloning the same repo every 3 minutes; bounded disk usage |
| File selection heuristic | Keyword matching: extract nouns from issue title/body, match against file names and `grep -l` for content matches | No LLM needed for file selection; fast and deterministic |
| Context size limit | Max 5 source files, max 2000 chars each, max 10000 chars total context | Fits within model context windows without crowding the actual task |
| Where to add enrichment | In `issue_bridge.py` before calling `run_task()` — build the enriched prompt, pass it through | Single point of enrichment; both agent and verifier get the same context |
| Triage integration | Call `classify_issue(title, labels, body)` at the top of the issue loop in `bridge_issues()` | Self-healing pipeline; no manual labeling needed |
| CURRICULUM removal | Delete entirely, don't keep as fallback | The issue bridge is the real task source. Keeping curriculum guarantees agents still get toy problems |

## Implementation Units

### U1. Add repo clone + file-tree extraction

**Goal:** Clone the target repo and extract a file tree that agents can reference.

**Files:**
- `repo_reader.py` (new) — `clone_repo(repo_slug) -> Path`, `get_file_tree(repo_path) -> str`, `find_relevant_files(repo_path, keywords, max_files=5) -> list[Path]`
- `tests/test_repo_reader.py` (new)

**Approach:**
- `clone_repo()` uses `subprocess.run(["git", "clone", "--depth", "1", url, tmpdir])` with a temp directory
- `get_file_tree()` runs `git ls-tree -r --name-only HEAD` to get all tracked files, then formats as a tree (similar to `tree` command output)
- `find_relevant_files()` extracts keywords from the issue text (simple noun extraction: words > 3 chars that aren't stop words), then runs `grep -l -i <keyword>` on source files, ranks by match count, returns top N
- Context size limit: 5 files × 2000 chars = 10000 chars max appended to prompt

**Patterns to follow:** `github_fetcher.py` already uses `subprocess` for `gh` CLI calls — same pattern for `git` commands.

**Test scenarios:**
- Happy path: clone a known public repo, verify file tree contains expected files
- Keyword matching: issue about "room creation" matches files containing "room" or "create"
- Context limit: verify truncation when files exceed 2000 chars or total exceeds 10000 chars
- Clone failure: invalid repo slug raises clear error
- Empty repo: repo with no source files returns empty context

### U2. Enrich issue prompts with codebase context

**Goal:** Before dispatching a task, build a prompt that includes the repo file tree and relevant source files.

**Files:**
- `issue_bridge.py` — modify `bridge_issues()` to clone repo, build enriched prompt
- `tests/test_issue_bridge.py` — add tests for enrichment

**Approach:**
- At the top of `bridge_issues()`, after fetching issues, clone the repo via `repo_reader.clone_repo(repo_slug)`
- For each issue, call `repo_reader.get_file_tree(repo_path)` and `repo_reader.find_relevant_files(repo_path, issue_text_keywords)`
- Build a context block:
  ```
  ## Codebase Context

  ### File Tree
  {tree}

  ### Relevant Files
  #### {file_path}
  {file_content_truncated}
  ```
- Prepend this context to `issue["prompt"]` before passing to `run_task()`
- Pass the same context string to `verify_task_output()` as a new `codebase_context` parameter

**Test scenarios:**
- Issue about "room creation" gets room-related files in context
- Issue with no keyword matches still gets file tree (no relevant files section)
- Context block is prepended correctly to the prompt
- Total context stays under 10000 chars

### U3. Pass codebase context to the verifier

**Goal:** The verifier evaluates agent output against the actual codebase, not just the issue text.

**Files:**
- `issue_bridge.py` — modify `verify_task_output()` signature and `VERIFICATION_PROMPT_TEMPLATE`
- `tests/test_issue_bridge.py` — add tests for context-aware verification

**Approach:**
- Add `codebase_context: str = ""` parameter to `verify_task_output()`
- Update `VERIFICATION_PROMPT_TEMPLATE` to include a new section:
  ```
  [CODEBASE CONTEXT]
  {codebase_context}
  ```
- Update the call site in `bridge_issues()` to pass the context
- The verifier can now check: "Does the response reference actual files from the codebase? Does it match the existing code patterns?"

**Test scenarios:**
- Agent response referencing real files from context → higher score
- Agent response with generic advice (no file references) → lower score
- Empty codebase context → verifier behaves as before (backward compatible)

### U4. Wire triage_classifier into issue_bridge

**Goal:** Auto-classify fetched issues so unlabeled ones don't get dispatched.

**Files:**
- `issue_bridge.py` — add triage call before dispatch

**Approach:**
- Import `classify_issue` from `triage_classifier`
- In the issue loop, call `classify_issue(title, labels, body)` and skip issues where state is not `ready-for-agent`
- Log skipped issues for visibility

**Test scenarios:**
- Issue with "ready-for-agent" label → processed
- Issue with "needs-info" label → skipped
- Issue with no labels but clear bug keywords → classified as bug/ready-for-agent
- Issue with no labels and vague body → classified as needs-triage, skipped

### U5. Delete CURRICULUM dict and task picker from autonomous_loop

**Goal:** Remove the toy problem source so the issue bridge is the only task source.

**Files:**
- `autonomous_loop.py` — delete CURRICULUM dict (lines 58-197), delete `_pick_task_for_agent()` (lines 215-282)

**Approach:**
- Delete the CURRICULUM dict entirely
- Delete `_pick_task_for_agent()` method
- Remove the `issue_mode` flag from AutonomousScheduler — the loop only runs curriculum rounds now
- If no issues available, the bridge logs "no actionable issues" and exits cleanly

**Test scenarios:**
- `autonomous_loop.py` no longer contains CURRICULUM reference
- `autonomous_loop.py` no longer contains `_pick_task_for_agent`
- `pytest tests/` passes with no import errors

### U6. Delete routing.py and clean dead imports

**Goal:** Remove dead code that creates confusion about where routing decisions live.

**Files:**
- `routing.py` — delete entire file
- `director.py` — move `StaffSandbox`, `StaffLoader`, `StaffContext` imports inside `run_staff()` function body

**Approach:**
- Delete `routing.py` (76 lines, never imported)
- Move the three staff imports from top-level (director.py:13-15) to inside `run_staff()` where they're already used via lazy imports (director.py:439-440)
- Run `grep -r "routing" .` to confirm no remaining references

**Test scenarios:**
- `pytest tests/` passes
- `grep -r "from routing import"` returns zero results
- `grep -r "StaffSandbox"` only appears inside `run_staff()`

## Execution Order

1. **U1** (repo_reader) — foundation, everything else depends on it
2. **U4** (triage wiring) — independent, can run in parallel with U1
3. **U2** (prompt enrichment) — depends on U1
4. **U3** (verifier context) — depends on U2, tightly coupled
5. **U5** (CURRICULUM removal) — independent cleanup
6. **U6** (dead code cleanup) — independent cleanup

Recommended parallelization: U1 + U4 in parallel, then U2 + U3 together, then U5 + U6 together.

## Risks

| Risk | Mitigation |
|------|------------|
| `git clone` fails (network, private repo) | Catch exception, fall back to issue-only prompt (no codebase context). Log warning. |
| Context too large for model window | Hard limit: 5 files × 2000 chars. Truncate with notice. |
| `ccc` CLI not installed for CocoIndex | `enrich_prompt()` already handles this gracefully (returns empty string). No change needed. |
| File selection returns irrelevant files | Keyword matching is heuristic. The file tree alone gives agents enough orientation. |
| Removing CURRICULUM breaks existing tests | Update tests to use issue-bridge mode instead of curriculum mode. |

## Open Questions

1. **Private repo access**: Does the `gh` CLI have access to clone private repos? If not, should we fall back to GitHub API for file contents?
2. **Large repos**: For repos with 1000+ files, `git ls-tree` output could be huge. Should we limit tree depth or filter by file extension?
