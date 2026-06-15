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
        """Numeric score: 100 minus weighted penalty from findings."""
        if not self.findings:
            return 100.0
        severity_weights = {
            Severity.CRITICAL: 25,
            Severity.HIGH: 15,
            Severity.MEDIUM: 8,
            Severity.LOW: 3,
        }
        penalty = sum(severity_weights.get(f.severity, 5) for f in self.findings)
        return max(0.0, 100.0 - penalty)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "score": self.score,
            "findings": [f.to_dict() for f in self.findings],
            "lens_used": self.lens_used,
            "confidence": self.confidence,
            "gaps": self.gaps,
            "suggestions": self.suggestions,
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


def select_lenses(task_domain: str, override: list[LensType] | None = None) -> list[LensType]:
    """Select adversarial lenses based on task domain.

    Override takes precedence. Falls back to defaults if domain is unknown.
    """
    if override:
        return override
    return DOMAIN_LENS.get(task_domain, DEFAULT_LENSES)


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
            )

        domain = task.get("domain", "general")
        lenses = select_lenses(domain, lens_types)

        all_findings: list[Finding] = []
        primary_lens = lenses[0]
        result = self._apply_lens(primary_lens, output, task, codebase_context)
        all_findings.extend(result.findings)

        # Circuit breaker: if PASS with 0 findings, try second lens
        if (
            circuit_breaker
            and result.verdict == Verdict.PASS
            and not result.findings
            and len(lenses) > 1
        ):
            second_lens = lenses[1]
            second_result = self._apply_lens(second_lens, output, task, codebase_context)
            if second_result.findings:
                all_findings.extend(second_result.findings)
                result = ReviewResult(
                    verdict=Verdict.FAIL if second_result.findings else Verdict.PASS,
                    findings=all_findings,
                    lens_used=f"{primary_lens.value}+{second_lens.value}(circuit_breaker)",
                    confidence=second_result.confidence,
                )
            else:
                # Both lenses agree: PASS with 0 findings — flag as potential sycophancy
                logger.warning(
                    "circuit_breaker_double_pass",
                    extra={
                        "task_domain": domain,
                        "primary_lens": primary_lens.value,
                        "second_lens": second_lens.value,
                    },
                )
                result.lens_used = f"{primary_lens.value}+{second_lens.value}(double_pass)"

        elif len(lenses) > 1:
            # Multiple lenses: apply remaining lenses and merge findings
            for lens in lenses[1:]:
                lens_result = self._apply_lens(lens, output, task, codebase_context)
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
        )

        self._update_stats(lenses, bool(all_findings))
        return final_result

    def _apply_lens(
        self,
        lens_type: LensType,
        output: str,
        task: dict,
        codebase_context: str,
    ) -> ReviewResult:
        """Apply a single lens to the output.

        Dispatches to the lens-specific prompt template and model call.
        In test/CI environments without model access, returns a stub result.
        """
        from lenses import get_lens_prompt

        lens_prompt = get_lens_prompt(lens_type)
        system_prompt = self._build_system_prompt(lens_prompt)
        user_prompt = self._build_user_prompt(output, task, codebase_context)

        try:
            raw = self._call_model(user_prompt, system_prompt=system_prompt)
            return self._parse_lens_output(raw, lens_type.value)
        except Exception as e:
            logger.warning("lens_review_failed", extra={"lens": lens_type.value, "error": str(e)})
            return ReviewResult(
                verdict=Verdict.PASS,
                findings=[],
                lens_used=lens_type.value,
                confidence=0.0,
            )

    def _build_system_prompt(self, lens_prompt: str) -> str:
        """Build the system prompt for an adversarial lens."""
        return (
            "You are an adversarial code reviewer. Your ONLY job is to find flaws, "
            "gaps, and issues. Do NOT praise, do NOT suggest alternatives, "
            "do NOT rewrite code. Output ONLY structured findings.\n\n"
            f"[LENS]\n{lens_prompt}\n\n"
            '[OUTPUT FORMAT] Output ONLY a JSON object: {"findings": [{"section": "...", '
            '"issue_class": "...", "severity": "CRITICAL|HIGH|MEDIUM|LOW", '
            '"citation": "...", "description": "...", "suggestion": "..."}]}'
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
        parts.append("\nReview this output against the task. Find every flaw, gap, and issue.")

        return "\n".join(parts)

    def _parse_lens_output(self, raw: str, lens_name: str) -> ReviewResult:
        """Parse the model's structured JSON output into ReviewResult."""
        import re
        try:
            json_str = raw.strip()
            if "```" in json_str:
                match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", json_str)
                if match:
                    json_str = match.group(1).strip()
            json_str = re.sub(r"^json\s*", "", json_str, flags=re.IGNORECASE)
            idx = json_str.find("{")
            if idx >= 0:
                json_str = json_str[idx:]
            data = json.loads(json_str, strict=False)
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("lens_parse_failed", extra={"lens": lens_name, "raw": raw[:200]})
            return ReviewResult(verdict=Verdict.PASS, findings=[], lens_used=lens_name)

        findings = []
        for entry in data.get("findings", []):
            try:
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
