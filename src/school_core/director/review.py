"""Director review flow for the school-core pipeline.

Owns the CTO+COO two-judge adversarial review, Orca execution sandbox,
repo verify gate, and the verification-co-evolution pass.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from adversarial_reviewer import (
    AdversarialReviewer,
    LensType,
    Verdict,
    Finding,
    Severity,
    VerificationCoevolution,
    ReviewResult,
)
from bookbag import update_bookbag, REPO_GLOBAL
from executor import call_model
from orca_executor import OrcaExecutionManager, CodeExtractor, OrcaUnavailableError
from pipeline_metrics import PipelineMetrics
from review_packet import ReviewPacket
from verify_gate import run_verify_gate
from school_core.director.scoring import _acceptance_checks_from_spec, _synthesize_judge_narratives
from school_core.paths import REPO_ROOT


def _run_two_judge_review(
    bead: str,
    output: str,
    task: dict,
    codebase_context: str = "",
    role: str = "reviewer",
    repo: str = REPO_GLOBAL,
    repo_path: Optional[Path] = None,
    preverified_verification: Optional[dict] = None,
    pipeline_metrics: Optional[PipelineMetrics] = None,
    synthesize_narratives: Optional[bool] = None,
) -> dict:
    """Run CTO+COO two-judge adversarial review on student output.

    CTO (Chief Technical Officer): correctness + security lenses.
    COO (Chief Operating Officer): completeness + acceptance criteria.

    Both must return PASS for the work to be accepted.
    Returns dict with cto_verdict, coo_verdict, combined findings, and accepted flag.
    """
    if synthesize_narratives is None:
        synthesize_narratives = True

    def _call_model(prompt, sp=None, **kw):
        try:
            response = call_model(
                role, prompt, system_prompt=sp,
                timeout=kw.get("timeout", 90),
            )
        except Exception:
            if pipeline_metrics is not None:
                pipeline_metrics.record_model(
                    role,
                    prompt_chars=len(prompt or ""),
                )
            raise
        if pipeline_metrics is not None:
            pipeline_metrics.record_model(
                role,
                prompt_chars=len(prompt or ""),
                output_chars=len(response or ""),
            )
        return response

    if pipeline_metrics is not None:
        pipeline_metrics.record_call("two_judge_review")

    # ── Orca Execution ──
    execution_findings: list = []
    executable_domains = {"python-coding", "python-testing", "code-implementation"}
    if task.get("domain") in executable_domains:
        orca = OrcaExecutionManager()
        try:
            orca_repo_path = _resolve_repo_path(repo, explicit_path=repo_path)
            lang = CodeExtractor.language_for_domain(
                task.get("domain", ""),
                repo_path=orca_repo_path,
            )
            if lang is not None and lang != "python":
                execution_findings.append(Finding(
                    section="execution",
                    issue_class="language_not_supported",
                    severity=Severity.LOW,
                    citation=f"detected_language={lang}",
                    description=f"Orca sandbox only supports Python; skipping execution for {lang} project",
                    suggestion="",
                ))
            else:
                code = CodeExtractor.extract(output, language=lang)
                if code.strip():
                    result = orca.execute(code=code, bead=bead, timeout_ms=30000)

                    if result.timed_out:
                        execution_findings.append(Finding(
                            section="execution",
                            issue_class="timeout",
                            severity=Severity.HIGH,
                            citation="timed out after 30s",
                            description="Code execution timed out — possible infinite loop or blocking call",
                            suggestion="Ensure the code terminates in reasonable time",
                        ))
                    elif result.exit_code != 0:
                        execution_findings.append(Finding(
                            section="execution",
                            issue_class="runtime_failure",
                            severity=Severity.HIGH,
                            citation=f"exit_code={result.exit_code}",
                            description=(result.stderr or "Unknown execution error")[:300],
                            suggestion="Fix the runtime errors above",
                        ))
                    else:
                        execution_findings.append(Finding(
                            section="execution",
                            issue_class="execution_passed",
                            severity=Severity.LOW,
                            citation="exit_code=0",
                            description=f"Code executed successfully in {result.duration_ms}ms",
                            suggestion="",
                        ))
                else:
                    execution_findings.append(Finding(
                        section="execution",
                        issue_class="no_code_found",
                        severity=Severity.LOW,
                        citation="code extraction returned empty",
                        description="No runnable code could be extracted from the student's output",
                        suggestion="",
                    ))
        except Exception as e:
            execution_findings.append(Finding(
                section="execution",
                issue_class="sandbox_error",
                severity=Severity.LOW,
                citation="Orca execution error",
                description=str(e)[:200],
                suggestion="",
            ))

    # ── Repo verify gate (BUILD lens) ──
    build_findings: list = []
    verification_output: str = ""
    verify_strict = os.environ.get("VERIFY_GATE_STRICT") == "1"
    if preverified_verification is not None:
        vg = dict(preverified_verification)
        verification_output = json.dumps(vg, indent=2)[:2000]
        if vg.get("passed"):
            build_findings.append(Finding(
                section="build",
                issue_class="verification_passed",
                severity=Severity.LOW,
                citation=f"ran={vg.get('ran', 0)} commands (premerge)",
                description=f"Repo verify gate passed ({vg.get('ran', 0)} commands)",
                suggestion="",
            ))
        else:
            for failure in vg.get("failures", []) or [{"cmd": "(verify_gate)"}]:
                build_findings.append(Finding(
                    section="build",
                    issue_class="verify_failed",
                    severity=Severity.CRITICAL,
                    citation=f"cmd: {failure.get('cmd', '')}",
                    description=(failure.get("stderr") or "pre-merge verify gate failed")[:300],
                    suggestion="Fix the failing verify command (typecheck/test/lint)",
                ))
    elif (
        task.get("domain") in executable_domains
        and os.environ.get("REVIEW_RUN_VERIFY_GATE", "1").strip().lower()
        not in ("0", "false", "no")
    ):
        repo_path = _resolve_repo_path(repo, explicit_path=repo_path)
        if pipeline_metrics is not None:
            pipeline_metrics.record_call("verify_gate")
            pipeline_metrics.record_verification(invocations=1)
        if repo_path is None:
            vg = {
                "passed": False,
                "skipped": True,
                "failures": [{
                    "cmd": "(repo)",
                    "exit": None,
                    "stderr": "No repository checkout resolved — verify gate SKIPPED.",
                }],
                "ran": 0,
            }
            verification_output = json.dumps(vg, indent=2)[:2000]
            build_findings.append(Finding(
                section="build",
                issue_class="verification_skipped",
                severity=Severity.CRITICAL if verify_strict else Severity.LOW,
                citation="repo_unresolved",
                description=vg["failures"][0]["stderr"],
                suggestion="Resolve a cached checkout before running the build gate",
            ))
        else:
            try:
                vg = run_verify_gate(
                    repo_path=repo_path,
                    project_verify=None,
                    flake_path=REPO_ROOT,
                )
                if pipeline_metrics is not None:
                    gate_metrics = vg.get("telemetry") or {}
                    pipeline_metrics.record_verification(
                        shell_starts=gate_metrics.get("shell_starts", 0),
                        commands=gate_metrics.get("commands", 0),
                        copied_bytes=gate_metrics.get("copied_bytes", 0),
                    )
                verification_output = json.dumps(vg, indent=2)[:2000]
                if vg.get("skipped"):
                    reason = (vg.get("failures") or [{}])[0].get(
                        "stderr", "verify gate could not run"
                    )
                    build_findings.append(Finding(
                        section="build",
                        issue_class="verification_skipped",
                        severity=Severity.CRITICAL if verify_strict else Severity.LOW,
                        citation="gate_skipped",
                        description=reason[:300],
                        suggestion=("Install Nix with a verifyShell and declare "
                                    "project_verify.yaml commands"),
                    ))
                elif vg.get("strict_escalated") or not vg.get("passed"):
                    for failure in vg.get("failures", []):
                        build_findings.append(Finding(
                            section="build",
                            issue_class="verify_failed",
                            severity=Severity.CRITICAL,
                            citation=f"cmd: {failure.get('cmd', '')}",
                            description=(failure.get("stderr") or "verify command failed")[:300],
                            suggestion="Fix the failing verify command (typecheck/test/lint)",
                        ))
                else:
                    build_findings.append(Finding(
                        section="build",
                        issue_class="verification_passed",
                        severity=Severity.LOW,
                        citation=f"ran={vg.get('ran', 0)} commands",
                        description=f"Repo verify gate passed ({vg.get('ran', 0)} commands)",
                        suggestion="",
                    ))
            except Exception as e:
                vg = {
                    "passed": False,
                    "skipped": True,
                    "gate_error": True,
                    "failures": [{"cmd": "(verify_gate)", "exit": None,
                                  "stderr": str(e)[:500]}],
                    "ran": 0,
                }
                verification_output = json.dumps(vg, indent=2)[:2000]
                build_findings.append(Finding(
                    section="build",
                    issue_class="verify_gate_error",
                    severity=Severity.CRITICAL if verify_strict else Severity.LOW,
                    citation="exception",
                    description=str(e)[:200],
                    suggestion="Inspect the verify gate infrastructure before retrying",
                ))

    def _run_judge(lens_types):
        judge = AdversarialReviewer(call_model_fn=_call_model)
        return judge.review(
            output=output,
            task=task,
            codebase_context=codebase_context,
            lens_types=lens_types,
        )

    def _run_both_judges():
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="school-review") as pool:
            cto_future = pool.submit(_run_judge, [LensType.CORRECTNESS, LensType.SECURITY])
            coo_future = pool.submit(_run_judge, [LensType.COMPLETENESS])
            return cto_future.result(), coo_future.result()

    if pipeline_metrics is not None:
        with pipeline_metrics.stage("review"):
            cto_result, coo_result = _run_both_judges()
    else:
        cto_result, coo_result = _run_both_judges()

    cto_verdict = cto_result.verdict.value
    coo_verdict = coo_result.verdict.value
    all_findings = execution_findings + build_findings + cto_result.findings + coo_result.findings
    has_critical = any(
        getattr(f, "severity", None) == Severity.CRITICAL for f in all_findings
    )
    parse_failed = bool(
        getattr(cto_result, "parse_failed", False)
        or getattr(coo_result, "parse_failed", False)
    )
    accepted = (
        cto_verdict == "PASS"
        and coo_verdict == "PASS"
        and cto_result.score >= 50
        and coo_result.score >= 50
        and not has_critical
        and not parse_failed
    )
    combined_score = (cto_result.score + coo_result.score) / 2.0

    # ── Verification-co-evolution pass (P2.2) ──
    coevolution_report = None
    try:
        _coevo_checks = _acceptance_checks_from_spec(task, repo)
        _coevo = VerificationCoevolution(call_model_fn=_call_model)
        _merged = ReviewResult(
            verdict=cto_result.verdict if cto_result.verdict == coo_result.verdict
            else Verdict.FAIL,
            findings=cto_result.findings + coo_result.findings,
            lens_used=cto_result.lens_used,
            confidence=max(cto_result.confidence, coo_result.confidence),
            difficulty=task.get("difficulty", "medium"),
        )
        _trace: dict = dict(getattr(cto_result, "_lens_trace", {}))
        _trace.update(getattr(coo_result, "_lens_trace", {}))
        _merged._lens_trace = _trace  # type: ignore[attr-defined]
        coevolution_report = _coevo.analyze(_merged, task, _coevo_checks)
    except Exception as e:
        sys.stderr.write(f"[director] coevolution pass skipped: {e}\n")

    review_packet = ReviewPacket.create(
        artifact={"bead": bead, "repo": repo},
        execution={"findings": execution_findings},
        verification=verification_output,
        cto={
            "verdict": cto_verdict,
            "score": cto_result.score,
            "confidence": cto_result.confidence,
            "findings": [f.to_dict() for f in cto_result.findings],
        },
        coo={
            "verdict": coo_verdict,
            "score": coo_result.score,
            "confidence": coo_result.confidence,
            "findings": [f.to_dict() for f in coo_result.findings],
        },
        accepted=accepted,
    )

    update_bookbag(
        bead,
        cto_verdict=cto_verdict,
        coo_verdict=coo_verdict,
        findings=[f.to_dict() for f in all_findings],
        accepted=accepted,
        lens=f"cto({cto_verdict})+coo({coo_verdict})",
        verification=verification_output or None,
        repo=repo,
    )

    sys.stderr.write(
        f"[director] Two-judge review: CTO={cto_verdict} (score={cto_result.score:.0f}), "
        f"COO={coo_verdict} (score={coo_result.score:.0f}) → "
        f"{'ACCEPTED' if accepted else 'REJECTED'}\n"
    )

    if synthesize_narratives:
        cto_narrative, coo_narrative = _synthesize_judge_narratives(
            _call_model=_call_model,
            task=task,
            output=output,
            cto_verdict=cto_verdict, cto_score=cto_result.score, cto_lens=cto_result.lens_used,
            coo_verdict=coo_verdict, coo_score=coo_result.score, coo_lens=coo_result.lens_used,
            cto_findings=[f.to_dict() for f in cto_result.findings],
            coo_findings=[f.to_dict() for f in coo_result.findings],
        )
    else:
        cto_narrative, coo_narrative = None, None
    if cto_narrative:
        update_bookbag(bead, cto_narrative=cto_narrative, repo=repo)
    if coo_narrative:
        update_bookbag(bead, coo_narrative=coo_narrative, repo=repo)

    return {
        "cto_verdict": cto_verdict,
        "coo_verdict": coo_verdict,
        "cto_score": cto_result.score,
        "coo_score": coo_result.score,
        "cto_confidence": cto_result.confidence,
        "coo_confidence": coo_result.confidence,
        "confidence": max(cto_result.confidence, coo_result.confidence),
        "combined_score": combined_score,
        "findings": [f.to_dict() for f in all_findings],
        "build_findings": [f.to_dict() for f in build_findings],
        "build_verification": verification_output or None,
        "accepted": accepted,
        "coevolution": coevolution_report.to_dict() if coevolution_report else None,
        "cto_narrative": cto_narrative,
        "coo_narrative": coo_narrative,
        "review_packet": review_packet.to_dict(),
    }


def _resolve_repo_path(
    repo: str,
    *,
    explicit_path: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the checkout for a task without silently crossing repositories."""
    from repo_reader import CACHE_DIR as _REPO_CACHE_DIR

    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.exists() and (candidate / ".git").exists():
            return candidate
        return None

    if not repo or repo == REPO_GLOBAL:
        return None

    cached = _REPO_CACHE_DIR / repo.replace("/", "__")
    if cached.exists() and (cached / ".git").exists():
        return cached

    try:
        from repo_default import default_repo
        if repo == default_repo():
            return REPO_ROOT
    except Exception:
        pass

    return None
