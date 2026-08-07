"""Adversarial Reviewer — Core Engine.

Takes a student's output, applies domain-specific adversarial lenses,
and produces a structured verdict with gaps and suggestions.

Generator-Critic separation: the reviewer OUTPUTS structured findings only.
It never produces rewrites or alternative solutions.

Usage:
    from adversarial_reviewer import AdversarialReviewer, LensType

    reviewer = AdversarialReviewer(call_model_fn=executor.call_model)
    result = reviewer review(
        output=student_output,
        task={"title": "...", "body": "...", "domain": "code-implementation"},
        codebase_context=file_tree + relevant_files,
        lens_types=[LensType.CORRECTNESS, LensType.SECURITY, LensType.COMPLETENESS],
    )
    # result.verdict => "PASS" | "FAIL"
    # result.gaps => [Finding(...), ...]
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class LensType(str, Enum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    COMPLETENESS = "completeness"
    BUILD = "build"  # Executable verification: does the code compile/test? Fed by verify_gate.
    SIMPLICITY = "simplicity"  # Approach B — deferred for now


@dataclass
class Finding:
    """A single issue found during adversarial review."""

    section: str  # What part of the output this relates to
    issue_class: str  # Category: "logic_error", "security_vulnerability", "missing_edge_case", etc.
    severity: Severity
    citation: str  # Specific line/reference from the output
    description: str  # What the issue is
    suggestion: Optional[str] = None  # How to fix it

    def to_dict(self) -> dict:
        return {
            "section": self.section,
            "issue_class": self.issue_class,
            "severity": self.severity.value,
            "citation": self.citation,
            "description": self.description,
            "suggestion": self.suggestion,
        }


@dataclass
class ReviewResult:
    """The complete output of an adversarial review."""

    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    lens_used: str = ""
    confidence: float = 0.0
    difficulty: str = "medium"  # Used by .score for calibrated severity weights
    coevolution: Optional["CoevolutionReport"] = None  # Verification-co-evolution report, set by review_with_coevolution()

    @property
    def gaps(self) -> list[str]:
        """Human-readable gap descriptions (legacy compat)."""
        return [f.description for f in self.findings]

    @property
    def suggestions(self) -> list[str]:
        """Human-readable suggestions (legacy compat)."""
        return [f.suggestion for f in self.findings if f.suggestion]

    @property
    def score(self) -> float:
        """Numeric score: 100 minus weighted penalty from findings.

        Penalty weights and floor are calibrated by task difficulty so
        that simple mechanical tasks (constant extraction, etc.) aren't
        penalized as harshly for minor or hallucinated findings.
        """
        if not self.findings:
            return 100.0
        weights = DIFFICULTY_WEIGHTS.get(self.difficulty, DIFFICULTY_WEIGHTS["medium"])
        penalty = sum(
            weights.get(f.severity.value, 5) for f in self.findings
        )
        floor = DIFFICULTY_FLOORS.get(self.difficulty, DIFFICULTY_FLOORS["medium"])
        return max(floor, 100.0 - penalty)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "score": self.score,
            "findings": [f.to_dict() for f in self.findings],
            "lens_used": self.lens_used,
            "confidence": self.confidence,
            "gaps": self.gaps,
            "suggestions": self.suggestions,
            "coevolution": self.coevolution.to_dict() if self.coevolution else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# Lens selection by task domain

DOMAIN_LENS: dict[str, list[LensType]] = {
    "code-implementation": [LensType.CORRECTNESS, LensType.SECURITY, LensType.COMPLETENESS],
    "code-review": [LensType.CORRECTNESS, LensType.SECURITY, LensType.COMPLETENESS],
    "debugging": [LensType.CORRECTNESS, LensType.SECURITY, LensType.COMPLETENESS],
    "python-testing": [LensType.CORRECTNESS, LensType.COMPLETENESS],
    "git-operations": [LensType.CORRECTNESS, LensType.COMPLETENESS],
    "planning": [LensType.CORRECTNESS, LensType.COMPLETENESS],
    "analysis": [LensType.CORRECTNESS, LensType.COMPLETENESS],
    "triage-category": [LensType.CORRECTNESS, LensType.COMPLETENESS],
}

DEFAULT_LENSES = [LensType.CORRECTNESS, LensType.COMPLETENESS]

# ── Difficulty-aware severity weights ──
# Penalties are lighter for easy/medium tasks so minor nitpicks don't
# tank the score on simple mechanical work (constant extraction, etc.).
# CRITICAL stays at 25 across all difficulties — real bugs are real bugs.

DIFFICULTY_WEIGHTS: dict[str, dict[str, int]] = {
    "easy": {"CRITICAL": 25, "HIGH": 8, "MEDIUM": 4, "LOW": 2},
    "medium": {"CRITICAL": 25, "HIGH": 12, "MEDIUM": 6, "LOW": 3},
    "hard": {"CRITICAL": 25, "HIGH": 15, "MEDIUM": 8, "LOW": 3},
    "diploma": {"CRITICAL": 25, "HIGH": 15, "MEDIUM": 8, "LOW": 3},
}

# Higher floor means easy tasks can't be penalized into irrelevance.
DIFFICULTY_FLOORS: dict[str, float] = {
    "easy": 50.0,
    "medium": 40.0,
    "hard": 30.0,
    "diploma": 30.0,
}

# System prompt calibration blocks — injected per-difficulty to guide the model's strictness.
DIFFICULTY_CALIBRATION: dict[str, str] = {
    "easy": (
        "[DIFFICULTY CALIBRATION — IMPORTANT]\n"
        "This is an EASY task — a simple, mechanical change (e.g. extracting a constant, "
        "renaming a variable, fixing a typo). Only flag ACTUAL bugs or correctness issues. "
        "Do NOT invent edge cases, completeness concerns, or style suggestions for trivial changes.\n"
        "If the output correctly implements the simple change, report NO findings.\n\n"
    ),
    "medium": (
        "[DIFFICULTY CALIBRATION]\n"
        "This is a MEDIUM task. Apply standard review rigor. "
        "Flag actual bugs, missing edge cases, and correctness issues. "
        "Avoid minor style or convention nitpicks.\n\n"
    ),
    "hard": (
        "[DIFFICULTY CALIBRATION]\n"
        "This is a HARD task. Apply full adversarial rigor. "
        "Every flaw, edge case, and implicit assumption should be flagged.\n\n"
    ),
    "diploma": (
        "[DIFFICULTY CALIBRATION]\n"
        "This is a DIPLOMA/COMPLEX task. Maximum scrutiny. "
        "Flag every issue — correctness, completeness, security, edge cases.\n\n"
    ),
}


def select_lenses(task_domain: str, override: list[LensType] | None = None) -> list[LensType]:
    """Select adversarial lenses based on task domain.

    Override takes precedence. Falls back to defaults if domain is unknown.
    """
    if override:
        return override
    return DOMAIN_LENS.get(task_domain, DEFAULT_LENSES)


def extract_balanced_json(text: str, open_char: str, close_char: str) -> str:
    """Extract a balanced JSON block from *text* starting at the first *open_char*.

    Counts nesting depth to find the matching closing character, so
    nested objects/arrays inside the JSON don't confuse the extractor.
    Returns the substring from the first *open_char* through the
    matching *close_char* (inclusive), or the original *text* if
    balancing fails.
    """
    start = text.find(open_char)
    if start < 0:
        return text
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text  # unbalanced — return as-is for json.loads to reject


class AdversarialReviewer:
    """Core adversarial review engine.

    Applies one or more lenses to a student's output and produces
    a structured ReviewResult with verdict, findings, and score.
    """

    def __init__(self, call_model_fn: Callable):
        """Initialize with a model call function.

        call_model_fn(prompt: str, system_prompt: Optional[str] = None, **kwargs) -> str
        """
        self._call_model = call_model_fn
        self._stats: dict[str, list[bool]] = {}  # lens_name -> [found_issues_bool]

    def review(
        self,
        output: str,
        task: dict,
        codebase_context: str = "",
        lens_types: list[LensType] | None = None,
        circuit_breaker: bool = True,
    ) -> ReviewResult:
        """Review a student's output against the task.

        Args:
            output: The student's response (code, analysis, plan, etc.)
            task: Dict with at least "title", "body", "domain" keys
            codebase_context: Same context the student received
            lens_types: Override automatic lens selection
            circuit_breaker: If True, escalate PASS/0-findings to second review

        Returns:
            ReviewResult with verdict, findings, score
        """
        if not output or not output.strip():
            return ReviewResult(
                verdict=Verdict.FAIL,
                findings=[Finding(
                    section="output",
                    issue_class="empty_output",
                    severity=Severity.HIGH,
                    citation="N/A",
                    description="No output produced",
                    suggestion="Student should attempt the task rather than returning empty output",
                )],
                lens_used="none",
                confidence=1.0,
                difficulty=task.get("difficulty", "medium"),
            )

        domain = task.get("domain", "general")
        difficulty = task.get("difficulty", "medium")
        lenses = select_lenses(domain, lens_types)

        all_findings: list[Finding] = []
        # Per-lens trace: lens_value -> ReviewResult. Captured for the
        # verification-co-evolution loop (see VerificationCoevolution.analyze),
        # which uses real per-axis capability rather than the merged aggregate.
        lens_trace: dict[str, ReviewResult] = {}
        primary_lens = lenses[0]
        result = self._apply_lens(primary_lens, output, task, codebase_context, difficulty=difficulty)
        all_findings.extend(result.findings)
        lens_trace[primary_lens.value] = result

        # Circuit breaker: if PASS with 0 findings, try second lens
        if (
            circuit_breaker
            and result.verdict == Verdict.PASS
            and not result.findings
            and len(lenses) > 1
        ):
            second_lens = lenses[1]
            second_result = self._apply_lens(second_lens, output, task, codebase_context, difficulty=difficulty)
            lens_trace[second_lens.value] = second_result
            if second_result.findings:
                all_findings.extend(second_result.findings)
                result = ReviewResult(
                    verdict=Verdict.FAIL if second_result.findings else Verdict.PASS,
                    findings=all_findings,
                    lens_used=f"{primary_lens.value}+{second_lens.value}(circuit_breaker)",
                    confidence=second_result.confidence,
                    difficulty=difficulty,
                )
            else:
                # Both lenses agree: PASS with 0 findings — flag as potential sycophancy
                logger.warning(
                    "circuit_breaker_double_pass",
                    extra={
                        "task_domain": domain,
                        "primary_lens": primary_lens.value,
                        "second_lens": second_lens.value,
                        "difficulty": difficulty,
                    },
                )
                result.lens_used = f"{primary_lens.value}+{second_lens.value}(double_pass)"

        elif len(lenses) > 1 and not all_findings:
            # First lens found nothing — try remaining lenses for coverage
            for lens in lenses[1:]:
                lens_result = self._apply_lens(lens, output, task, codebase_context, difficulty=difficulty)
                lens_trace[lens.value] = lens_result
                all_findings.extend(lens_result.findings)

        has_critical_or_high = any(
            f.severity in (Severity.CRITICAL, Severity.HIGH) for f in all_findings
        )
        verdict = Verdict.FAIL if has_critical_or_high else Verdict.PASS

        final_result = ReviewResult(
            verdict=verdict,
            findings=all_findings,
            lens_used=",".join(l.value for l in lenses),
            confidence=self._calculate_confidence(all_findings),
            difficulty=difficulty,
        )
        # Attach the per-lens trace for the co-evolution loop (non-API attr).
        final_result._lens_trace = lens_trace  # type: ignore[attr-defined]

        self._update_stats(lenses, bool(all_findings))
        return final_result

    def _apply_lens(
        self,
        lens_type: LensType,
        output: str,
        task: dict,
        codebase_context: str,
        difficulty: str = "medium",
    ) -> ReviewResult:
        """Apply a single lens to the output.

        Dispatches to the lens-specific prompt template and model call.
        On parse failure (confidence=0.0, no findings), retries once with
        a stricter JSON-only prompt before falling back to PASS/empty.
        In test/CI environments without model access, returns a stub result.
        """
        from lenses import get_lens_prompt

        lens_prompt = get_lens_prompt(lens_type)
        system_prompt = self._build_system_prompt(lens_prompt, difficulty=difficulty)
        user_prompt = self._build_user_prompt(output, task, codebase_context)

        try:
            raw = self._call_model(user_prompt, system_prompt=system_prompt)
            result = self._parse_lens_output(raw, lens_type.value, difficulty=difficulty)

            # Retry on parse failure: confidence=0.0 + no findings + non-trivial
            # raw indicates the model output couldn't be parsed as JSON.
            if result.confidence == 0.0 and not result.findings and len(raw) > 20:
                logger.warning(
                    "lens_retry_on_parse_failure",
                    extra={
                        "lens": lens_type.value,
                        "raw_len": len(raw),
                        "raw": raw,  # Full output for diagnostic analysis
                    },
                )
                # Stricter retry prompt — insist on JSON-only output
                retry_system = (
                    "You are an adversarial code reviewer. Your ONLY job is to find flaws.\n\n"
                    f"[DIFFICULTY] {difficulty} — adjust strictness proportionally.\n\n"
                    "[CRITICAL — OUTPUT FORMAT]\n"
                    "You MUST respond with ONLY valid JSON. NO markdown, NO code fences, "
                    "NO preamble, NO explanation. Start with '{' and end with '}'.\n\n"
                    "Format:\n"
                    '{"findings": [{"section": "...", "issue_class": "...", '
                    '"severity": "CRITICAL|HIGH|MEDIUM|LOW", '
                    '"citation": "...", "description": "...", "suggestion": "..."}]}\n'
                    "If no issues: {\"findings\": []}"
                )
                retry_user = (
                    "[TASK]\n"
                    f"Domain: {task.get('domain', 'unknown')}\n"
                    f"Difficulty: {difficulty}\n"
                    f"Title: {task.get('title', 'N/A')}\n\n"
                    "[OUTPUT TO REVIEW]\n"
                    f"{output[:3000]}"
                )
                try:
                    raw2 = self._call_model(retry_user, system_prompt=retry_system)
                    result2 = self._parse_lens_output(raw2, f"{lens_type.value}(retry)", difficulty=difficulty)
                    # Use retry result if it parsed successfully
                    if result2.confidence > 0 or result2.findings:
                        logger.warning(
                            "lens_retry_succeeded",
                            extra={
                                "lens": lens_type.value,
                                "findings": len(result2.findings),
                            },
                        )
                        return result2
                except Exception as e2:
                    logger.warning(
                        "lens_retry_failed",
                        extra={"lens": lens_type.value, "error": str(e2)},
                    )

            return result
        except Exception as e:
            logger.warning("lens_review_failed", extra={"lens": lens_type.value, "error": str(e)})
            return ReviewResult(
                verdict=Verdict.PASS,
                findings=[],
                lens_used=lens_type.value,
                confidence=0.0,
                difficulty=difficulty,
            )

    def _build_system_prompt(self, lens_prompt: str, difficulty: str = "medium") -> str:
        """Build the system prompt for an adversarial lens.

        Injects task difficulty so the model adjusts its strictness:
        simple mechanical tasks should only flag actual bugs, not
        style or edge-case concerns for trivial changes.
        """
        calib_block = DIFFICULTY_CALIBRATION.get(difficulty, DIFFICULTY_CALIBRATION["medium"])

        return (
            "You are an adversarial code reviewer. Your ONLY job is to find flaws, "
            "gaps, and issues. Do NOT praise, do NOT suggest alternatives, "
            "do NOT rewrite code.\n\n"
            f"{calib_block}"
            f"[LENS]\n{lens_prompt}\n\n"
            "[OUTPUT FORMAT — MANDATORY]\n"
            "You MUST respond with EXACTLY one JSON object and NOTHING else. "
            "No markdown, no preamble, no explanation, no code blocks. "
            "Only this exact structure:\n"
            '{"findings": [{"section": "...", '
            '"issue_class": "...", "severity": "CRITICAL|HIGH|MEDIUM|LOW", '
            '"citation": "...", "description": "...", "suggestion": "..."}]}\n\n'
            "If you find no issues, respond with: {\"findings\": []}"
        )

    def _build_user_prompt(self, output: str, task: dict, codebase_context: str) -> str:
        """Build the user prompt with task context and student output."""
        parts = [
            "[TASK]",
            f"Domain: {task.get('domain', 'unknown')}",
            f"Difficulty: {task.get('difficulty', 'medium')}",
            f"Title: {task.get('title', 'N/A')}",
            f"Description: {task.get('body', task.get('prompt', 'N/A'))}",
        ]

        if codebase_context:
            parts.append(f"\n[CODEBASE CONTEXT]\n{codebase_context}")

        parts.append(f"\n[STUDENT OUTPUT]\n{output}")
        parts.append("\nReview this output against the task. Output ONLY a JSON object with your findings — no markdown, no explanation, no other text.")

        return "\n".join(parts)

    @staticmethod
    def _extract_balanced_json(text: str, open_char: str, close_char: str) -> str:
        """Thin wrapper — delegates to module-level :func:`extract_balanced_json`."""
        return extract_balanced_json(text, open_char, close_char)

    def _parse_lens_output(self, raw: str, lens_name: str, difficulty: str = "medium") -> ReviewResult:
        """Parse the model's structured JSON output into ReviewResult.

        Handles common model output quirks:
        - Markdown code fences (```json ... ```)
        - Preamble text before the JSON
        - Unwrapped arrays when the model drops {"findings": [...]}
        - Control characters and Unicode whitespace
        """
        import re

        # --- Step 1: normalise the raw string ---
        cleaned = raw.strip()
        # Strip control characters that break JSON parsing
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', cleaned)

        # --- Step 2: collect all JSON candidates ---
        candidates: list[str] = []
        if "```" in cleaned:
            for match in re.finditer(r"```(?:json)?\s*\n?([\s\S]+?)```", cleaned):
                candidates.append(match.group(1).strip())
        candidates.append(cleaned)  # fallback: the whole string

        # Strip leading "json" / "JSON" keyword some models emit
        candidates = [re.sub(r"^(?:json|JSON)\s*", "", c) for c in candidates]

        # --- Step 3: try each candidate, object strategy first, then array ---
        data = None
        try:
            for candidate_str in candidates:
                for strategy in ("object", "array"):
                    try:
                        if strategy == "object":
                            idx = candidate_str.find("{")
                            if idx < 0:
                                continue
                            candidate = candidate_str[idx:]
                            candidate = extract_balanced_json(candidate, "{", "}")
                        else:
                            idx = candidate_str.find("[")
                            if idx < 0:
                                continue
                            candidate = candidate_str[idx:]
                            candidate = extract_balanced_json(candidate, "[", "]")

                        parsed = json.loads(candidate, strict=False)
                        if strategy == "array" and isinstance(parsed, list):
                            data = {"findings": parsed}
                        elif strategy == "object" and isinstance(parsed, dict) and "findings" in parsed:
                            data = parsed
                        if data is not None:
                            break
                    except (json.JSONDecodeError, ValueError):
                        continue
                if data is not None:
                    break
        except Exception as e:
            logger.warning(
                "lens_parse_failed",
                extra={"lens": lens_name, "raw_len": len(raw), "raw": raw[:2000], "error": str(e)},
            )
            return ReviewResult(verdict=Verdict.PASS, findings=[], lens_used=lens_name, difficulty=difficulty)

        if data is None:
            logger.warning(
                "lens_parse_failed",
                extra={"lens": lens_name, "raw_len": len(raw), "raw": raw[:2000]},
            )
            return ReviewResult(verdict=Verdict.PASS, findings=[], lens_used=lens_name, difficulty=difficulty)

        findings = []
        string_count = 0
        MAX_STRING_FINDINGS = 5  # Cap string entries to prevent score bottoming out
        for entry in data.get("findings", []):
            try:
                # Handle string-only entries: model returns ["desc1", "desc2"]
                if isinstance(entry, str):
                    string_count += 1
                    if string_count > MAX_STRING_FINDINGS:
                        continue
                    # First string finding triggers FAIL (HIGH), rest are MEDIUM
                    # so the score degrades proportionally with count
                    sev = Severity.HIGH if string_count == 1 else Severity.MEDIUM
                    findings.append(Finding(
                        section="output",
                        issue_class="review_finding",
                        severity=sev,
                        citation="",
                        description=entry,
                    ))
                    continue
                findings.append(Finding(
                    section=entry.get("section", "unknown"),
                    issue_class=entry.get("issue_class", "unknown"),
                    severity=Severity(entry.get("severity", "LOW")),
                    citation=entry.get("citation", ""),
                    description=entry.get("description", ""),
                    suggestion=entry.get("suggestion"),
                ))
            except (ValueError, KeyError):
                continue

        has_critical = any(f.severity in (Severity.CRITICAL, Severity.HIGH) for f in findings)

        return ReviewResult(
            verdict=Verdict.FAIL if has_critical else Verdict.PASS,
            findings=findings,
            lens_used=lens_name,
            confidence=min(1.0, len(findings) * 0.3 + 0.2) if findings else 0.5,
            difficulty=difficulty,
        )

    def _calculate_confidence(self, findings: list[Finding]) -> float:
        """Calculate confidence based on finding count and consistency."""
        if not findings:
            return 0.5  # Neither confident nor uncertain
        return min(1.0, len(findings) * 0.25 + 0.3)

    def _update_stats(self, lenses: list[LensType], found_issues: bool) -> None:
        """Track per-lens statistics for circuit breaker monitoring."""
        for lens in lenses:
            key = lens.value
            if key not in self._stats:
                self._stats[key] = []
            self._stats[key].append(found_issues)

    def get_agreement_rate(self, lens_type: LensType, window: int = 50) -> float:
        """Get the pass rate (no findings) for a lens over the last N reviews.

        High agreement rate (>0.85) suggests the lens may be drifiting toward sycophancy.
        """
        key = lens_type.value
        history = self._stats.get(key, [])
        if not history:
            return 0.0
        recent = history[-window:]
        return sum(1 for found in recent if not found) / len(recent)

    def flag_drifting_lenses(self, threshold: float = 0.85) -> list[LensType]:
        """Return lenses whose agreement rate exceeds the threshold."""
        drifting = []
        for lens_type in LensType:
            rate = self.get_agreement_rate(lens_type)
            if rate > threshold and lens_type.value in self._stats:
                drifting.append(lens_type)
        return drifting


# ─────────────────────────────────────────────────────────────────────────────
# Verification-co-evolution loop
# ─────────────────────────────────────────────────────────────────────────────
#
# The "Verification Horizon" problem: as the agent/harness improves, a *fixed*
# acceptance check (a verify command, a heuristic, a threshold) becomes a reward
# to be gamed rather than a constraint to be met. The check stops measuring real
# quality and starts being a target the agent optimizes against — reward hacking.
#
# This module closes that loop. After each review/verification pass we RECORD what
# the agent achieved (the harness capability at that moment). When a later pass
# shows the agent has improved *on a dimension the current checks do not cover*,
# we (a) flag the coverage gap and (b) optionally regenerate/harden the acceptance
# checks so the next evaluation actually measures the new capability instead of
# letting the agent coast on a stale test.
#
# The loop is intentionally transparent and human-in-the-loop: regeneration is
# decidable (a proposal, not a silent mutation of production checks). A strategy
# hook lets callers plug in real LLM-driven regeneration; the default strategy is
# a cheap, deterministic rule-based hardener.

# A harness is considered "improved" on a dimension when the new measured score on
# that dimension exceeds the recorded capability by at least this margin.
CAPABILITY_GAIN_MARGIN = 0.10  # 10 percentage points

# How long (in reviews) a capability baseline stays valid before we treat a bump
# as a genuine improvement rather than noise.
CAPABILITY_WINDOW = 25

# A dimension whose acceptance coverage is below this is a candidate for hardening.
COVERAGE_THRESHOLD = 0.5


@dataclass
class CoevolutionReport:
    """Result of a verification-co-evolution analysis pass.

    Always serializable (to_dict) so it can be persisted in the bookbag and
    surfaced to a human reviewer alongside the review verdict.
    """

    triggered: bool = False                       # Did the loop fire at all?
    capability_delta: dict[str, float] = field(default_factory=dict)  # dimension -> score gain
    coverage_gaps: list[dict] = field(default_factory=list)          # uncovered/under-covered dims
    proposals: list[dict] = field(default_factory=list)              # check-regeneration proposals
    actions_applied: list[str] = field(default_factory=list)         # what was actually changed
    reason: str = ""                                               # human-readable summary

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "capability_delta": self.capability_delta,
            "coverage_gaps": self.coverage_gaps,
            "proposals": self.proposals,
            "actions_applied": self.actions_applied,
            "reason": self.reason,
        }


def _dimensions_from_review(result: "ReviewResult", task: dict) -> dict[str, float]:
    """Map a review result onto a normalized capability vector (0..1 per dimension).

    Dimensions are keyed by the lenses that ran plus the task domain so that
    capability is tracked per-axis. A PASS with no critical findings => high
    capability on that axis; findings degrade it proportionally to severity.
    """
    dims: dict[str, float] = {}
    severity_penalty = {"CRITICAL": 0.9, "HIGH": 0.6, "MEDIUM": 0.3, "LOW": 0.1}
    # Per-lens-type rollup from the findings we can see.
    from collections import defaultdict
    lens_trace = getattr(result, "_lens_trace", None)
    if lens_trace:
        for lens_value, fres in lens_trace.items():
            pen = sum(severity_penalty.get(f.severity.value, 0.1) for f in fres.findings)
            dims[lens_value] = max(0.0, 1.0 - pen)
    else:
        # Fallback: single aggregate dimension from the whole result.
        pen = sum(severity_penalty.get(f.severity.value, 0.1) for f in result.findings)
        dims["overall"] = max(0.0, 1.0 - pen)

    # NOTE: we intentionally do NOT also inject a per-domain axis. The lens axes
    # (correctness/security/completeness) ARE the capability dimensions the
    # acceptance checks cover, so adding a separate `domain` axis would create a
    # phantom uncovered dimension on every gain. Callers that want per-domain
    # tracking can map domain -> their own checks' `covers` keys.
    return dims


class VerificationCoevolution:
    """Stateful engine that co-evolves acceptance checks with agent capability.

    Usage:
        coevo = VerificationCoevolution(call_model_fn=reviewer._call_model)
        report = coevo.analyze(review_result, task, acceptance_checks)
        review_result.coevolution = report
        if report.triggered:
            # surface report.proposals to a human, or
            applied = coevo.apply_proposals(report, acceptance_checks,
                                            strategy=my_regenerator)
    """

    def __init__(
        self,
        call_model_fn: Callable | None = None,
        capability_margin: float = CAPABILITY_GAIN_MARGIN,
        coverage_threshold: float = COVERAGE_THRESHOLD,
        window: int = CAPABILITY_WINDOW,
    ):
        self._call_model = call_model_fn
        self._capability_margin = capability_margin
        self._coverage_threshold = coverage_threshold
        self._window = window
        # dimension -> list of recent capability scores (ring buffer)
        self._history: dict[str, list[float]] = {}

    # ── capability tracking ──
    def _record(self, dims: dict[str, float]) -> dict[str, float]:
        """Record new capability measurements and return dimension -> delta vs window-avg."""
        delta: dict[str, float] = {}
        for dim, score in dims.items():
            buf = self._history.setdefault(dim, [])
            buf.append(score)
            if len(buf) > self._window:
                buf.pop(0)
            if len(buf) >= 2:
                baseline = sum(buf[:-1]) / (len(buf) - 1)
                d = score - baseline
                # Epsilon guard: a true gain of exactly `margin` (e.g. a LOW
                # finding removed) is represented as ~0.09999998 in float and
                # would otherwise fail the `>=` check against 0.10.
                if d >= self._capability_margin - 1e-9:
                    delta[dim] = round(d, 4)
        return delta

    def capability_for(self, dimension: str) -> float | None:
        buf = self._history.get(dimension)
        if not buf:
            return None
        return sum(buf) / len(buf)

    # ── analysis ──
    def analyze(
        self,
        result: "ReviewResult",
        task: dict,
        acceptance_checks: list[dict] | None = None,
    ) -> "CoevolutionReport":
        """Run one co-evolution pass.

        Args:
            result: the just-produced ReviewResult (must carry a lens trace via
                ``result._lens_trace`` for per-axis coverage; otherwise an
                aggregate dimension is used).
            task: the task dict (needs at least a ``domain`` key).
            acceptance_checks: the current acceptance checks being used, each a
                dict with at least a ``covers`` key naming the dimension(s) it
                gates. Used to detect coverage gaps.

        Returns:
            CoevolutionReport describing what (if anything) should change.
        """
        dims = _dimensions_from_review(result, task)
        delta = self._record(dims)

        report = CoevolutionReport()
        if not delta:
            report.reason = "No capability gain detected above margin; checks unchanged."
            return report

        report.triggered = True
        report.capability_delta = delta

        # Coverage analysis: which gained dimensions lack a gating acceptance check?
        checks = acceptance_checks or []
        covered = set()
        for chk in checks:
            covered.update(chk.get("covers", []) if isinstance(chk.get("covers"), (list, tuple))
                          else [str(chk.get("covers", ""))])
        for dim in delta:
            if dim not in covered:
                report.coverage_gaps.append({
                    "dimension": dim,
                    "gain": delta[dim],
                    "covered": False,
                    "note": "Agent improved on a dimension no acceptance check covers.",
                })
            report.proposals.append(self._propose_check(dim, delta[dim], task))

        if report.coverage_gaps:
            report.reason = (
                f"Agent capability rose on {len(delta)} dimension(s); "
                f"{len(report.coverage_gaps)} lack a gating acceptance check — "
                "stale checks risk reward hacking. Regenerate/harden proposed."
            )
        else:
            report.reason = (
                f"Agent capability rose on {len(delta)} dimension(s); all are "
                "covered by existing checks. Monitor only."
            )
        return report

    @staticmethod
    def _propose_check(dimension: str, gain: float, task: dict) -> dict:
        """Build a deterministic, rule-based regeneration proposal for a dimension.

        This is the *default* hardener. A caller may pass a richer ``strategy`` to
        ``apply_proposals`` to use an LLM to draft the actual check content. The
        proposal records intent + rationale so a human can approve before
        anything mutates the production check set.
        """
        return {
            "dimension": dimension,
            "gain": gain,
            "action": "regenerate_or_harden",
            "rationale": (
                f"Capability on '{dimension}' improved by {gain:.0%}; the current "
                "acceptance surface does not measure this. Add/extend a check that "
                "exercises this dimension so the gain is verified, not rewarded blindly."
            ),
            "spec": {
                "covers": [dimension],
                "domain": task.get("domain", "general"),
                "kind": "acceptance_check",
            },
        }

    def apply_proposals(
        self,
        report: "CoevolutionReport",
        acceptance_checks: list[dict],
        strategy: Callable[[dict], dict] | None = None,
    ) -> list[dict]:
        """Apply co-evolution proposals to the acceptance-check list.

        Args:
            report: a CoevolutionReport produced by :meth:`analyze`.
            acceptance_checks: the current check list (mutated in place).
            strategy: optional callable ``proposal -> check_dict`` that *generates*
                the actual check content (e.g. an LLM that writes a test). If omitted,
                a conservative placeholder check is appended so the gap is at least
                acknowledged and re-measured next cycle.

        Returns:
            The list of newly-added check dicts.
        """
        added: list[dict] = []
        for prop in report.proposals:
            if strategy is not None:
                try:
                    new_check = strategy(prop)
                except Exception as e:  # strategy failure must not corrupt the loop
                    logger.warning("coevolution_strategy_failed", extra={"error": str(e), "dim": prop.get("dimension")})
                    new_check = self._placeholder_check(prop)
            else:
                new_check = self._placeholder_check(prop)
            if new_check:
                acceptance_checks.append(new_check)
                added.append(new_check)
                report.actions_applied.append(
                    f"added check covering '{prop['dimension']}'"
                )
        return added

    @staticmethod
    def _placeholder_check(prop: dict) -> dict:
        """Conservative, human-reviewable placeholder when no strategy is supplied.

        We deliberately DO NOT invent test bodies — we record intent so the next
        verification cycle treats the dimension as covered (re-measured) but a human
        still has to write/approve the real check. This avoids the loop silently
        colonizing the check set with unverified assertions.
        """
        spec = prop.get("spec", {})
        return {
            "covers": spec.get("covers", [prop.get("dimension")]),
            "domain": spec.get("domain", "general"),
            "kind": "acceptance_check",
            "status": "proposed",  # NOT 'active' — must be approved before it gates
            "rationale": prop.get("rationale", ""),
            "gain": prop.get("gain"),
        }


# Convenience orchestration hook so callers can run a single co-evolved review
# without standing up a VerificationCoevolution instance themselves.
def review_with_coevolution(
    reviewer: "AdversarialReviewer",
    output: str,
    task: dict,
    codebase_context: str = "",
    lens_types: list["LensType"] | None = None,
    acceptance_checks: list[dict] | None = None,
    coevo: "VerificationCoevolution | None" = None,
) -> "ReviewResult":
    """Run ``reviewer.review`` and attach a co-evolution report.

    The returned ``ReviewResult`` has its ``coevolution`` field populated so the
    downstream pipeline (director two-judge, bookbag) can see whether the
    acceptance surface needs hardening. This is the single seam the rest of the
    codebase should call to get verification-co-evolution "for free".
    """
    # Run lenses individually so we can capture per-lens findings for the trace.
    if lens_types is None:
        domain = task.get("domain", "general")
        lens_types = select_lenses(domain)
    result = reviewer.review(
        output=output, task=task, codebase_context=codebase_context, lens_types=lens_types,
    )
    # reviewer.review() already populated result._lens_trace (per-lens breakdown).
    # analyze() uses it for real per-axis capability; absent it, analyze() degrades
    # gracefully to an aggregate dimension. We never overwrite it here.
    if coevo is None:
        coevo = VerificationCoevolution(call_model_fn=reviewer._call_model)
    result.coevolution = coevo.analyze(result, task, acceptance_checks)
    return result

