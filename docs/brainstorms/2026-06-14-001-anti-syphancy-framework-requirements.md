---
title: "Requirements: Anti-Syphancy Agent Collaboration Framework"
created: 2026-06-14
status: active
type: brainstorm-requirements
origin: docs/thinking/agent-school-framework.md
---

# Requirements: Anti-Syphancy Agent Collaboration Framework

## Problem Frame

AI agent workflows default to sycophancy: the model agrees with you because agreement is rewarded. This works when you know the domain. It fails when you're exploring unfamiliar territory — exactly when you need the model most.

The Agent School system has the infrastructure for agent orchestration (routing, scoring, verification, roles) but lacks the **adversarial collaboration layer** that makes the difference between a tool that executes and a collaborator that challenges.

This document defines the requirements for that layer.

## Scope

### In Scope
- Semantic anchor system — standardized terms that activate known patterns in LLMs
- Adversarial review pipeline — structured challenge at each stage of work
- Role-based agent personification — skills.md as teacher/student/principal/janitor
- Grounded scoring — execution-based verification that measures correctness, not confidence
- Growth measurement — tracking real capability improvement, not just task completion

### Out of Scope
- Model training / LoRA fine-tuning (separate roadmap item)
- Multi-user / team features
- GUI / dashboard (CLI reports only)
- Specific issue processing pipeline (already implemented in issue_bridge.py)

## Requirements

### R1. Semantic Anchor Registry
**What:** A registry of semantic anchors — terms that activate known patterns — organized by domain and difficulty level.

**Why:** Right now, anchors are scattered across prompt_composer.py, triage_classifier.py, and individual skill files. There's no single source of truth for "what does [TDD] mean in this system?" This makes it hard to add new anchors, ensure consistency, or measure which anchors actually improve output.

**Acceptance:**
- A YAML/JSON registry of anchors with: name, domain, activation pattern, expected behaviors, and examples
- Anchors are referenceable by name in prompts (e.g., "Apply [Fagan Inspection]")
- New anchors can be added without code changes
- Anchor usage is logged (which anchors were activated, for which tasks)

### R2. Adversarial Review Pipeline
**What:** Every piece of work (plan, code, analysis) passes through an adversarial reviewer before scoring. The reviewer's job is to find flaws, not to validate.

**Why:** The current verifier scores based on surface structure. An adversarial reviewer catches what the verifier misses: logical gaps, missing edge cases, unstated assumptions.

**Acceptance:**
- After a student produces output, it's dispatched to an adversarial reviewer
- The reviewer has a specific lens (security, correctness, simplicity, etc.) based on the task domain
- The review produces: a verdict (PASS/FAIL), specific gaps, and suggested improvements
- The review score is combined with the execution score for the final rating
- Reviewer identity is logged (which lens, what it found)

### R3. Role-Based Agent Personification
**What:** Every agent interaction is framed by a role with specific rules, standards, and behavioral expectations. Roles are defined in skills.md files.

**Why:** A prompt tells the model what to do. A role tells the model who to be. Roles are persistent across tasks and maintain standards even when individual prompts vary.

**Acceptance:**
- Each agent interaction loads a role definition before the task
- Roles include: name, domain expertise, rules of engagement, evaluation criteria, and escalation conditions
- Role definitions are versioned and auditable
- Role performance is tracked (which roles produce the highest-quality output)

### R4. Grounded Scoring System
**What:** Replace blind LLM verification with execution-based scoring that measures whether the work is actually correct.

**Why:** The current verifier (foundry-coder-1.5b) gives 90/EXCELLENT for "Step 1: Identify the Error Source" because it sees structure, not substance. Grounded scoring measures what matters: does the code work? Do the tests pass? Does it solve the issue?

**Acceptance:**
- For code tasks: run the project's test suite against the student's patch
- For analysis tasks: check claims against the codebase (AST verification)
- For planning tasks: verify plan consistency and completeness
- Scores are: execution-based (0-100), not LLM-estimated
- Failed executions produce specific error messages the student can learn from

### R5. Growth Measurement
**What:** Track real capability improvement over time, not just task completion rates.

**Why:** A student who scores 70 on the same task twice hasn't grown. A student who scores 50 on a harder task has grown more. The scoring system needs to distinguish between performance and learning.

**Acceptance:**
- Track: tasks attempted, tasks succeeded, difficulty progression, time-to-solve, error recurrence
- Growth = improvement on harder tasks, not just more tasks at the same level
- Students have a "capability profile" that shows strengths, weaknesses, and growth trajectory
- The system can answer: "Is this student better than they were last week?"

### R6. Memory-Aware Context Enrichment
**What:** Integrate the existing 4-layer memory architecture (Engram/Serena/CocoIndex) into the student task dispatch pipeline so students receive the right context at the right layer.

**Why:** Currently, `repo_reader.py` provides file tree + keyword-matched files (Layer 1 partial). But students don't get Layer 0 (ambient/domain conventions), Layer 2 (what they learned from similar past issues), or Layer 3 (consolidated patterns from sleep cycles). A student debugging a websocket issue should know: "Last time you saw this pattern, the fix was in the consumer layer" (Layer 3) and "Here's the project's error handling convention" (Layer 0).

**Acceptance:**
- Before dispatch, the context_orchestrator enriches the prompt with Layer 0 + Layer 1 + relevant Layer 3
- After task completion, observations are saved to Layer 2 (Engram)
- On sleep/wake, Layer 2 → Layer 3 consolidation runs
- Context per issue stays under 10000 chars (repo_reader.py already enforces this)

### R7. "I Don't Know" Escalation
**What:** Students can decline tasks they're not ready for, and the system routes accordingly.

**Why:** Right now, every student tries every issue. A 0.5b model attempting a multi-module race condition bug wastes compute and produces garbage. Knowing what you don't know is a skill.

**Acceptance:**
- Before dispatch, the student can assess: "Can I solve this?" with a confidence estimate
- Low-confidence assessments trigger escalation to a stronger model
- Escalation is logged (which issues needed escalation, from which student)
- Over time, escalation rate should decrease as students grow

## Success Criteria

1. **Output quality improves**: Agent responses reference actual codebase files and produce targeted fixes, not generic advice
2. **Scoring becomes meaningful**: Scores correlate with actual correctness (verified by execution), not surface structure
3. **Students grow**: Measurable improvement on harder tasks over time (month-over-month)
4. **Syphancy decreases**: Adversarial reviewers catch >30% of issues that the current verifier misses
5. **Framework is open-sourceable**: The semantic anchor registry, role definitions, and adversarial patterns are documented and reproducible

## Approach Options

### Approach A: Layer on Existing Infrastructure
Add adversarial patterns on top of the current system. Minimal changes to director.py and scoring.py. New components: anchor registry, adversarial reviewer, growth tracker.

**Pros:** Low risk, incremental, ships fast
**Cons:** Inherits current architecture limitations, may hit scaling walls

### Approach B: Redesign Around Adversarial Collaboration
Rebuild the core pipeline with adversarial collaboration as the foundation. Every task flows through: student → adversarial review → grounded scoring → growth update.

**Pros:** Clean architecture, optimal for the collaboration pattern
**Cons:** Higher risk, more code, longer to ship

### Approach C: Hybrid — Start with A, Migrate to B
Ship Approach A as a thin layer. Use the data it generates (what adversarial patterns work, which anchors matter) to design Approach B.

**Pros:** Pragmatic, data-informed migration path
**Cons:** Two migrations, temporary complexity

**Recommendation: Approach C.** Ship the adversarial layer now, learn from the data, then redesign around what works.

## Memory Architecture (Context Engine)

The Agent School's memory system is built on three MCP-backed layers, inspired by the Harness-1 paper's state-externalizing architecture:

### Layer 0 — Ambient (Always Loaded)
**What:** Persistent context — vault structure, domain glossary, project conventions, role definitions.
**Storage:** Obsidian vault (`Knowledge Core/`), loaded via CocoIndex `ccc search`.
**Implementation:** `context_orchestrator.py` → `_cocoindex_context()` (runs `ccc search <prompt>` against the vault).

### Layer 1 — Structural (Codebase Topology)
**What:** File tree, symbol index, import graphs, function signatures.
**Storage:** Serena (LSP-backed symbol search) + CocoIndex (AST-aware semantic search).
**Implementation:** `repo_reader.py` (file tree + keyword matching) can be upgraded to use Serena's `find_symbol` and CocoIndex's `ccc search` for richer context.

### Layer 2 — Episodic (Session History)
**What:** Trajectories, decisions, recent observations from the current session.
**Storage:** Engram (SQLite + FTS5, 21 MCP tools including `mem_store`, `mem_search`, `mem_timeline`).
**Implementation:** `engram_adapter.py` → `save_trajectory()`, `search_trajectories()`.

### Layer 3 — Archival (Compressed Summaries)
**What:** Sleep/wake consolidation artifacts, handoff anchors, long-term patterns.
**Storage:** Obsidian (`engram/` namespace) + Engram's semantic memory (vector embeddings).
**Implementation:** `sleep_state.py` (YAML consolidation), `handoff-protocol.md` (Obsidian anchors).

### The Sleep/Wake Cycle
Inspired by "Language Models Need Sleep" and Engram's REM-style dreaming:
1. **Sleep trigger:** session timeout (15min), context pressure (>70% window), explicit request, or cron.
2. **Consolidation:** Compress Layer 2 (episodic) into YAML summary → Layer 3 (archival). Run Engram's `trigger_rem_cycle` to cluster related memories and extract patterns.
3. **Wake:** Load Layer 0 + Layer 1 for target repo + relevant Layer 3 archives. Resume queued tasks.

### How Students Use Memory
- **Before task:** Load Layer 0 (ambient) + Layer 1 (structural for target repo) → enriched prompt
- **During task:** Record observations to Layer 2 (episodic) via Engram
- **After task:** Consolidate high-value observations to Layer 3 (archival)
- **On wake:** Reload from Layer 3, hydrate with fresh Layer 1 for new repo

## Dependencies / Assumptions

- The issue_bridge.py pipeline is stable and can accept an adversarial review step
- The repo_reader.py codebase context system is working
- At least one model (7b cloud) is capable of adversarial review
- Execution-based scoring requires the target repo to have a test suite
- Engram, Serena, and CocoIndex MCP servers are installed and configured (see `04-Reference/Documentation/MCP Tools Setup - Engram Serena Cocoindex.md`)
- The sleep/wake protocol is implemented (`sleep_state.py`, `handoff-protocol.md`)

## Open Questions

1. **Who writes the adversarial reviewer's lens?** Are lenses hand-authored, or generated from the issue context?
2. **How to prevent adversarial reviewers from becoming sycophants too?** Recursive sycophancy is a real risk.
3. **What's the right granularity for semantic anchors?** Too fine-grained = noise. Too coarse-grained = no activation.
4. **How to measure "genuine" adversarial review vs. going through the motions?** The adversarial reviewer needs its own evaluation.
5. **Should Layer 1 use Serena (symbol) or CocoIndex (semantic) as the primary search?** Serena is exact but language-dependent. CocoIndex is language-agnostic but approximate.
