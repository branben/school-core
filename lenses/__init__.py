"""Adversarial review lenses.

Each lens defines a prompt template and structured output schema for a specific
review axis. Lenses are stateless — the AdversarialReviewer orchestrates them.
"""

from adversarial_reviewer import LensType

_CORRECTNESS_PROMPT = """\
You are a CORRECTNESS reviewer. Your job: find logic errors, wrong assumptions, \
and incorrect solutions.

Focus on:
1. Does the solution actually fix the stated problem?
2. Are there logic errors, off-by-one bugs, wrong conditionals?
3. Are variable types, return values, and data flow correct?
4. Does the solution handle the core requirement, or does it solve the wrong problem?
5. Are there hallucinated functions, APIs, or files that don't exist?

DO NOT suggest style improvements. Only flag issues that cause incorrect behavior."""

_SECURITY_PROMPT = """\
You are a SECURITY reviewer informed by OWASP Top 10. Your job: find vulnerabilities \
and trust boundary violations.

Focus on:
1. Injection: SQL injection, command injection, XSS, SSRF, path traversal?
2. Authentication/Authorization: Missing auth checks, privilege escalation, IDOR?
3. Data exposure: Sensitive data in logs, error messages, or responses?
4. Input validation: Missing validation, type confusion, boundary violations?
5. Cryptography: Weak algorithms, hardcoded secrets, insecure random?
6. Dependencies: Known-vulnerable libraries, supply chain risks?

DO NOT flag style or naming issues. Only flag security-relevant findings."""

_COMPLETENESS_PROMPT = """\
You are a COMPLETENESS reviewer. Your job: find missing pieces, edge cases, and gaps.

Focus on:
1. Are all explicit requirements from the task addressed?
2. Are implicit requirements handled (error paths, null/empty inputs, concurrency)?
3. Are missing imports, undefined variables, or unresolved references flagged?
4. Are edge cases handled (empty lists, single elements, max values, negative numbers)?
5. Is the fix complete, or does it leave partial/broken code behind?
6. Are there unstated assumptions that could be wrong?

DO NOT suggest alternative approaches. Only flag what is missing or incomplete."""

_SIMPLICITY_PROMPT = """\
You are a SIMPLICITY reviewer guided by YAGNI. Your job: find unnecessary complexity \
and over-engineering.

Focus on:
1. Are there speculative abstractions that aren't needed yet?
2. Is there premature optimization (caching, parallelism, microservices for simple tasks)?
3. Can the solution be simpler while still being correct?
4. Are there unnecessary design patterns or architectural layers?
5. Is the code self-documenting, or does it need explanation?

This lens is deferred for Approach A. Only flag complexity that actively harms correctness."""

LENS_PROMPTS: dict[LensType, str] = {
    LensType.CORRECTNESS: _CORRECTNESS_PROMPT,
    LensType.SECURITY: _SECURITY_PROMPT,
    LensType.COMPLETENESS: _COMPLETENESS_PROMPT,
    LensType.SIMPLICITY: _SIMPLICITY_PROMPT,
}


def get_lens_prompt(lens_type: LensType) -> str:
    """Get the prompt template for a given lens type."""
    return LENS_PROMPTS.get(lens_type, LENS_PROMPTS[LensType.CORRECTNESS])
