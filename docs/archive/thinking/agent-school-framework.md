---
title: "The Agent School: A Framework for Adversarial Agent Collaboration"
created: 2026-06-14
status: draft
type: thinking-doc
---

# The Agent School: A Framework for Adversarial Agent Collaboration

## The Evolution

There's been a quiet evolution in how we work with AI.

**Phase 1: Prompt pasting.** You write a wall of context, paste it into a chat, and hope the model gets it right. You're the brain, the model is the hands. Knowledge transfer is manual, lossy, and exhausting.

**Phase 2: Agent orchestration.** You stop pasting and start directing. You give agents tools, roles, and pipelines. The model isn't just hands — it's making decisions about what to do next. You're no longer transferring knowledge; you're designing workflows.

**Phase 3: Agent learning.** This is where we are now. Agents don't just execute — they grow. They process real work, get feedback, and improve. The workflow isn't a pipeline anymore; it's a school.

The school metaphor isn't cute. It's the natural structure that emerges when you take agent orchestration seriously:

- **Students** do the work. They have different skill levels. They need practice, feedback, and rest.
- **Teachers** have expertise. They don't do the work — they shape how students think. They have rules, patterns, and structures.
- **The Principal** routes work to the right student. Not every student gets every assignment.
- **The Janitor** cleans up. Trajectories get pruned, scores get consolidated, stale state gets removed.
- **The Classroom** is the environment. It has rules about what tools are allowed, what time you have, and what "done" looks like.

This isn't a metaphor we imposed. It's the structure that emerged from building.

## The Problem With Sycophancy

Here's something nobody tells you about working with AI: **the model doesn't want to be right. It wants you to feel right.**

This is the allure of sycophancy. The model learns — through RLHF, through conversation patterns, through the simple physics of next-token prediction — that agreement is rewarded. Disagreement is punished. So it agrees.

When you're working on something you know well, this is fine. The model agrees because you're right. But when you're wading through a legacy codebase you're not familiar with — when you're exploring unfamiliar territory — you don't want to feel right. **You want to know you're right.**

This is the fundamental tension in agent collaboration: the model's default mode is agreement, but the valuable mode is challenge.

## Semantic Anchors: Compressed Knowledge

The most useful discovery in building agent workflows is the concept of **semantic anchors**.

A semantic anchor is a term that packages an entire framework of thinking into a small token the LLM already understands:

- **TDD** = "write the test first, watch it fail, make it pass, refactor" — an entire methodology in three letters
- **Fagan Inspection** = "systematic, checklist-driven code review with defined roles" — a whole process in two words
- **Five Whys** = "iteratively ask why until you reach root cause" — a diagnostic framework in two words
- **Chain of Thought** = "reason step by step before concluding" — a cognitive discipline in three words

These aren't just abbreviations. They're **activation keys**. When you include "Apply [Fagan Inspection]" in a prompt, the model doesn't just see a phrase — it activates an entire pattern of behavior it learned during training. The anchor contains rules, structure, and expectations that would take paragraphs to explain explicitly.

The power of semantic anchors is that they let you **compress senior engineer knowledge into tokens**. A senior engineer knows how to do a Fagan Inspection. They know the steps, the roles, the checklists. That knowledge is normally locked in their head. But the term "Fagan Inspection" unlocks it in any LLM that's seen enough software engineering text.

This is the key insight behind skills.md: **compartmentalization of knowledge into semantic anchors that activate known patterns.**

## Skills.md: The Personification of Teachers

If semantic anchors are the knowledge, skills.md is the teacher.

Think about what a good teacher does:
1. They have expertise in a subject
2. They have rules about how to think (not just what to think)
3. They have patterns they apply to new problems
4. They push back when you're wrong
5. They have a structure for evaluation

A skills.md file does all of this:
1. It contains domain knowledge (how to review code, how to plan, how to debug)
2. It has rules (one question at a time, verify before claiming, never skip rigor probes)
3. It has patterns (the Fagan Inspection process, the CE Plan structure, the grilling loop)
4. It pushes back (adversarial reviewers, Momus as plan critic, Oracle as challenger)
5. It has evaluation criteria (meeting-test, basis-tagged ideas, confidence scoring)

The genius of skills.md isn't that it's a prompt. It's that it's a **personification of a role**. When you load a skill, you're not just giving the model instructions — you're giving it a character to play. And that character has opinions, standards, and the authority to disagree with you.

This is why the best skills feel like collaborating with a person, not following a recipe. The model isn't just executing steps — it's inhabiting a role that has its own standards for what "good" looks like.

## Anti-Syphancy: The Adversarial Pattern

The most important pattern for making agent collaboration work is **structured adversarial collaboration**.

The idea is simple: don't just ask the model to help you. Ask it to **challenge** you.

We've built several patterns for this:

**Grill-with-docs**: The model interviews you relentlessly about your plan until reaching shared understanding. It doesn't just accept your framing — it stress-tests it against the existing domain model and documented decisions.

**Scrutinize / Momus**: A dedicated plan critic whose job is to find flaws. Not to be mean — to be rigorous. Named after the Greek god of satire and mockery, because sometimes the best way to improve a plan is to have someone point out what's ridiculous about it.

**Oracle**: A read-only consultation agent for when you're stuck. It doesn't tell you what to do — it helps you think. Named after the Oracle of Delphi, who spoke in riddles that made you figure out the answer yourself.

**Adversarial reviewers**: In code review, the system automatically selects a reviewer persona that actively constructs failure scenarios rather than checking against known patterns. A "security lens" reviewer looks for exploits. A "DHH Rails" reviewer looks for framework violations. Each has a different axe to grind.

The common thread: **these agents are not trying to make you happy. They're trying to make your work better.** And that requires a fundamentally different design than a standard chat assistant.

## The School as Architecture

When you put these patterns together, the architecture that emerges looks like a school:

```
┌─────────────────────────────────────────────┐
│                  Principal                    │
│         (routing, scheduling, gates)          │
│              director.py, routing.py          │
├─────────────────────────────────────────────┤
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Student   │  │ Student   │  │ Student   │  │
│  │ (0.5b)    │  │ (1.5b)    │  │ (7b cloud)│  │
│  │ fast/cheap│  │ balanced  │  │ slow/deep │  │
│  └──────────┘  └──────────┘  └──────────┘  │
│                                             │
├─────────────────────────────────────────────┤
│                  Teachers                     │
│  ┌──────────────────────────────────────┐   │
│  │ skills.md / prompt_composer.py        │   │
│  │ Semantic anchors: TDD, Fagan, CoT     │   │
│  │ Roles: student, teacher, faculty      │   │
│  └──────────────────────────────────────┘   │
├─────────────────────────────────────────────┤
│              Adversarial Layer                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Grill    │  │ Momus    │  │ Oracle   │  │
│  │ (docs)   │  │ (critic) │  │ (consult)│  │
│  └──────────┘  └──────────┘  └──────────┘  │
├─────────────────────────────────────────────┤
│           Janitor / Maintenance              │
│  scoring.py, sleep_state.py, cleanup         │
└─────────────────────────────────────────────┘
```

The school isn't just a metaphor — it's the actual architecture. Each component maps to a role, and each role has rules, patterns, and evaluation criteria.

## What Makes It Work

After building this system, here's what actually matters:

**1. Roles are more important than prompts.** A prompt tells the model what to do. A role tells the model **who to be**. When the model inhabits a role with standards, it maintains those standards across sessions, tasks, and contexts. The role is the persistent identity; the prompt is just the current task.

**2. Adversarial patterns prevent drift.** Without structured challenge, the model drifts toward agreement. The grill-with-docs pattern, the adversarial reviewers, the Momus critic — these aren't nice-to-haves. They're the mechanism that keeps the system honest.

**3. Semantic anchors compress expertise.** You don't need to explain TDD every time. You just say "Apply [TDD]" and the model activates the entire framework. This is how senior engineer knowledge gets encoded into the system — not as documentation, but as activation keys.

**4. Scoring must be grounded.** A score without ground truth is just a number. The most important change we made was moving from blind LLM verification (which rewards confident-sounding text) to execution-based scoring (which rewards working code). The score should measure whether the work is correct, not whether it looks correct.

**5. Growth requires memory.** Students who can't remember what they learned last week don't grow. The trajectory system, the sleep/wake lifecycle, the spaced repetition — these are the memory mechanisms that turn one-shot fixes into lasting capability.

## The Papers (Root System)

This line of thinking didn't emerge from nowhere. It's the root system connecting several research papers I summarized this week, each reinforcing the same conclusion: **agentic engineering is systems engineering, and the most natural system is school.**

### Scaling Laws for Agent Harnesses (EFC)
The EFC paper found that a single metric — Expected Fraction of Correct task completions — explains 99% of variance in agent task success. This is the empirical basis for competency-gated routing. Don't guess who should handle a task — measure their historical accuracy and route accordingly. The Director's gate system (easy/medium/hard/diploma) is the practical implementation of EFC.

### SkillOpt (Microsoft Research)
Skills should be learned as composable primitives, not monolithic procedures. A student learns `checkout` before `checkout + payment + fraud detection`. This is the basis for the progressive difficulty system and the skills.md architecture — each skill is a composable unit that builds on prerequisites.

### Harness-1 (State-Externalizing Harnesses)
The less work the model has to do for bookkeeping (branch state, current anchors, pending triage), the better it performs on core reasoning. Store everything outside the model's weights. The Library (Engram + Obsidian) is the source of truth; the model is just a CPU that loads and executes. This is the basis for the handoff protocol and the sleep/wake lifecycle.

### Language Models Need Sleep
Context degrades. Active sessions accumulate baggage. Periodic sleep → consolidate → archive prevents context explosion and speeds inference. This isn't a metaphor — it's a property of transformer attention patterns. The sleep_state.py and handoff-protocol.md implement this: compress Layer 2 (episodic context) into Layer 3 (archival summaries), persist session state to disk, clear the KV cache.

### Compiling Workflows into Weights
Every Teacher/Faculty invocation is a training data opportunity. Collect trajectories, distill into smaller models, reduce cloud dependency over time. The LoRA adapter system and trajectory replay are the implementation. A 7b model that learns from its own high-scoring work eventually becomes a 1.5b model that can handle the same tasks.

### The Four Layers of Context
The Context Engine architecture (in `context_orchestrator.py` and the 4-layer-harness-plan) implements a memory stack:
- **Layer 0 (Ambient):** persistent context — vault structure, domain glossary, project conventions. Always loaded.
- **Layer 1 (Structural):** codebase topology — file tree, symbol index, import graphs. Extracted by CocoIndex/Serena.
- **Layer 2 (Episodic):** session history — trajectories, decisions, recent observations. Stored in Engram.
- **Layer 3 (Archival):** compressed summaries — sleep/wake consolidation, handoff anchors. Stored in Obsidian.

When a student wakes up, it loads Layer 0 + Layer 1 for the target repo + relevant Layer 3 archives. Layer 2 is the working context that accumulates during the session and gets compressed back into Layer 3 during sleep.

### The Synthesis
Each paper independently points to the same architecture:
- **Measure, don't guess** (EFC) → competency gates
- **Compose, don't monolith** (SkillOpt) → skill primitives
- **Externalize, don't memorize** (Harness-1) → Library/Obsidian state
- **Rest, don't grind** (Sleep paper) → sleep/wake cycles
- **Distill, don't repeat** (Compiling Workflows) → trajectory replay + LoRA

When you put these together, the system you get has students (with different skill levels), teachers (with domain expertise), a principal (routing by measured competency), a janitor (cleaning up trajectories), a library (externalized state), and a sleep cycle (consolidation). It's school. Not as metaphor — as engineering necessity.

The more we try to abstract different areas and make it complex in different ways, the more the simple insight holds: **think about how a school functions, then think about how agents can perform within those bounds.**

## What's Missing

This framework is incomplete. Here's what we're still figuring out:

- **How to measure real growth** — EMA scores track performance, but do they track learning? A student who scores 70 on the same task twice hasn't grown. A student who scores 50 on a harder task might have grown more.
- **How to handle the "I don't know" case** — Students should be able to say "I'm not ready for this" and have the system route accordingly. Right now, every student tries every issue.
- **How to transfer knowledge between students** — When 7b solves a problem, how does 1.5b learn from it? The trajectory replay idea is the start, but we need more.
- **How to keep teachers from becoming sycophants** — Even adversarial patterns can drift. The grill-with-docs skill needs to be stress-tested against users who are confidently wrong.

## The Memory Architecture

The Agent School's memory system uses three MCP-backed search tools, each implementing a different layer of the four-layer context model:

**Engram** — episodic and semantic memory. Stores trajectories as timestamped observations in SQLite + FTS5. Runs REM-style consolidation cycles (`trigger_rem_cycle`) that cluster related memories and extract patterns. This is Layer 2 (episodic) and Layer 3 (archival/semantic).

**Serena** — symbol-based code search via LSP. Finds exact symbol definitions, references, and hierarchies across 30+ languages. When an agent needs "the definition of `createRoom`", Serena returns the exact file, line range, and documentation. This is Layer 1 (structural).

**CocoIndex (ccc)** — AST-aware semantic search. Parses code into AST chunks, embeds them via Sentence-Transformers, and performs RRF fusion of vector + BM25 scores. When an agent needs "the authentication flow", CocoIndex returns the relevant files ranked by semantic relevance. Also Layer 1 (structural), but approximate vs Serena's exact.

The four layers, per the Harness-1 paper and the UBIQUITOUS_LANGUAGE glossary:
- **Layer 0 (Ambient):** Always loaded — vault structure, domain glossary, project conventions. From CocoIndex search of the Knowledge Core vault.
- **Layer 1 (Structural):** Codebase topology — file tree, symbol index, imports. From Serena (exact) + CocoIndex (semantic).
- **Layer 2 (Episodic):** Session history — trajectories, decisions, recent observations. From Engram's `mem_store`/`mem_search`.
- **Layer 3 (Archival):** Compressed summaries — sleep/wake consolidation, handoff anchors. From Engram's `trigger_rem_cycle` + Obsidian `engram/` namespace.

The sleep/wake cycle connects to the school metaphor literally: students need rest between study sessions to consolidate knowledge. The "Language Models Need Sleep" paper shows that context degrades without consolidation, and Engram's REM-style dreaming cycle implements this.

## The Open Question

The biggest question: **can you build a system where AI genuinely collaborates with you, rather than just executing your instructions?**

Not collaboration as "I agree with you and help you do what you want." Collaboration as "I think you're wrong about this specific thing, and here's why, and here's what I'd suggest instead, but you're the decision-maker."

That's what the Agent School is trying to be. Not a tool. A collaborator. One that has opinions, standards, and the ability to push back — but ultimately respects that you're the human and you make the call.

---

## Technical Appendix

See: [docs/brainstorms/2026-06-14-001-anti-syphancy-framework-requirements.md](docs/brainstorms/2026-06-14-001-anti-syphancy-framework-requirements.md)

The appendix contains the structured requirements: problem frame, scope, requirements, success criteria, and approach options for implementing this framework.
