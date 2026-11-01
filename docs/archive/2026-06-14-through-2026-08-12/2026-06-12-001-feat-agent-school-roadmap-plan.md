---
title: "feat: Agent School Roadmap — EFC Scoring, AutoHarness, SkillOpt, and Beyond"
created: 2026-06-12
status: draft
author: Sisyphus
project: agent-school
tags: [scoring, curriculum, lora, benchmarks, sleep-wake]
---

# Agent School Roadmap: EFC Scoring, AutoHarness, SkillOpt, and Beyond

## Problem Frame

Agent School's core infrastructure (scoring, routing, director, trajectory) is functional but uses **simple heuristics** where the research literature points to more effective approaches:

1. **Scoring** is a flat EMA (70/30) with no awareness of trajectory informativeness, task validity, or retention — the EFC criteria from recent agent research
2. **Curricula** are hand-authored YAML — can't scale beyond 3 domains without automation
3. **No cost awareness** — high-scoring but expensive models get chosen as often as cheap adequate ones
4. **No compliance tracking** — the Director routes but never verifies the agent actually did the task
5. **No training pipeline** — trajectories accumulate in Engram but never drive model improvement via LoRA
6. **No state serialization** — the entire system state lives in process memory; no sleep/wake lifecycle
7. **No benchmarks** — no standardized way to compare agent quality across models or domains

## Scope

### In Scope
- Replace EMA scoring with EFC formula (Informative × Valid × Retained per trajectory)
- Add cost-aware model selection to the router
- Build AutoHarness: auto-generate curriculum YAML from high-scoring trajectories
- Add plan-compliance tracking to the Director
- Build SkillOpt LoRA pipeline (trajectory → training data → LoRA fine-tune)
- Implement Sleep/Wake state serialization via Engram temporal layers
- Integrate HAL-style standardized benchmarks with cost-aware leaderboard

### Out of Scope (for this plan)
- MCP server changes (the Hermes MCP layer is stable)
- Hermes core model routing (this is Agent School, not the Hermes provider layer)
- GUI or dashboard (CLI reports only)
- Multi-user or team features

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| EFC scoring | `I×V×R` per trajectory, EMA on decomposed factors | Matches agent research; more nuanced than flat task_score EMA |
| Cost awareness | Latency $ proxy (model tier: free/ollama/foundry/cloud) | No real API cost data; tiers are a useful approximation |
| Trajectory source | `trajectories_for_training()` with min_score > 50 | Already implemented; leverages existing data pipeline |
| Curriculum output | YAML format matching existing `code-review.yaml` | No changes needed to curriculum parser or index |
| LoRA backend | Unsloth via subprocess | Pure stdio integration; no Python dependency in school-core |
| State serialization | JSON snapshot → Engram observations | Engram is already wired; no new MCP servers needed |
| Benchmark format | JSON lines: `{model, domain, score, latency, trajectory_ref}` | Matches HAL paper format; easy to aggregate |

## Implementation Units

### U1. EFC Scoring Formula

**Goal:** Replace the single `task_score` EMA in `ScoreStore.update_score()` with decomposed EFC scoring: track I(informativeness), V(validity), R(retention) per trajectory, score = I × V × R.

**Dependencies:** None

**Files:**
- `scoring.py` — add EFC scoring to `ScoreStore`, deprecate `update_score`
- `trajectory.py` — add `efc` field to trajectory schema (I, V, R factors)
- `director.py` — update `evaluate_and_update()` to use EFC scoring

**Approach:**
- `EFCScorer` class with methods: `score_trajectory(domain, prompt, response, task_score)` → `{informative, valid, retained, composite}`
- I = `min(task_score / 100, 1.0)` (informativeness relative to max)
- V = `1.0 if task_score > 0 else 0.0` (validity: binary pass/fail gate)
- R = `min(trajectory_length_chars / 5000, 1.0)` (retention: longer trajectories retain better)
- Composite = I × V × R, scaled to 0-100
- Keep backward compat: `update_score()` delegates to EFC internally, same signature
- **LEAP reference removed** — EFC is the correct citation; LEAP was a placeholder

**Patterns to follow:** Existing `ScoreStore.update_score()` signature

**Test scenarios:**
- EFC composite score = 0 when V=0 (failed task), regardless of I or R
- EFC composite score increases with longer trajectory content (R factor)
- EFC composite saturates at 100 (I×V×R capped)
- Backward compat: `update_score()` produces similar range to current 70/30 EMA
- Edge case: empty trajectory → R=0 → composite=0
- Edge case: task_score=0 → V=0 → composite=0

### U2. Cost-Aware Model Selection

**Goal:** The router prefers cheaper adequate models over expensive perfect ones when the task difficulty is low.

**Dependencies:** U1 (needs EFC scores for comparison)

**Files:**
- `routing.py` — add cost-aware sort to `route_task()`
- `executor.py` — expose model cost tiers
- `scoring.py` — add cost tier to SEED_AGENTS

**Approach:**
- Model cost tiers: `free` (0), `ollama` (1), `foundry` (2), `cloud` (3)
- Selection score = `model_score - cost_penalty(difficulty)`
- `cost_penalty(easy)=10`, `cost_penalty(medium)=5`, `cost_penalty(hard)=0`, `cost_penalty(blocker)=0`
- This naturally prefers small local models for easy tasks, premium for hard ones
- COMBO_MAP entries tagged with cost tier in SEED_AGENTS

**Patterns to follow:** Existing `route_task()` score ordering; COMBO_MAP naming convention

**Test scenarios:**
- Easy task prefers ollama/foundry over cloud when scores are close
- Hard/blocker task always picks highest score regardless of cost
- Cost penalty doesn't cause zero-score selection (minimum score gate still applies)
- Edge case: only cloud models qualify → cost penalty ignored

### U3. AutoHarness: Auto-Generated Curriculum from Trajectories

**Goal:** Replace hand-authored curriculum YAMLs with data-driven synthesis from high-scoring trajectories.

**Dependencies:** U1 (needs EFC scores to identify high-quality trajectories)

**Files:**
- `curricula/generator.py` — new file: trajectory → curriculum YAML pipeline
- `trajectory.py` — add `trajectories_for_curriculum()` extended filter
- `curricula/` — auto-generated YAMLs written here

**Approach:**
- Scan `trajectories_for_training(domain, min_score=70)` for qualifying examples
- Cluster trajectories by `prompt` semantic similarity (simple: same-domain, same-difficulty form clusters)
- For each cluster: extract common patterns, generate task descriptions
- Write curriculum YAML with `gates` derived from score distribution
- Register in `curricula/index.yaml`
- Dry-run mode: preview what would be generated without writing

**Output structure:**
```
curricula/
  auto-python-testing-20260612.yaml
  auto-git-operations-20260612.yaml
  auto-code-review-20260612.yaml
  index.yaml  (updated)
```

**Test scenarios:**
- Generator produces valid YAML matching existing schema
- Generated tasks reference actual trajectory prompts
- Gates align with score percentiles of source trajectories
- Dry-run produces no file changes
- Edge case: empty trajectory pool → no curricula generated, logged warning
- Edge case: single trajectory → single-task curriculum

### U4. Plan-Compliance Tracking (Director)

**Goal:** The Director tracks whether routed tasks were actually executed correctly — not just attempted.

**Dependencies:** U1 (EFC scores provide quality signal)

**Files:**
- `director.py` — add compliance tracking to `run_task()` and `evaluate_and_update()`
- `trajectory.py` — add compliance fields to trajectory schema

**Approach:**
- Compliance dimensions:
  - `routed`: was a model selected? (always yes for non-blocked)
  - `attempted`: did the model produce a non-empty response? (vs error/empty)
  - `completed`: did the output include required deliverables? (keyword/format check)
  - `scored`: was a task_score assigned? (via human eval)
- Compliance score = weighted average across dimensions
- Track running compliance per-domain per-agent in `scores.json`
- Report in director output and weekly report

**Test scenarios:**
- Error response → `attempted=false` → compliance < 100%
- Empty response → `attempted=false`
- Valid response but no evaluation → `scored=false`
- Full cycle: route → execute → evaluate → compliance = 100%
- Edge case: blocked task → all compliance dimensions = N/A

### U5. SkillOpt LoRA Pipeline

**Goal:** Build a training pipeline that feeds high-scoring trajectories into LoRA fine-tuning via Unsloth, treating "skill" as a trainable parameter as the SkillOpt framework describes.

**Dependencies:** U3 (curriculum structure defines what to train on)

**Files:**
- `training/lora_pipeline.py` — new: orchestrates LoRA training
- `training/lora_config.py` — new: LoRA hyperparameters
- `executor.py` — add LoRA model loading if trained model exists
- `director.py` — update to prefer LoRA-tuned models for their domain

**Approach:**
- `train_for_domain(domain, base_model="qwen2.5-coder:7b")`:
  1. Fetch trajectories via `trajectories_for_training(domain, min_score=70)`
  2. Format as training pairs `(prompt → response)`
  3. Write temp JSONL for Unsloth
  4. Subprocess `unsloth_train.py --data /tmp/train.jsonl --output ~/.hermes/models/lora/{domain}/`
  5. Register LoRA adapter path in `COMBO_MAP`
- `lora_config.py`: rank=16, alpha=32, target_modules=["q_proj","v_proj"], epochs=3
- `predict_with_lora(agent_name, prompt)`: prepend adapter activation to system prompt
- **No Unsloth dependency in school-core** — subprocess only

**Test scenarios:**
- `train_for_domain()` produces a valid LoRA adapter directory
- Directory contains `adapter_config.json` and `adapter_model.safetensors`
- Insufficient trajectories (< 5) → skip with logged warning
- Runtime: subprocess failure → `ExecutorError` with stderr
- Edge case: domain has zero qualifying trajectories → no-op

### U6. Sleep/Wake Protocol

**Goal:** Serialize Agent School's full system state to Engram on "sleep" and restore on "wake", implementing the Harness-1 state-externalizing pattern.

**Dependencies:** U4 (compliance data included in snapshot)

**Files:**
- `state.py` — new: Sleep/Wake lifecycle
- `data/` — sleep snapshots stored here (JSON)
- `director.py` — add `sleep()`/`wake()` entry points
- `cli.py` — add `school sleep` and `school wake` commands

**Approach:**
- Sleep snapshot contains:
  - All scores (serialize `ScoreStore.scores`)
  - Recent trajectories (last N per domain)
  - Compliance stats
  - Session metadata (timestamp, duration, model used)
- Snapshot written to `data/sleep/{timestamp}.json`
- Synced to Engram as persistent observation
- Wake: scan for latest snapshot → deserialize → restore ScoreStore state
- Graceful: no prior snapshot → start fresh (no error)

**Test scenarios:**
- Sleep produces valid JSON with all expected fields
- Snapshot syncs to Engram (check `engram_available()`)
- Wake restores scores to pre-sleep values
- No prior snapshot → wake is no-op (fresh start)
- Edge case: interrupted sleep → partial snapshot discarded
- Edge case: corrupted snapshot → wake logs warning, starts fresh

### U7. HAL Benchmark Integration

**Goal:** Standardized evaluation harness — run all models against all domains, produce a cost-aware leaderboard, enable cross-model comparison.

**Dependencies:** U2 (cost awareness), U5 (LoRA models benchmarkable)

**Files:**
- `benchmark/runner.py` — new: orchestrate HAL-style eval
- `benchmark/report.py` — new: generate leaderboard + cost analysis
- `data/benchmarks/` — benchmark results directory
- `docs/weekly/` — benchmark reports written here

**Approach:**
- Benchmark format (JSON lines, one per model × domain):
  ```json
  {"model": "north-coding", "domain": "python-testing", "score": 85, "latency_s": 2.4, "cost_tier": 3, "trajectory_ref": "path/to/traj.json"}
  ```
- `runner.py`: for each model × domain × difficulty:
  1. Route to model
  2. Execute
  3. Score response (auto: response exists × non-empty × format checks)
  4. Record latency, cost tier
  5. Save trajectory
- `report.py`: aggregate → cost-normalized leaderboard
  - `efficiency_score = score / cost_tier`
  - Domain-specific rankings
  - Cross-model comparison table
- Weekly report: cumulative trends, top movers, regression warnings

**Test scenarios:**
- Single model × domain benchmark produces valid JSONL row
- Leaderboard sorts correctly by efficiency_score
- Cost-normalized ranking differs from raw score ranking
- Edge case: model unreachable → score=0, error logged
- Edge case: empty model list → no-op

## Dependencies & Sequencing

```
U1 (EFC Scoring)
 └── U2 (Cost-Aware Routing) — needs EFC scores
 └── U3 (AutoHarness) — needs EFC scores for trajectory quality
      └── U5 (SkillOpt LoRA) — needs curriculum structure
           └── U7 (Benchmarks) — needs LoRA models
U4 (Compliance) — mostly independent, minor overlap with U1
 └── U6 (Sleep/Wake) — needs compliance data
```

**Recommended order:**
1. **U1 + U4** (parallel — independent except shared trajectory schema)
2. **U2** (depends on U1)
3. **U3** (depends on U1)
4. **U5** (depends on U3)
5. **U6** (depends on U4)
6. **U7** (depends on U2, U5)

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| EFC formula doesn't match real eval quality | Medium | Keep backward-compat `update_score()`; compare EFC vs EMA on historical trajectories |
| AutoHarness generates low-quality curricula | Medium | Dry-run mode; human review before index update |
| LoRA training requires GPU memory | High | Foundry daemon already runs on GPU; Unsloth supports QLoRA (4-bit) for 7B models |
| Sleep/Wake adds latency | Low | Snapshots are JSON I/O; acceptable for session boundaries |
| Benchmarks are costly (API calls) | Medium | Test with 1-2 models first; free/ollama models only for initial run |

## Future Work (Post-Roadmap)

- Real-time cost tracking from OmniRoute API (replace tier proxy with actual token costs)
- Multi-agent task routing (A2A composition, not just single-model fallback)
- Auto-remediation: low compliance triggers auto-retrain
- Dashboard GUI (streamlit or similar)
- Community model contributions (external agent profiles)
