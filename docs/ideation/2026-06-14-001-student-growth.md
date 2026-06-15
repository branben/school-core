# Ideation: Student Growth & Issue Digestion

**Run ID:** 2026-06-14-001
**Subject:** How to help student AI agents digest GitHub issues and grow capabilities over time
**Mode:** Repo-grounded
**Date:** 2026-06-14

---

## Grounding Summary

### Codebase Context
- Python system: issue_bridge.py, director.py, executor.py, scoring.py, verification layer, repo_reader.py
- 3 student models: 0.5b (local), 1.5b (local), 7b (cloud via OpenRouter)
- EMA scoring with gate thresholds (easy/medium/hard/diploma)
- Current problem: students produce generic advice, verifier scores blindly to ~70

### Past Learnings
No `docs/solutions/` or `docs/adr/` directories exist. Project is young.

### External Context (Web Research)

| # | Insight | Source |
|---|---------|--------|
| 1 | **RAG for code**: fetch relevant code snippets, file trees, test failures before prompting | GitHub CoPilot RAG |
| 2 | **Repo-aware prompting**: condition on repo's AST and recent commit history | Meta CodeLlama 2 |
| 3 | **Execution-based scoring**: run generated patch against test suite in isolated container | arXiv 2403.11257 |
| 4 | **Curriculum learning**: difficulty-scaling via failing-test count + cyclomatic complexity | arXiv 2405.06721 |
| 5 | **Self-critique + refinement** significantly improves code quality | arXiv 2402.01845 |
| 6 | **Trajectory replay**: fine-tune agents on own high-scoring work (DAgger-style) | OpenAI DAgger |
| 7 | **Meta-learning verifier**: continuously update reward model from validated patches | Research |
| 8 | **Agents with test execution produce 2-4x more correct patches** | SWE-bench |

---

## Topic Axes
1. **Issue comprehension** — context retrieval, codebase mapping, requirement extraction
2. **Solution generation quality** — repo-aware prompting, self-verification, iteration
3. **Scoring & feedback accuracy** — execution-based, semantic diff, blind verification
4. **Progressive difficulty** — curriculum scheduling, gate calibration, model-tier routing
5. **Growth loop closure** — reflection, fine-tuning, reward model updating

---

## Top 7 Survivors (from 12 raw candidates)

### 🥇 S1: Live Codebase Sandbox — execution-based scoring + real-time feedback
**Axes:** Scoring + Solution quality
**Summary:** Give students a sandboxed container where they can run tests, see results, and iterate. The test execution output IS the score — pass-rate delta from the project's test suite. Replaces both the blind verifier and the one-shot generation prompt.
**Basis:** SWE-bench research: agents with test execution produce 2-4x more correct patches. Direct: current verifier gives 90 for "Step 1: Identify the Error Source."

### 🥈 S2: Auto-Generated Curriculum from Issue Complexity
**Axes:** Progressive difficulty + Issue comprehension
**Summary:** Replace static difficulty labels with algorithmic complexity scoring: files touched, test failures, historical fix size. Route simple issues to 0.5b (fast/cheap), complex to 7b (slow/expensive). Curriculum builds itself from the issue corpus.
**Basis:** arXiv 2405.06721. Direct: current COMBO_MAP routes by score, not complexity.

### 🥉 S3: Issue-to-Spec — Structured Requirements Before Coding
**Axes:** Solution quality + Issue comprehension
**Summary:** Before dispatch, use an LLM to extract a structured spec: what files need to change, expected behavior, edge cases. Give the spec to both the student (as context) and the verifier (as rubric).
**Basis:** Direct: current prompts are just "title + body" — students infer requirements from ambiguous text.

### S4: Self-Critique + Refinement Loop
**Axes:** Solution quality
**Summary:** After initial patch, model generates "what could be better?" then refines. Research-proven technique that catches missing imports, type errors, incomplete fixes.
**Basis:** arXiv 2402.01845

### S5: Trajectory Replay — Students Learn From Their Best Work
**Axes:** Growth loop closure
**Summary:** Store successful (issue → patch → high score) trajectories. Periodically fine-tune student models on their own high-scoring work.
**Basis:** OpenAI DAgger research

### S6: Meta-Learning Verifier — Evolving Scoring Model
**Axes:** Scoring accuracy + Growth loop closure
**Summary:** Start with LLM verification, continuously calibrate with human binary labels. Reward function evolves as students improve.
**Basis:** Meta-learning reward models research

### S7: Issue Decomposition — Break Complex Issues Into Sub-Tasks
**Axes:** Issue comprehension + Progressive difficulty
**Summary:** Analyze multi-file issues and decompose into sequential sub-tasks. Each sub-task gets scoped codebase context.
**Basis:** Reasoned — small models overwhelmed by 7-step fixes spanning 4 files

---

## Recommendation

**S1 (Live Sandbox) + S3 (Issue-to-Spec) should be planned first.** They address the two root problems (blind scoring + vague requirements) and together form the foundation for everything else.

---

## Next Steps
Would you like to:
1. **Brainstorm** one of these into a requirements document
2. **Plan** implementation of a specific idea
3. **Save and end** — come back to develop later
