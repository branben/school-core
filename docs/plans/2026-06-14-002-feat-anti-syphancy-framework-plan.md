---
title: "feat: Anti-Syphancy Agent Collaboration Framework"
created: 2026-06-14
status: completed
author: Sisyphus
project: agent-school
tags: [adversarial, anti-syphancy, semantic-anchors, grounded-scoring, roles, growth]
origin: docs/brainstorms/2026-06-14-001-anti-syphancy-framework-requirements.md
---

# Anti-Syphancy Agent Collaboration Framework

## Problem Frame

AI agent workflows default to sycophancy: the model agrees with you because agreement is rewarded. This works when you know the domain. It fails when you're exploring unfamiliar territory — exactly when you need the model most.

The Agent School system demonstrates this problem acutely. The current verifier (`foundry-coder-1.5b`) gives 90/EXCELLENT for responses like "Step 1: Identify the Error Source" because it sees structure, not substance. Agents produce generic advice ("Open the App. Enter Player Name...") instead of referencing actual codebase files. The scoring system (EMA-based, surface-structured) can't distinguish between a confident-sounding wrong answer and a real fix.

Three root failures:

1. **No adversarial layer**: Every agent (student, teacher, faculty) is incentivized to produce output that looks correct. No mechanism exists to challenge assumptions, find gaps, or cross-validate claims against the actual codebase (see origin: problem frame, lines 13-17).

2. **Scoring is blind**: The verifier scores based on surface structure (bullet points, confident language, rubric alignment) rather than substance (does the code work? does it reference real files?). The EMA formula `new = old * 0.7 + task_score * 0.3` converges to a number that reflects confidence, not correctness (see origin: R4, lines 70-80).

3. **No growth signal**: A student who scores 70 on the same task twice hasn't grown. A student who scores 50 on a harder task has grown more. The current system can't make this distinction because it tracks performance on a flat scale, not capability progression across difficulty levels (see origin: R5, lines 82-91).

## Scope

### In Scope
- R1: Semantic Anchor Registry — YAML anchor registry replacing scattered bracket tags
- R2: Adversarial Review Pipeline — structured challenge at each stage of work
- R3: Role-Based Agent Personification — skills.md as versioned role definitions
- R4: Grounded Scoring — execution-based verification (tests, AST, plan consistency)
- R5: Growth Measurement — difficulty progression, capability profiles, trajectory analysis
- R6: Memory-Aware Context Enrichment — 4-layer memory integration into dispatch pipeline
- R7: "I Don't Know" Escalation — student confidence-based task decline

### Out of Scope
- Model training / LoRA fine-tuning (separate roadmap item, see `docs/plans/2026-06-12-001-feat-agent-school-roadmap-plan.md`)
- Multi-user / team features
- GUI / dashboard (CLI reports only)
- Specific issue processing pipeline (`issue_bridge.py` is stable; we add adversarial review as a step within it)
- Cross-model review (using a different model family as reviewer) — deferred to Approach B
- Serena integration for Layer 1 (CocoIndex is primary; Serena is a follow-up effort)

### Dependencies on Other Plans
- **Requires** `docs/plans/2026-06-14-001-feat-real-codebase-context-plan.md` (U1-U3): agents must see real codebase context before adversarial review can be meaningful. The adversarial reviewer needs the same codebase context that the student receives.
- **Parallel with** `2026-06-14-001` U4-U6 (triage integration, CURRICULUM removal, dead code cleanup): no direct dependency, but clean integration requires CURRICULUM to be gone.

## Requirements Traceability

| Requirement | Description | Success Criteria | Implementation Units |
|-------------|-------------|-----------------|---------------------|
| R1 | Semantic Anchor Registry | Anchors are in a single YAML registry, referenceable by name, addable without code changes, usage-logged | U1 |
| R2 | Adversarial Review Pipeline | Every output passes adversarial review; reviewer has domain-specific lens; verdict + gaps produced; score combined with execution score | U2, U3 |
| R3 | Role-Based Agent Personification | Roles defined in versioned files; each dispatch loads role definition; roles include rules, criteria, escalation conditions | U4 |
| R4 | Grounded Scoring | Code tasks run test suite; analysis tasks verify claims against AST; plans checked for consistency; scores execution-based | U5 |
| R5 | Growth Measurement | Track difficulty progression, capability profiles, time-to-solve, error recurrence; system answers "is this student better?" | U6 |
| R6 | Memory-Aware Context Enrichment | Dispatch enriches with Layer 0 + Layer 1 + Layer 3; observations saved to Layer 2; consolidation runs on sleep/wake | U7 |
| R7 | "I Don't Know" Escalation | Students can decline tasks; low-confidence triggers escalation to stronger model; escalation logged; rate decreases over time | U8 |

## Key Technical Decisions

### TD1. Approach C — Ship Thin Layer First, Then Redesign

**Choice:** Start with Approach A (adversarial patterns on existing infrastructure), gather data on which patterns work, then migrate to Approach B (core pipeline redesigned around adversarial collaboration).

**Rationale:** The current system has 84 trajectories and an existing orchestration pipeline. Layering adversarial review on top of `issue_bridge.py` and `director.py` is lower risk than a full redesign. The data generated (which adversarial lenses catch real issues, which anchors improve output, how students respond to challenge) informs the Approach B architecture. Approach A is the plan's scope; Approach B is a future plan triggered by data.

**Risk:** Approach A inherits current architecture limitations (single-point orchestration in `director.py`, sequential candidate iteration). Mitigated by keeping the adversarial layer as a **StaffPlugin** (the existing extensibility model), which can be extended later without touching core orchestration.

### TD2. Adversarial Review as a Staff Plugin + Post-Process Step

**Choice:** Implement the adversarial reviewer as a **StaffPlugin** (`staff/plugins/adversarial_reviewer.py`) and as a **post-process step in `issue_bridge.py`** between student output and scoring.

**Rationale:** The StaffPlugin system (`StaffPlugin` ABC, `StaffSandbox`, `PluginTrust`) is the established extensibility model. Three existing plugins (janitor, score-auditor, session-manager) demonstrate the pattern. The adversarial reviewer needs:
- Access to the student's output (run after dispatch)
- Access to the codebase context (same as what the student received)
- The ability to modify scores (PluginTrust.VERIFIED for score influence)
- Logging of findings (via existing activity_log)

Running as both a plugin AND a post-process step gives flexibility: the plugin handles the "review and flag" pattern (staff maintenance rounds), while the post-process step runs in the critical path of every task (ensuring no output goes un-reviewed).

### TD3. Three Adversarial Lenses for Approach A

**Choice:** Ship with three hand-authored adversarial lenses: **Correctness**, **Security**, **Completeness**.

**Rationale:** External research (adverse, hermaguard, adversarial-reviewer) consistently finds 3-4 personas optimal — enough for diverse perspectives, few enough to avoid analysis paralysis. These three cover the highest-value axes:

| Lens | Focus | Maps To |
|------|-------|---------|
| **Correctness** | Does the solution actually fix the issue? Are there logic errors, off-by-one, wrong assumptions? | R2 acceptance: verdict + gaps |
| **Security** | Are there injection points, trust boundary violations, data exposure? (OWASP-informed) | R2 acceptance: domain-specific lens |
| **Completeness** | Are edge cases handled? Missing imports? Incomplete fix? Unstated assumptions? | R2 acceptance: suggested improvements |

A fourth lens (Simplicity / YAGNI) is deferred to Approach B — it requires understanding the codebase's existing style, which the real-codebase-context plan provides.

Lens selection is deterministic based on task domain: code tasks get Correctness + Security + Completeness; planning tasks get Correctness + Completeness; analysis tasks get Correctness + Completeness.

### TD4. Anchor Registry Schema — Three-Tier Categorization

**Choice:** Three anchor tiers: **Methodology**, **Principle**, **Technique**.

**Rationale:** The existing `prompt_composer.py` already uses anchors informally across these tiers (e.g., `[Fagan Inspection]` = methodology, `[YAGNI]` = principle, `[Five Whys]` = technique). External research confirms MECE categorization as best practice for knowledge organization in LLM prompting.

```
Anchor:
  name: str           # "Fagan Inspection"
  tier: enum          # methodology | principle | technique
  domain: str         # "code-review", "planning", "debugging"
  activation_pattern: str  # What the model should do when this anchor is activated
  expected_behaviors: list[str]  # Observable behaviors that confirm activation
  examples: list[str]  # Concrete examples of the anchor in action
  difficulty_gate: str  # minimum gate to use this anchor ("easy" | "medium" | "hard")
```

### TD5. Grounded Scoring — Tiered Architecture

**Choice:** Three-tier scoring: execution (deterministic) → heuristic (structural) → LLM-as-judge (subjective only as fallback).

**Rationale:** External research consistently shows LLM-as-judge scores every model 8-9/10 while execution scoring shows actual variance (4, 6, 7). The tiered approach uses the right tool for each signal. This is not a replacement for the existing EMA system — it **feeds** it. The grounded score becomes the `task_score` that EMA updates against.

| Tier | What | How | Cost | Example |
|------|------|-----|------|---------|
| 1. Execution | Does it work? | Run tests, compile, AST parse | ~$0 | Patch passes test suite |
| 2. Heuristic | Is it well-structured? | Lint, complexity, token count, grounding check | ~$0 | References real files, not hallucinated |
| 3. LLM-as-Judge | Is it stylistically appropriate? | Structured rubric, categorical scoring | Small | Tone, formatting (only when Tiers 1-2 pass) |

**Critical:** When Tier 1 is unavailable (e.g., analysis tasks, planning tasks), Tier 2 becomes the primary signal. Tier 3 is never the sole signal for a score. This prevents the Step-1-Identify-Error-Source problem where the LLM judge rewards structure over substance.

### TD6. Growth = Difficulty-Adjusted Performance

**Choice:** Growth is measured as improvement on tasks of increasing difficulty, not just higher scores on the same tasks.

**Rationale:** A student scoring 70 on 10 easy tasks has not grown. A student scoring 50 on a hard task has grown more. The growth metric must incorporate task difficulty.

```
growth_score = Σ(difficulty_weight_i × score_i) / Σ(difficulty_weight_i)
where difficulty_weight = f(files_touched, test_failures, historical_fix_size, complexity)
```

This is tracked per agent per time window (weekly). The system can answer: "Is Ada better at hard tasks this week than last week?" (see origin: R5 acceptance).

### TD7. Memory Layer Strategy — CocoIndex Primary, Engram Already Wired

**Choice:** For R6, use existing CocoIndex (`ccc search`) for Layer 0 + Layer 1, existing Engram adapter for Layer 2, and add a new `consolidation_writer.py` for Layer 3 integration with sleep/wake.

**Rationale:** `context_orchestrator.py` already wires CocoIndex and Engram. Layer 3 (archival) is partially implemented via `sleep_state.py` and the library log. The main gap is: observations are saved to Layer 2 (Engram) after tasks but not consolidated to Layer 3 (YAML archival). This is the smallest delta to close.

Serena (symbol search) is not currently integrated and would be a separate effort. For Approach A, CocoIndex's AST-aware semantic search is sufficient for Layer 1 context.

### TD8. Confidence-Based Escalation via Pre-Dispatch Assessment

**Choice:** Before dispatch, the routing layer includes a lightweight "readiness check" — the student model produces a 1-line confidence estimate. Below threshold, route to next-stronger model.

**Rationale:** This is the cheapest form of "I don't know" — no full inference needed. The routing layer (`director.py` candidate selection) already iterates through agents by score gate. Adding a confidence pre-check is a small delta: the first agent in the candidate list produces a confidence estimate. If it's below the threshold for the task's difficulty gate, skip to the next candidate.

This avoids the "0.5b attempts a multi-module race condition" waste described in the requirements (see origin: R7, lines 104-113).

## High-Level Technical Design

### Adversarial Pipeline Flow

The adversarial collaboration layer sits between student output and scoring. Every task flows through: **Student → Adversarial Review → Grounded Scoring → Growth Update**.

```
┌──────────────────────────────────────────────────────────────────┐
│                     ISSUE BRIDGE (existing)                       │
│  github_fetcher → triage_classifier → issue_bridge               │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                  DIRECTOR (existing)                         │ │
│  │  prompt_composer → context_orchestrator → executor          │ │
│  │                                                              │ │
│  │  [NEW] Pre-dispatch confidence check (R7)                   │ │
│  │  [NEW] Role definition loading (R3)                         │ │
│  │  [NEW] Anchor registry lookup (R1)                          │ │
│  │                                                              │ │
│  │  Student produces output ──────────┐                        │ │
│  └────────────────────────────────────┼────────────────────────┘ │
│                                       ▼                          │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              ADVERSARIAL REVIEWER (new)                   │    │
│  │                                                          │    │
│  │  1. Receive: student output + codebase context + task    │    │
│  │  2. Select lens: Correctness / Security / Completeness   │    │
│  │  3. Review: structured critique (PASS/FAIL + gaps)       │    │
│  │  4. Circuit breaker: if reviewer agrees, escalate to     │    │
│  │     second opinion (different lens or model)             │    │
│  │  5. Output: verdict, gaps, suggested improvements        │    │
│  │                                                          │    │
│  │  [NEW] staff/plugins/adversarial_reviewer.py             │    │
│  │  [NEW] issue_bridge.py post-process step               │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                       │                          │
│                                       ▼                          │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              GROUNDED SCORING (new)                       │    │
│  │                                                          │    │
│  │  Tier 1: Execution (test suite, compile, AST parse)       │    │
│  │  Tier 2: Heuristic (lint, grounding check, complexity)   │    │
│  │  Tier 3: LLM-as-Judge (style only, fallback)             │    │
│  │                                                          │    │
│  │  Combined: execution_score × 0.5 + review_score × 0.3    │    │
│  │           + heuristic_score × 0.2                         │    │
│  │                                                          │    │
│  │  [NEW] scoring.py: GroundedScoreCalculator                │    │
│  │  [MOD] scoring.py: EMA update uses grounded score         │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                       │                          │
│                                       ▼                          │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              GROWTH + MEMORY (new)                        │    │
│  │                                                          │    │
│  │  [NEW] GrowthTracker: difficulty-adjusted scoring (R5)   │    │
│  │  [NEW] consolidation_writer.py: Layer 2→3 (R6)          │    │
│  │  [MOD] engram_adapter.py: richer observations (R6)       │    │
│  │  [NEW] EscalationLog: track "I don't know" rate (R7)     │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

### Circuit Breaker for Recursive Sycophancy

```
After adversarial review:
  if verdict == PASS and findings == 0:
    # Possible sycophancy — escalate
    second_opinion = dispatch_reviewer(different_lens_or_model)
    if second_opinion.verdict == PASS:
      log_syndrome("double_pass", task_id, reviewer_id)
      # Accept but flag for recalibration
    else:
      # Use second opinion's findings
      review = second_opinion
  
  # Track agreement rate per reviewer
  agreement_rate = reviewer.stats.pass_rate_last_50_tasks
  if agreement_rate > 0.85:
    flag_for_recalibration(reviewer_id)
```

## Implementation Units

### U1. Semantic Anchor Registry

**Goal:** Create a single YAML registry of semantic anchors that replaces the scattered bracket tags currently spread across `prompt_composer.py`, `issue_bridge.py`, and individual skill files (see origin: R1).

**Requirements:** R1

**Dependencies:** None (standalone data file + loader)

**Files:**
- `config/anchors.yaml` (new) — anchor registry with three tiers (methodology, principle, technique)
- `anchor_loader.py` (new) — load, validate, and query anchors by domain/tier/difficulty
- `prompt_composer.py` (modify) — replace hardcoded anchor dicts with registry lookups
- `tests/test_anchor_loader.py` (new)

**Approach:**
- `config/anchors.yaml` contains all anchors with schema from TD4 (name, tier, domain, activation_pattern, expected_behaviors, examples, difficulty_gate)
- `anchor_loader.py` provides `get_anchors(domain, tier, difficulty_gate) -> list[Anchor]` and `get_anchor(name) -> Anchor`
- `prompt_composer.py` replaces `DOMAIN_ANCHORS`, `DIFFICULTY_ANCHORS`, `ROLE_ANCHORS` dicts with calls to `anchor_loader.get_anchors()`
- Existing bracket notation (`[Fagan Inspection]`, `[YAGNI]`) is preserved — the registry is the source of truth for what gets composed into prompts
- Migration: extract all existing anchors from `prompt_composer.py` into the YAML file, verify identical behavior

**Patterns to follow:** `config/github.yaml` and `curricula/index.yaml` already use YAML config with loader patterns.

**Test scenarios:**
- Happy path: `get_anchors("code-review", "methodology", "hard")` returns Fagan Inspection and related methodology anchors
- Tier filtering: `get_anchors("code-review", "principle", "easy")` returns only principle-tier anchors appropriate for easy difficulty
- Domain filtering: `get_anchors("planning", None, "medium")` returns planning-domain anchors across all tiers
- Missing domain: `get_anchors("nonexistent", None, "easy")` returns empty list, no error
- Backward compatibility: `prompt_composer.compose_prompt()` produces identical output before and after migration (golden test)
- New anchor added to YAML without code change: appears in next `get_anchors()` call
- Invalid YAML: `anchor_loader.load()` raises clear validation error with line number

### U2. Adversarial Reviewer — Core Engine

**Goal:** Build the adversarial review engine that takes a student's output, applies a domain-specific lens, and produces a structured verdict with gaps and suggested improvements (see origin: R2).

**Requirements:** R2

**Dependencies:** U1 (anchor registry for lens prompt composition)

**Files:**
- `adversarial_reviewer.py` (new) — core review engine with three lenses
- `lenses/correctness.py` (new) — Correctness lens definition
- `lenses/security.py` (new) — Security lens definition (OWASP-informed)
- `lenses/completeness.py` (new) — Completeness lens definition
- `tests/test_adversarial_reviewer.py` (new)
- `tests/lenses/test_correctness.py` (new)
- `tests/lenses/test_security.py` (new)
- `tests/lenses/test_completeness.py` (new)

**Approach:**
- `adversarial_reviewer.py` provides `review(output, task, codebase_context, lenses) -> ReviewResult`
- `ReviewResult` is a dataclass: `verdict: PASS | FAIL`, `gaps: list[Finding]`, `suggestions: list[str]`, `confidence: float`, `lens_used: str`
- `Finding` is a dataclass: `section: str`, `issue_class: str`, `severity: CRITICAL | HIGH | MEDIUM | LOW`, `citation: str` (specific line/reference)
- Each lens is a prompt template + structured output schema. The lens defines what the reviewer looks for and how it reasons.
- Lens selection is deterministic: code tasks → Correctness + Security + Completeness; planning → Correctness + Completeness; analysis → Correctness + Completeness
- The reviewer receives: (1) original task/issue, (2) student output, (3) codebase context (same as student), (4) lens definition
- Output is structured JSON only — no free-form prose (Generator-Critic separation contract from external research)
- Circuit breaker: if verdict == PASS with 0 findings, escalate to second lens for cross-validation

**Patterns to follow:** `issue_bridge.py:verify_task_output()` already produces structured JSON verdicts — same output format. The adversarial reviewer is a stricter, more domain-specific version.

**Test scenarios:**
- Happy path: student output with a logic error → Correctness lens returns FAIL with specific gap citing the error
- Security: student output with string interpolation in SQL → Security lens returns FAIL with injection finding
- Completeness: student output missing edge case handling → Completeness lens returns FAIL with missing case
- Clean output: correct, secure, complete solution → all three lenses return PASS
- Empty output: student returns nothing → verdict FAIL, gaps = ["No output produced"]
- Hallucinated files: student references non-existent files → Correctness lens returns FAIL with grounding finding
- Lens selection: code task gets 3 lenses, planning task gets 2 lenses (no Security)
- Circuit breaker: first lens returns PASS/0-findings → second lens is dispatched, its verdict used
- Recursive sycophancy: both lenses return PASS/0-findings → logged as "double_pass" syndrome, accepted but flagged

### U3. Adversarial Reviewer — Integration Points

**Goal:** Wire the adversarial reviewer into the task dispatch pipeline as both a post-process step (critical path) and a StaffPlugin (maintenance rounds) (see TD2).

**Requirements:** R2

**Dependencies:** U2 (adversarial reviewer core engine)

**Files:**
- `issue_bridge.py` (modify) — add adversarial review step between `run_task()` and `verify_task_output()`
- `staff/plugins/adversarial_reviewer.py` (new) — StaffPlugin wrapper for maintenance rounds
- `director.py` (modify) — pass adversarial review results to `evaluate_and_update()`
- `activity_log.py` (modify) — add `ActivityType.ADVERSARIAL_REVIEW` event type
- `tests/test_issue_bridge.py` — add adversarial review integration tests

**Approach:**
- In `issue_bridge.py:bridge_issues()`, after `run_task()` returns a trajectory:
  1. Call `adversarial_reviewer.review(trajectory.output, issue, codebase_context, lenses)`
  2. Attach `ReviewResult` to the trajectory
  3. If verdict == FAIL: include gaps in the trajectory's feedback for potential retry
  4. Pass `ReviewResult` to `evaluate_and_update()` as an additional scoring signal
- In `director.py:evaluate_and_update()`:
  1. Combine adversarial review score with execution score: `combined = execution_score * 0.5 + review_score * 0.3 + heuristic_score * 0.2`
  2. Use combined score for EMA update
  3. Log adversarial review findings to activity log
- StaffPlugin `adversarial_reviewer.py`:
  - Runs in staff maintenance rounds (every 5 rounds per existing schedule)
  - Reviews recent trajectories that scored high but may have been falsely inflated
  - Flags score/trajectory mismatches (complements existing `score_auditor.py` plugin)
  - Trust level: VERIFIED (can influence scores with delta limits)

**Patterns to follow:** `staff/plugins/score_auditor.py` already detects score/trajectory mismatches — the adversarial reviewer extends this with structured critique. `staff/plugins/janitor.py` demonstrates the StaffPlugin ABC pattern.

**Test scenarios:**
- Happy path: trajectory passes adversarial review → score updated normally
- Score adjustment: trajectory fails adversarial review → combined score is lower than execution-only score
- StaffPlugin round: adversarial reviewer plugin reviews recent high-scoring trajectories, flags 2 as potentially inflated
- Missing codebase context: adversarial reviewer receives empty context → still reviews based on output + task only, logs warning
- Reviewer timeout: adversarial review exceeds timeout → trajectory proceeds with execution-only score, review logged as "incomplete"

### U4. Role-Based Agent Personification

**Goal:** Define agent roles as versioned, auditable files that are loaded before each dispatch, giving each agent interaction a persistent identity with rules, standards, and behavioral expectations (see origin: R3).

**Requirements:** R3

**Dependencies:** U1 (anchor registry for role-specific anchor selection)

**Files:**
- `config/roles/student.yaml` (new) — student role definition
- `config/roles/teacher.yaml` (new) — teacher role definition
- `config/roles/faculty.yaml` (new) — faculty role definition
- `role_loader.py` (new) — load role definitions, resolve role from agent score gate
- `director.py` (modify) — load role definition before dispatch, inject into system prompt
- `tests/test_role_loader.py` (new)

**Approach:**
- Each role YAML contains: name, domain_expertise, rules_of_engagement (list of behavioral rules), evaluation_criteria (what "good" looks like for this role), escalation_conditions (when to escalate to a stronger role)
- `role_loader.py` provides `get_role(agent_score, domain) -> Role` — maps score gate to role (student/teacher/faculty) and loads the corresponding YAML
- `director.py:run_task()` calls `role_loader.get_role()` before `prompt_composer.compose_prompt()` and injects the role definition as a new section in the system prompt
- Role definitions are versioned (YAML frontmatter with version + date) and auditable (git-tracked)
- The existing `ROLE_ANCHORS` in `prompt_composer.py` are preserved but supplemented with the full role definition

**Patterns to follow:** `config/anchors.yaml` (U1) uses the same YAML config pattern. The school metaphor (Student → Teacher → Faculty) is already established in `scoring.py` gate thresholds and `leaderboard.py` role names.

**Test scenarios:**
- Happy path: agent with score 15 → student role loaded, system prompt includes student rules
- Gate boundary: agent with score 25 → teacher role loaded (not student)
- Domain specialization: teacher role for code-review domain includes code-review-specific rules
- Missing role file: `role_loader.get_role()` raises clear error with role name
- Role versioning: YAML frontmatter includes `version: 1.0.0` and `updated: 2026-06-14`
- Backward compatibility: without role_loader, `director.py` falls back to existing `ROLE_ANCHORS` behavior

### U5. Grounded Scoring System

**Goal:** Replace blind LLM verification with execution-based scoring that measures whether the work is actually correct, using a three-tier architecture (execution → heuristic → LLM-as-judge) (see origin: R4, TD5).

**Requirements:** R4

**Dependencies:** U2 (adversarial review scores feed into the combined score)

**Files:**
- `scoring.py` (modify) — add `GroundedScoreCalculator` class, update `evaluate_and_update()` to use tiered scoring
- `execution_scorer.py` (new) — Tier 1: run tests, compile, AST parse
- `heuristic_scorer.py` (new) — Tier 2: lint, grounding check, complexity metrics
- `tests/test_grounded_scoring.py` (new)
- `tests/test_execution_scorer.py` (new)
- `tests/test_heuristic_scorer.py` (new)

**Approach:**
- `GroundedScoreCalculator` provides `calculate(output, task, codebase_context) -> GroundedScore`
- `GroundedScore` is a dataclass: `execution_score: float | None`, `heuristic_score: float | None`, `llm_score: float | None`, `combined: float`, `details: dict`
- **Tier 1 (Execution):** For code tasks, write the patch to a temp file, run the project's test suite against it. Score = pass_rate (0.0-1.0). If no test suite exists, Tier 1 returns None.
- **Tier 2 (Heuristic):** Always runs. Checks: (a) grounding — does the output reference real files from the codebase? (b) lint — does the code pass basic syntax checks? (c) complexity — is the solution reasonably sized? Score = weighted average of checks.
- **Tier 3 (LLM-as-Judge):** Only runs when Tier 1 or Tier 2 produces a score below threshold (subjective quality check). Uses structured rubric with categorical scoring (incorrect/partially correct/almost correct/correct), never a single number.
- Combined score: `combined = (exec or 0.5) * 0.5 + heuristic * 0.3 + (llm or 0.5) * 0.2`
- The combined score feeds into the existing EMA formula in `scoring.py:evaluate_and_update()`

**Patterns to follow:** `issue_bridge.py:verify_task_output()` already has a verification flow — the grounded scorer replaces the LLM-only verification with execution-first verification. `scoring.py:ScoreStore` persists scores — the grounded score becomes the `task_score` that EMA updates against.

**Test scenarios:**
- Happy path: patch passes all tests → execution_score = 1.0, combined is high
- Partial pass: patch passes 3/5 tests → execution_score = 0.6
- No test suite: execution_score = None, combined uses heuristic + llm only
- Grounding check: output references non-existent file → heuristic_score penalized
- Syntax error: output has invalid Python → execution_score = 0.0 (compile fails)
- Empty output: all tiers return 0.0
- LLM-as-Judge fallback: Tier 1 unavailable, Tier 2 score = 0.4 → Tier 3 runs, provides categorical score
- Combined score formula: verify weights sum correctly (0.5 + 0.3 + 0.2 = 1.0)
- EMA integration: grounded score feeds into existing `new = old * 0.7 + task_score * 0.3`

### U6. Growth Measurement

**Goal:** Track real capability improvement over time by measuring difficulty-adjusted performance, producing per-agent capability profiles that show strengths, weaknesses, and growth trajectory (see origin: R5, TD6).

**Requirements:** R5

**Dependencies:** U5 (grounded scoring provides the scores to track)

**Files:**
- `growth_tracker.py` (new) — difficulty-adjusted scoring, capability profiles, growth queries
- `scoring.py` (modify) — add `difficulty_weight` to score records
- `leaderboard.py` (modify) — add growth trajectory visualization to HTML leaderboard
- `tests/test_growth_tracker.py` (new)

**Approach:**
- `growth_tracker.py` provides:
  - `calculate_difficulty_weight(task) -> float` — based on files_touched, test_failures, historical_fix_size, complexity
  - `record_performance(agent, task, score, difficulty_weight)` — stores in `data/growth/{agent}.json`
  - `get_capability_profile(agent) -> CapabilityProfile` — strengths, weaknesses, growth trajectory
  - `query_growth(agent, time_window) -> GrowthReport` — "Is this student better than last week?"
- `CapabilityProfile` dataclass: `agent: str`, `strengths: list[str]` (domains with high scores), `weaknesses: list[str]` (domains with low scores), `growth_rate: float`, `difficulty_progression: list[float]`
- `GrowthReport` dataclass: `agent: str`, `period: str`, `tasks_attempted: int`, `tasks_succeeded: int`, `avg_difficulty: float`, `score_trend: float` (positive = improving), `is_better_than_previous: bool`
- Difficulty weight formula: `weight = 1.0 + 0.5 * log(files_touched) + 0.3 * test_failures + 0.2 * complexity_score`
- Growth rate: linear regression of difficulty-weighted scores over time. Positive slope = growing.
- Leaderboard enhancement: add a "Growth" column showing week-over-week trend arrow (↑ ↓ →)

**Patterns to follow:** `scoring.py:ScoreStore` already persists per-agent per-domain scores. `decision_log.py:correlate()` already computes improvement rates by decision type — growth tracking extends this to difficulty-adjusted improvement.

**Test scenarios:**
- Happy path: agent scores 80 on difficulty-2.0 task and 60 on difficulty-3.0 task → growth_score improves
- No growth: agent scores 70 on 10 difficulty-1.0 tasks → growth_rate ≈ 0
- Regression: agent's recent scores are lower than previous period → `is_better_than_previous = false`
- Capability profile: agent has high code-review scores, low planning scores → strengths = ["code-review"], weaknesses = ["planning"]
- Difficulty weight: task touching 5 files with 3 test failures → weight > task touching 1 file with 0 failures
- Empty history: agent with no tasks → profile shows "insufficient data"
- Time window: `query_growth("ada", "7d")` returns last 7 days of performance data

### U7. Memory-Aware Context Enrichment

**Goal:** Integrate the 4-layer memory architecture (CocoIndex Layer 0+1, Engram Layer 2, consolidation Layer 3) into the student task dispatch pipeline so students receive the right context at the right layer (see origin: R6, TD7).

**Requirements:** R6

**Dependencies:** None (memory infrastructure already exists; this is integration work)

**Files:**
- `context_orchestrator.py` (modify) — add Layer 3 (archival) loading, enrich prompt with all relevant layers
- `consolidation_writer.py` (new) — write high-value observations from Layer 2 (Engram) to Layer 3 (YAML archival)
- `engram_adapter.py` (modify) — add richer observation saving (include adversarial review findings, growth signals)
- `sleep_state.py` (modify) — trigger consolidation_writer during sleep sequence
- `tests/test_context_orchestrator.py` (new)
- `tests/test_consolidation_writer.py` (new)

**Approach:**
- **Before dispatch (Layer 0 + Layer 1 + Layer 3):**
  - Layer 0: CocoIndex `ccc search <issue_summary>` → domain conventions, project glossary (already wired)
  - Layer 1: CocoIndex `ccc search <codebase_context>` → file tree, relevant source files (already wired via `repo_reader.py`)
  - Layer 3 (NEW): Load relevant archival summaries from `data/sessions/consolidation/` — past consolidation artifacts related to the target domain
  - Combined context stays under 10000 chars (existing limit in `repo_reader.py`)
- **After task completion (Layer 2):**
  - Save observations to Engram via existing `engram_adapter.save_trajectory()` (already wired)
  - NEW: Include adversarial review findings and grounded score details in the observation
- **During sleep/wake (Layer 2 → Layer 3):**
  - NEW: `consolidation_writer.py` reads recent Engram observations, extracts high-value patterns, writes YAML summaries to `data/sessions/consolidation/`
  - Modify `sleep_state.py` sleep sequence to call `consolidation_writer.write()` as step 4 (after existing Engram sync)
  - On wake, `context_orchestrator.py` loads relevant consolidation YAML files as Layer 3 context

**Patterns to follow:** `context_orchestrator.py` already has the dual-source (CocoIndex + Engram) enrichment pattern. `sleep_state.py` already has the 6-step sleep sequence — consolidation writing is a new step in that sequence.

**Test scenarios:**
- Happy path: dispatch enriches prompt with Layer 0 (vault) + Layer 1 (codebase) + Layer 3 (archival) context
- Layer 3 available: consolidation YAML exists for the domain → included in context
- Layer 3 unavailable: no consolidation YAML → context uses Layer 0 + Layer 1 only, no error
- Context limit: combined context exceeds 10000 chars → Layer 3 truncated first, then Layer 1
- Consolidation: sleep sequence runs consolidation_writer → YAML file created in `data/sessions/consolidation/`
- Engram down: context_orchestrator gracefully degrades to Layer 0 + Layer 1 only (existing behavior)
- CocoIndex down: context_orchestrator gracefully degrades to Layer 2 + Layer 3 only (existing behavior)
- Rich observations: Engram observation includes adversarial review findings and grounded score

### U8. "I Don't Know" Escalation

**Goal:** Allow students to decline tasks they're not ready for via a lightweight pre-dispatch confidence check, routing low-confidence tasks to stronger models (see origin: R7, TD8).

**Requirements:** R7

**Dependencies:** U4 (role definitions include escalation conditions)

**Files:**
- `director.py` (modify) — add confidence pre-check in `run_task()` candidate selection
- `escalation_log.py` (new) — track escalation events, rates per agent
- `tests/test_escalation.py` (new)

**Approach:**
- In `director.py:run_task()`, before dispatching to a candidate agent:
  1. The first candidate agent receives a **confidence check prompt**: "On a scale of 1-10, how confident are you that you can solve this issue? Reply with only a number."
  2. If confidence < threshold for the task's difficulty gate (easy: 3, medium: 5, hard: 7, diploma: 8), skip to next candidate
  3. If all candidates are below threshold, dispatch to the strongest available agent (A2A fallback)
- Thresholds are configurable per domain in `config/escalation_thresholds.yaml`
- `escalation_log.py` tracks: agent, task, confidence, threshold, escalated_to, timestamp
- Escalation rate is computed per agent per week. Rate should decrease as students grow.
- The confidence check is a separate, lightweight model call (not the full task inference) — uses the same agent model but a shorter prompt

**Patterns to follow:** `director.py` already has A2A fallback when all candidates fail (lines 322-329). The confidence pre-check is a "soft fail" before the full dispatch attempt. `autonomous_loop.py` already has weighted agent selection — the confidence check adds a pre-filter.

**Test scenarios:**
- Happy path: agent confidence 8, task difficulty hard (threshold 7) → dispatched normally
- Escalation: agent confidence 4, task difficulty hard (threshold 7) → skipped, next candidate tried
- All decline: all candidates below threshold → dispatched to strongest available (A2A fallback)
- Escalation logging: escalation event recorded with agent, task, confidence, threshold
- Rate tracking: agent with 5 escalations in 10 tasks → escalation_rate = 0.5
- Growth signal: agent's escalation rate decreases from 0.5 to 0.2 over 4 weeks → logged as improvement
- Confidence check timeout: agent doesn't respond to confidence check within 10s → treated as low confidence, escalated
- Invalid confidence: agent replies with non-numeric response → treated as confidence 0, escalated

## Risks

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Adversarial reviewer becomes sycophant (recursive sycophancy) | High | Medium | Circuit breaker (TD3), cross-validation, agreement rate tracking, recalibration flag |
| Grounded scoring unavailable for repos without test suites | Medium | High | Tier 2 (heuristic) becomes primary; Tier 3 (LLM-as-judge) as fallback; no crash |
| Adversarial review adds latency to every task | Medium | High | Review runs in parallel with scoring; timeout cap (30s); async for non-critical path |
| Confidence check adds overhead to routing | Low | Medium | Lightweight prompt (1-line response); 10s timeout; only runs for first candidate |
| Anchor registry migration breaks existing prompts | Medium | Low | Golden tests: verify `compose_prompt()` output identical before/after migration |
| Growth measurement produces noisy signals for new agents | Low | High | Require minimum 10 tasks before computing growth rate; use confidence intervals |
| Role definitions add complexity to prompt composition | Low | Medium | Roles supplement (don't replace) existing ROLE_ANCHORS; fallback to anchors if role missing |

## Dependencies and Assumptions

### External Dependencies
- `foundry` CLI for local model inference (existing, stable)
- `engram` CLI for memory operations (existing, stable)
- `ccc` CLI for CocoIndex vault search (existing, stable)
- `gh` CLI for GitHub operations (existing, stable)
- Target repo must have a test suite for Tier 1 grounded scoring (graceful degradation if not)

### Internal Dependencies
- `2026-06-14-001` (real-codebase-context) U1-U3 should be merged before U2/U3/U5/U7 — adversarial reviewer needs codebase context to review against
- `2026-06-14-001` U5 (CURRICULUM removal) should be merged before U8 — escalation routing works correctly when issue bridge is the sole task source

### Assumptions
- At least one model (7b cloud or 1.5b local) is capable of adversarial review (verified: existing verifier uses 1.5b)
- The existing EMA scoring formula remains; grounded scores feed into it as the `task_score` input
- The StaffPlugin system is stable and can accommodate a new plugin type
- The sleep/wake lifecycle (`sleep_state.py`) is functional and can be extended with a consolidation step

## Open Questions (Deferred to Implementation)

1. **Exact sandbox strategy for Tier 1 execution**: Docker vs subprocess vs tempdir — depends on the target repo's test infrastructure. Defer to implementation of U5.
2. **Consolidation algorithm for Layer 2→3**: How to extract high-value patterns from Engram observations — depends on observation format. Defer to implementation of U7.
3. **Confidence check prompt wording**: The exact phrasing of the "how confident are you" prompt affects calibration. Defer to implementation of U8 with A/B testing.
4. **Lens prompt template optimization**: The exact wording of each adversarial lens's prompt template affects review quality. Defer to implementation of U2 with iteration.

## Success Criteria

1. **Output quality improves**: Agent responses reference actual codebase files (verified by grounding check in Tier 2) and produce targeted fixes, not generic advice (see origin: success criteria #1)
2. **Scoring becomes meaningful**: Scores correlate with actual correctness (verified by execution), not surface structure. Grounded score differentiates between 40/100 and 90/100 in cases where old verifier gave both 90 (see origin: success criteria #2)
3. **Students grow**: Measurable improvement on harder tasks over time (month-over-month growth_rate > 0 for at least 50% of active agents) (see origin: success criteria #3)
4. **Sycophancy decreases**: Adversarial reviewers catch >30% of issues that the current verifier misses (measured by comparing adversarial findings against verifier-passed trajectories) (see origin: success criteria #4)
5. **Framework is open-sourceable**: The semantic anchor registry, role definitions, and adversarial patterns are documented and reproducible (see origin: success criteria #5)

## Approach Migration Path (Approach C)

### Phase 1: Approach A (This Plan)
- Ship thin adversarial layer on existing infrastructure
- 8 implementation units (U1-U8) across ~3-4 weeks
- Data collected: adversarial finding rates, score distributions, escalation rates, growth signals

### Phase 2: Data Analysis (After Phase 1)
- Analyze which adversarial lenses catch the most real issues
- Identify which semantic anchors improve output quality
- Measure actual vs. perceived growth correlation
- Determine if circuit breaker is triggered too often (indicating recursive sycophancy)

### Phase 3: Approach B (Future Plan)
- Redesign core pipeline around adversarial collaboration as the foundation
- Every task flows through: student → adversarial review → grounded scoring → growth update
- Multi-model review (cross-model, not just cross-lens)
- Automated lens generation from issue context
- Integration with trajectory replay for student learning

### Trigger for Approach B Migration
- Approach A has been running for ≥4 weeks with ≥100 tasks reviewed
- Data shows clear patterns in which lenses/anchors work
- Recursive sycophancy rate is <10% (circuit breaker rarely triggered)
