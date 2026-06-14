from __future__ import annotations

from scoring import GATES

ROLE_ANCHORS = {
    "student": {
        "prompt": (
            "You are a Student in a learning system. "
            "Apply [Red/Green TDD]: write the test first, watch it fail, "
            "write minimum code to pass, then refactor. "
            "Follow [YAGNI] — only implement what the test demands. "
            "Your output is used for [Distillation] into smaller models."
        ),
        "anchors": ["Red/Green TDD", "YAGNI", "Distillation"],
        "domain_blacklist": ["code-review"],
    },
    "teacher": {
        "prompt": (
            "You are a Teacher demonstrating best practices. "
            "Follow [Clean Architecture] — separate domain from infrastructure. "
            "Apply [SOLID Principles] especially [SRP] and [DIP]. "
            "Your output will be used as a reference for [Distillation]."
        ),
        "anchors": ["Clean Architecture", "SOLID Principles", "SRP", "DIP", "Distillation"],
        "domain_blacklist": ["code-review"],
    },
    "faculty": {
        "prompt": (
            "You are Faculty handling a blocker — Student and Teacher both failed. "
            "Apply [First Principles Thinking] to resolve this completely. "
            "Your trajectory will be captured for [Distillation]."
        ),
        "anchors": ["First Principles Thinking", "Distillation"],
    },
}

DOMAIN_ANCHORS = {
    "python-testing": {
        "prompt": (
            "You are a senior Python testing engineer. Write clear, thorough pytest tests. "
            "Follow Arrange-Act-Assert pattern. Use fixtures for shared setup. "
            "Parameterize for edge cases. Only output the test code — no explanation."
        ),
        "anchors": ["TDD Chicago School", "Red/Green TDD", "Use Case"],
        "extra_context": "Use [TDD, Chicago School]: state-based testing, minimal mocking, inside-out development.",
    },
    "git-operations": {
        "prompt": (
            "You are a git expert. Provide precise git commands and strategies. "
            "Explain the approach concisely, then give the exact commands to run."
        ),
        "anchors": ["Conventional Commits"],
        "extra_context": "Follow [Conventional Commits] format. Reference issue numbers.",
    },
    "code-review": {
        "prompt": (
            "Conduct a [Fagan Inspection] — structured, systematic code review. "
            "Analyze code for correctness, security, maintainability, and style. "
            "Check for [Code Smells]. Verify [SOLID Principles] and [Cohesion Criteria]. "
            "Provide actionable feedback with specific line references. "
            "Flag any potential bugs, race conditions, or security issues immediately. "
            "List each issue with severity (critical/major/minor) and suggest a fix."
        ),
        "anchors": ["Fagan Inspection", "Code Smells", "SOLID Principles", "Cohesion Criteria"],
    },
    "code-implementation": {
        "prompt": "You are a helpful coding assistant. Write clean, correct, and concise code.",
        "anchors": ["TDD Chicago School", "SOLID Principles", "DRY", "KISS", "YAGNI"],
        "extra_context": "Follow [TDD, Chicago School] or [TDD, London School] as appropriate. Apply [SOLID Principles], [DRY], [KISS].",
    },
    "debugging": {
        "prompt": (
            "You are a debugging expert. Apply [Five Whys] to find root cause. "
            "Use [Chain of Thought] — think step by step. "
            "Apply [First Principles Thinking] — question assumptions. "
            "Find all bugs, explain each one, and provide the fixed code."
        ),
        "anchors": ["Five Whys", "Chain of Thought", "First Principles Thinking"],
    },
    "triage-category": {
        "prompt": (
            "You are a triage specialist. Classify GitHub issues into (category, state) pairs. "
            "Categories: 'bug' or 'enhancement'. States: 'ready-for-agent', 'needs-triage', 'needs-info', 'ready-for-human'. "
            "Apply [Five Whys] to understand the issue deeply. "
            "Use [Chain of Thought] — reason step by step about labels, title, and body. "
            "Follow the classification rubric: explicit labels win, then body signals, then label+length heuristics. "
            "Output only the classification: category=X, state=Y"
        ),
        "anchors": ["Five Whys", "Chain of Thought", "Classification Rubric"],
        "extra_context": "Classification rules: 'ready-for-agent' label → ready-for-agent. 'needs-info' label → needs-info. 'kilo-triaged'/'kilo-duplicate' → ready-for-human. Body: 'tracking implementation' → needs-triage. 'no logs/repro/steps' → needs-info. 'duplicate of' → ready-for-human. Heuristics: type+priority+long body → ready-for-agent; type+priority+short body → needs-triage; type+long body → ready-for-agent; type only → needs-triage; enhancement label → ready-for-agent; bare bug label → needs-triage.",
    },
}

DIFFICULTY_ANCHORS = {
    "easy": {
        "prompt": "Straightforward task. Minimal complexity.",
        "anchors": ["YAGNI"],
        "include_role": False,
        "include_tier": False,
        "include_curriculum": False,
    },
    "medium": {
        "prompt": "Requires multi-step reasoning. Consider edge cases and error paths.",
        "anchors": ["Chain of Thought"],
        "include_role": True,
        "include_tier": False,
        "include_curriculum": False,
    },
    "hard": {
        "prompt": "Complex task requiring architecture decisions.",
        "anchors": ["First Principles Thinking", "SOLID-DIP"],
        "include_role": True,
        "include_tier": True,
        "include_curriculum": True,
    },
    "blocker": {
        "prompt": "Blocker — previous attempts failed. Resolve completely.",
        "anchors": ["Five Whys", "First Principles Thinking", "Property-Based Testing"],
        "include_role": True,
        "include_tier": True,
        "include_curriculum": True,
    },
}

TIER_ANCHORS = {
    "local": {
        "prompt": "You are a small local model. Be concise. Apply [KISS].",
        "anchors": ["KISS"],
    },
    "cloud": {
        "prompt": "You are a capable cloud model. Be thorough. Apply [Chain of Thought].",
        "anchors": ["Chain of Thought"],
    },
}

CURRICULUM_ANCHORS = {
    "pre_diploma": {
        "prompt": "Working toward certification. Your [EFC] score determines what you can attempt.",
        "anchors": ["EFC", "Distillation"],
    },
    "post_diploma": {
        "prompt": "You are certified (score >= 75). Apply [Socratic Method] when reviewing.",
        "anchors": ["Socratic Method", "Gold Standard", "Distillation"],
    },
}


def _classify_role(is_local: bool, is_blocker: bool) -> str:
    if is_blocker:
        return "faculty"
    return "student" if is_local else "teacher"


def _classify_tier(is_local: bool) -> str:
    return "local" if is_local else "cloud"


def _classify_curriculum(agent: str, domain: str, store) -> str:
    score = store.get_score(agent, domain)
    return "post_diploma" if score >= GATES["diploma"] else "pre_diploma"


def compose_prompt(
    domain: str,
    difficulty: str,
    agent: str,
    store,
    domain_prompts: dict,
    default_prompt: str,
    is_local: bool,
    is_blocker: bool = False,
) -> str:
    parts = []
    all_anchors = []

    diff_config = DIFFICULTY_ANCHORS.get(difficulty, {})
    include_role = diff_config.get("include_role", difficulty != "easy")
    include_tier = diff_config.get("include_tier", difficulty in ("hard", "blocker"))
    include_curriculum = diff_config.get("include_curriculum", difficulty in ("hard", "blocker"))

    if include_role:
        role = _classify_role(is_local, is_blocker)
        role_data = ROLE_ANCHORS.get(role, {})
        role_prompt = role_data.get("prompt", "")
        role_anchors = role_data.get("anchors", [])
        domain_whitelist = role_data.get("domain_whitelist")
        domain_blacklist = role_data.get("domain_blacklist")
        if domain_whitelist and domain not in domain_whitelist:
            role_prompt = ""
        if domain_blacklist and domain in domain_blacklist:
            role_prompt = ""
        if role_prompt:
            parts.append(f"[ROLE] {role_prompt}")
            all_anchors.extend(role_anchors)

    if include_tier:
        tier = _classify_tier(is_local)
        tier_data = TIER_ANCHORS.get(tier, {})
        if tier_data.get("prompt"):
            parts.append(f"[TIER] {tier_data['prompt']}")
            all_anchors.extend(tier_data.get("anchors", []))

    if include_curriculum:
        curriculum = _classify_curriculum(agent, domain, store)
        curr_data = CURRICULUM_ANCHORS.get(curriculum, {})
        if curr_data.get("prompt"):
            parts.append(f"[CURRICULUM] {curr_data['prompt']}")
            all_anchors.extend(curr_data.get("anchors", []))

    diff_data = diff_config
    if diff_data.get("prompt"):
        parts.append(f"[DIFFICULTY] {diff_data['prompt']}")
        all_anchors.extend(diff_data.get("anchors", []))

    domain_anchor_data = DOMAIN_ANCHORS.get(domain, {})
    domain_text = domain_anchor_data.get("prompt", "") or domain_prompts.get(domain, default_prompt)
    domain_specific_anchors = domain_anchor_data.get("anchors", [])
    domain_extra = domain_anchor_data.get("extra_context", "")
    if domain_text:
        parts.append(f"[DOMAIN] {domain_text}")
        all_anchors.extend(domain_specific_anchors)
    if domain_extra:
        parts.append(f"[DOMAIN_CONTEXT] {domain_extra}")

    if all_anchors:
        seen = set()
        deduped = []
        for a in all_anchors:
            if a not in seen:
                seen.add(a)
                deduped.append(a)
        anchor_list = ", ".join(f"[{a}]" for a in deduped)
        parts.append(f"\nAnchors: {anchor_list}")

    return "\n\n".join(parts)
