#!/usr/bin/env python3
"""
issue_bridge.py — Bridges fetched GitHub issues into the Director's task pipeline.

Poll flow:
  1. Fetch open issues from a configured repo via github_fetcher
  2. Classify each issue → (category, state)
  3. Skip non-actionable issues (state != "ready-for-agent")
  4. Convert remaining issues into Director tasks via run_task()
  5. Verify output correctness (replaces hardcoded score)
  5. Track completed issue numbers to avoid re-processing
  6. Retry-once: transient failures get one retry next cycle before school-failed

Usage:
    from issue_bridge import bridge_issues, mark_processed, is_processed
    results = bridge_issues(repo="owner/repo")
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from github_fetcher import fetch_issues, load_config, _gh_command
from executor import call_model, COMBO_MAP, ExecutorError
from scoring import ScoreStore
from school_mail import notify_issue_alert

PROCESSED_FILE = Path(__file__).parent / "data" / "processed_issues.json"

# GitHub lifecycle labels so the repo's issue list reflects the school's work.
# Success → closed + school-done (queryable "the school finished this").
# Failure → school-failed, left open for human retriage.
SCHOOL_DONE_LABEL = "school-done"
SCHOOL_FAILED_LABEL = "school-failed"
_SCHOOL_LABELS = (
    (SCHOOL_DONE_LABEL, "0E8A16", "Processed successfully by Agent School"),
    (SCHOOL_FAILED_LABEL, "D93F0B", "Agent School attempted this issue but it failed"),
)

# Verification prompt template — uses semantic anchors for structured evaluation
VERIFICATION_PROMPT_TEMPLATE = """You are a verification evaluator. Your job: determine if the AGENT RESPONSE correctly and completely addresses the ORIGINAL TASK.

[VERIFICATION ROLE] You are a rigorous evaluator. Apply [Five Whys] to uncover gaps. Use [Chain of Thought] — reason step by step. Follow [Fagan Inspection] principles — systematic, checklist-driven.

[EVALUATION CRITERIA]
1. **Completeness**: Does the response address ALL parts of the original task? (Explicit + implied requirements)
2. **Correctness**: Is the solution technically correct? Would it work in practice?
3. **Quality**: Does it follow best practices for the domain? (Testing patterns, git conventions, code style, etc.)
4. **Edge Cases**: Are error paths, boundary conditions, and failure modes handled?

[SCORING RUBRIC]
- 90-100: EXCELLENT — Complete, correct, high quality, handles edge cases
- 75-89: GOOD — Mostly complete and correct, minor gaps
- 60-74: ACCEPTABLE — Core task done, but notable gaps or quality issues
- 40-59: PARTIAL — Significant gaps, incomplete, or partially incorrect
- 20-39: POOR — Major errors, misses core requirements
- 0-19: FAIL — Fundamentally wrong or empty

[ORIGINAL TASK]
{original_prompt}

[AGENT RESPONSE]
{agent_response}

[DOMAIN CONTEXT]
Domain: {domain}
Difficulty: {difficulty}

[CODEBASE CONTEXT]
{codebase_context}

Think through this step by step. Then output ONLY a JSON object:
{{
  "score": <integer 0-100>,
  "verdict": "EXCELLENT|GOOD|ACCEPTABLE|PARTIAL|POOR|FAIL",
  "reasoning": "Step-by-step justification for the score",
  "gaps": ["list of specific gaps or issues found"],
  "strengths": ["list of what was done well"]
}}"""


def _load_processed() -> set[int]:
    """Load previously processed issue numbers from disk."""
    if not PROCESSED_FILE.exists():
        return set()
    try:
        raw = PROCESSED_FILE.read_text().strip()
        if not raw:
            return set()
        return set(json.loads(raw))
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"[issue_bridge] Failed to load processed issues: {e}\n")
        return set()


def _save_processed(processed: set[int]) -> None:
    """Persist processed issue numbers to disk."""
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        PROCESSED_FILE.write_text(json.dumps(sorted(processed), indent=2))
    except OSError as e:
        sys.stderr.write(f"[issue_bridge] Failed to save processed issues: {e}\n")


def mark_processed(issue_number: int) -> None:
    """Mark a single issue as processed."""
    processed = _load_processed()
    processed.add(issue_number)
    _save_processed(processed)


def is_processed(issue_number: int) -> bool:
    """Check if an issue has already been processed."""
    return issue_number in _load_processed()


RETRY_FILE = Path(__file__).parent / "data" / "retry_issues.json"
# Retry-once: a failed issue gets one retry on the next cycle before it is
# marked processed + school-failed. attempt 1 → schedule retry; attempt 2 → final.
RETRY_LIMIT = 2


def _load_retries() -> dict[int, int]:
    """Load ``{issue_number: attempt_count}`` for issues awaiting a retry.

    Durable across school-loop cycles (each cycle does a fresh checkout), so
    the counter lives in a committed data file alongside the other state.
    """
    if not RETRY_FILE.exists():
        return {}
    try:
        raw = RETRY_FILE.read_text().strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return {int(k): int(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        sys.stderr.write(f"[issue_bridge] Failed to load retry issues: {e}\n")
        return {}


def _save_retries(retries: dict[int, int]) -> None:
    """Persist retry attempt counts to disk."""
    RETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        RETRY_FILE.write_text(json.dumps({str(k): v for k, v in retries.items()}, indent=2))
    except OSError as e:
        sys.stderr.write(f"[issue_bridge] Failed to save retry issues: {e}\n")


_LABELS_ENSURED = False


def _ensure_school_labels(repo: str) -> None:
    """Create the Agent School lifecycle labels if they don't exist yet.

    Memoized per process (labels are repo-wide; checking once per bridge run
    avoids a ``label list`` round trip for every issue). Non-fatal: label-API
    failures must never crash the bridge. ``gh`` resolves the repo from the
    current checkout when ``repo`` is empty.
    """
    global _LABELS_ENSURED
    if _LABELS_ENSURED:
        return
    try:
        existing: set[str] = set()
        out = _gh_command(["label", "list", "--repo", repo, "--json", "name"])
        if out:
            try:
                existing = {lbl.get("name") for lbl in json.loads(out)}
            except json.JSONDecodeError:
                existing = set()
        for name, color, desc in _SCHOOL_LABELS:
            if name not in existing:
                _gh_command([
                    "label", "create", name, "--repo", repo,
                    "--color", color, "--description", desc,
                ])
        _LABELS_ENSURED = True
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] Failed to ensure school labels: {e}\n")


def _mark_github_issue(repo: str, issue_number: int, status: str,
                       score: Optional[float] = None) -> None:
    """Reflect a processed issue on GitHub so the repo list shows the school's work.

    - ``status == "success"`` → add the ``school-done`` label and close the
      issue, with the combined score in the close comment.
    - ``status == "error"`` → add the ``school-failed`` label and leave the
      issue open for human retriage.

    Never raises — gh write failures are logged and ignored so a GitHub API
    hiccup can't take down the pipeline.
    """
    if status not in ("success", "error"):
        return
    try:
        _ensure_school_labels(repo)
        label = SCHOOL_DONE_LABEL if status == "success" else SCHOOL_FAILED_LABEL
        _gh_command(["issue", "edit", str(issue_number), "--repo", repo, "--add-label", label])
        if status == "success":
            comment = "✅ Processed by Agent School — status: success"
            if score is not None:
                comment += f" — score: {score:.1f}"
            _gh_command(["issue", "close", str(issue_number), "--repo", repo, "--comment", comment])
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] Failed to update GitHub issue #{issue_number}: {e}\n")


def record_run(path: Path, entry: dict) -> None:
    """Append an entry to a JSON-list run log at *path*, atomically.

    Each entry gets a server-side ``timestamp`` (ISO-8601 UTC) added if
    the entry does not already have one.  Writes to a temporary file then
    renames for crash-safe atomicity.  Creates parent directories as needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if "timestamp" not in entry:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Read existing runs (empty list if file missing or corrupt)
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(entry)

    # Atomic write: temp → rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    os.replace(tmp, path)


def verify_task_output(
    original_prompt: str,
    agent_response: str,
    domain: str,
    difficulty: str,
    codebase_context: str = "",
    verification_agent: str = "agy/gemini-3.5-flash-high",
) -> dict:
    """Verify agent output correctness using a structured evaluation prompt.

    Returns dict with: score (0-100), verdict, reasoning, gaps, strengths.
    Falls back to conservative score if verification fails.
    """
    prompt = VERIFICATION_PROMPT_TEMPLATE.format(
        original_prompt=original_prompt,
        agent_response=agent_response,
        domain=domain,
        difficulty=difficulty,
        codebase_context=codebase_context,
    )

    try:
        response = call_model(verification_agent, prompt, timeout=120)
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] Verification failed: {e}\n")
        return {
            "score": 50,
            "verdict": "PARTIAL",
            "reasoning": f"Verification error: {e}",
            "gaps": ["Verification could not complete"],
            "strengths": [],
        }

    # Parse JSON response from verifier
    try:
        import re

        # --- Step 1: normalise ---
        raw = response.strip()
        # Strip leading "json"/"JSON" prefix some models emit
        raw = re.sub(r'^(?:json|JSON)\s*', '', raw)
        # Strip control characters (preserve newlines and tabs)
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', raw)

        # --- Step 2: collect candidates (fence-extracted + raw) ---
        candidates = []
        if '```' in raw:
            for match in re.finditer(r'```(?:json)?\s*\n?([\s\S]+?)```', raw):
                candidates.append(match.group(1).strip())
        candidates.append(raw)
        candidates = [re.sub(r'^(?:json|JSON)\s*', '', c) for c in candidates]

        # --- Step 3: balanced extraction from first parseable candidate ---
        from adversarial_reviewer import extract_balanced_json

        result = None
        for candidate_str in candidates:
            idx = candidate_str.find('{')
            if idx < 0:
                continue
            # Extract balanced {...} block
            candidate = extract_balanced_json(candidate_str[idx:], "{", "}")
            try:
                result = json.loads(candidate, strict=False)
                break
            except json.JSONDecodeError:
                continue

        if result is None:
            raise json.JSONDecodeError("No parseable JSON found", "", 0)

        score = max(0, min(100, int(result.get("score", 50))))
        return {
            "score": score,
            "verdict": result.get("verdict", "PARTIAL"),
            "reasoning": result.get("reasoning", ""),
            "gaps": result.get("gaps", []),
            "strengths": result.get("strengths", []),
        }
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(f"[issue_bridge] Failed to parse verification response: {e}\n")
        return {
            "score": 50,
            "verdict": "PARTIAL",
            "reasoning": f"Parse error: {e}",
            "gaps": ["Verification response unparseable"],
            "strengths": [],
        }


def _strict_gate_failure(reason: str) -> dict:
    """VERIFY_GATE_STRICT escalation: the gate could not run ⇒ issue cannot pass.

    Strict mode enforces campus.md #3 (compiler before critic) end-to-end:
    when the hermetic gate cannot run at all — module missing, or an internal
    failure — a strict pipeline treats that as a real gate failure (verdict
    FAIL) rather than letting the issue flow on unverified. Mirrors the
    `strict_escalated` shape verify_gate._skipped_verdict returns.
    """
    return {
        "passed": False,
        "skipped": False,
        "strict_escalated": True,
        "ran": 0,
        "failures": [{
            "cmd": "(verify_gate)",
            "exit": None,
            "stderr": (
                f"{reason}\n[VERIFY_GATE_STRICT] Escalation: the verify gate "
                "could not run, so this issue cannot pass "
                "(compiler-before-critic is enforced)."
            ),
        }],
    }


def _run_verify_gate(
    repo_path: Optional[Path],
    issue: dict,
) -> Optional[dict]:
    """Run the hermetic verify gate (compile/typecheck/test) on the cloned repo.

    Returns the verify_gate result dict, or None on an import/internal failure
    in the default direct/manual path (the gate is reported as a finding, not a
    crash). The scheduled school-loop performs a hard Nix + verifyShell
    preflight before this function is reached. The reusable gate itself emits a
    visible soft-skip when its toolchain or commands are unavailable. When
    VERIFY_GATE_STRICT=1, an unrunnable gate escalates to a FAIL verdict instead
    of returning None (the issue cannot pass unverified).
    """
    if not repo_path or not repo_path.exists():
        return None
    try:
        from verify_gate import run_verify_gate
        project_verify = Path(repo_path) / "project_verify.yaml"
        # Pin the flake to the school-core checkout (this module's directory),
        # NEVER Path.cwd() — the runner invokes the bridge from the checkout
        # root today, but a workflow `working-directory:` would silently point
        # the gate at a flake-less dir and turn every issue into a fake
        # CRITICAL failure (missing-flake errors aren't exit-127, so the infra
        # filter can't catch them). The hermetic shell's flake lives with the
        # bridge, deterministically.
        return run_verify_gate(
            repo_path,
            project_verify if project_verify.exists() else None,
            # resolve() guards against a relative __file__ (e.g. invoked as
            # `python issue_bridge.py`), which would otherwise re-introduce the
            # cwd dependence we are eliminating.
            flake_path=Path(__file__).resolve().parent,
        )
    except ImportError:
        # verify_gate module not available — not a blocker (unless strict).
        if os.environ.get("VERIFY_GATE_STRICT") == "1":
            return _strict_gate_failure("verify_gate module not importable")
        return None
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] verify_gate failed (non-blocking): {e}\n")
        if os.environ.get("VERIFY_GATE_STRICT") == "1":
            return _strict_gate_failure(f"verify_gate raised: {e}")
        return None  # Can't run → don't override adversarial review


def _run_entire_sensor(repo_path: Optional[Path]) -> Optional[dict]:
    """Run `entire review` on the student's clone as a non-blocking sensor.

    Surfaces intent-aware findings (on the result + durable record) but never
    overrides the verdict — the adversarial LLM review remains the semantic
    gate. Returns None when the CLI is missing or the clone is unavailable,
    so the pipeline never blocks on the sensor.
    """
    if not repo_path or not repo_path.exists():
        return None
    try:
        from src.entire_review import run_entire_review
        return run_entire_review(str(repo_path), base_branch="main")
    except ImportError:
        return None
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] entire sensor failed (non-blocking): {e}\n")
        return None


def _run_adversarial_review(
    task_result: dict,
    issue: dict,
    codebase_ctx: str,
) -> dict:
    """Run adversarial review on a successful task result.

    Returns the review as a dict, or a fallback on failure.
    Lazy-imports adversarial_reviewer and executor to avoid circular deps.
    """
    try:
        from adversarial_reviewer import AdversarialReviewer, LensType
        from executor import call_model

        # Use cloud model for adversarial review (fast, reliable); avoid local foundry models
        # which can hang with 300s timeouts on M1
        _call_model = lambda prompt, system_prompt=None, **kw: call_model("auto/best-free", prompt, system_prompt=system_prompt, timeout=120, **kw)
        reviewer = AdversarialReviewer(call_model_fn=_call_model)
        review_result = reviewer.review(
            output=task_result["response"],
            task={
                "title": issue["title"],
                "body": issue.get("body", ""),
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "prompt": issue["prompt"],
            },
            codebase_context=codebase_ctx,
            lens_types=list(LensType),
        )
        return review_result.to_dict()
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] Adversarial review failed, falling back: {e}\n")
        return {
            "verdict": "PASS",
            "score": 50.0,
            "findings": [],
            "lens_used": "fallback",
            "confidence": 0.0,
            "gaps": [],
            "suggestions": [],
            "error": str(e),
        }


def bridge_issues(
    repo: str,
    labels: Optional[List[str]] = None,
    force_agent: Optional[str] = None,
    dry_run: bool = False,
    store: Optional[ScoreStore] = None,
) -> list[dict]:
    """Fetch actionable issues and dispatch each as a Director task.

    Returns list of result dicts, one per issue processed.

    `store` is injectable for test isolation; when omitted a live ScoreStore
    (data/scores.json) is used in production. Tests MUST pass a temp store to
    avoid polluting the real scores file.
    """
    from director import run_task, evaluate_and_update

    if store is None:
        store = ScoreStore()
    if not repo:
        # school-loop passes --repo "$SCHOOL_REPO" (usually empty) → resolve the
        # repo from the current checkout's origin remote, same as bridge_poll.
        from repo_default import default_repo
        repo = default_repo()
    issues = fetch_issues(repo, labels)
    processed = _load_processed()
    retries = _load_retries()
    results = []

    # U1: one session_id per cycle (not per issue) so Layer 3 archival
    # context can accumulate across the school-loop's sleep/wake cycles and
    # the orchestrator's `if session_id:` gate actually fires. Seconds-level
    # granularity keeps a manual dispatch + the scheduled cron in the same
    # minute from sharing a key (which would clobber the same consolidation
    # dir if the write side ever runs under a loop-* id).
    cycle_session_id = f"loop-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    if not issues:
        sys.stderr.write("[issue_bridge] No actionable issues found.\n")
        return results

    # Clone repo and build codebase context for enrichment
    from repo_reader import clone_repo, build_codebase_context, cleanup_stale_caches
    cleanup_stale_caches()
    repo_path = clone_repo(repo) if not dry_run else None

    for issue in issues:
        num = issue["issue_number"]
        if num in processed:
            continue

        # Build codebase context for this issue
        issue_text = f"{issue['title']}\n\n{issue.get('body', '')}"
        codebase_ctx = ""
        if repo_path:
            codebase_ctx = build_codebase_context(repo_path, issue_text)

        # Enrich prompt with codebase context
        enriched_prompt = issue["prompt"]
        if codebase_ctx:
            enriched_prompt = f"{codebase_ctx}\n\n## Issue\n{issue['prompt']}"

        if dry_run:
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "dry_run",
                "codebase_context_chars": len(codebase_ctx),
            })
            processed.add(num)
            try:
                record_run(
                    PROCESSED_FILE.parent / "last_run.json",
                    {"issue": num, "status": "dry_run", "agent": force_agent, "score": None, "trajectory": None},
                )
            except Exception as e:
                sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e}\n")
            continue

        try:
            task_result = run_task(
                prompt=enriched_prompt,
                domain=issue["domain"],
                difficulty=issue["difficulty"],
                force_agent=force_agent,
                store=store,
                session_id=cycle_session_id,
            )
        except Exception as e:
            sys.stderr.write(f"[issue_bridge] Task failed for #{num}: {e}\n")
            err = str(e)
            attempts = retries.get(num, 0) + 1
            if attempts < RETRY_LIMIT:
                # Transient failure (gateway hiccup, Orca unavailable, …):
                # schedule a retry on the next cycle. Not processed, not labeled.
                retries[num] = attempts
                results.append({
                    "issue_number": num,
                    "title": issue["title"],
                    "domain": issue["domain"],
                    "difficulty": issue["difficulty"],
                    "status": "retry",
                    "retry_attempt": attempts,
                    "error": err,
                })
                try:
                    record_run(
                        PROCESSED_FILE.parent / "last_run.json",
                        {"issue": num, "status": "retry", "agent": None, "score": None, "trajectory": None},
                    )
                except Exception as e_rec:
                    sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
                try:
                    notify_issue_alert(num, issue["title"], "retry", error=err,
                                       repo=repo, attempt=attempts,
                                       retry_limit=RETRY_LIMIT)
                except Exception as e_notify:
                    sys.stderr.write(f"[issue_bridge] Alert failed for #{num}: {e_notify}\n")
                continue
            retries.pop(num, None)
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "error",
                "error": err,
            })
            try:
                record_run(
                    PROCESSED_FILE.parent / "last_run.json",
                    {"issue": num, "status": "error", "agent": None, "score": None, "trajectory": None},
                )
            except Exception as e_rec:
                sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
            _mark_github_issue(repo, num, "error")
            try:
                notify_issue_alert(num, issue["title"], "school-failed", error=err,
                                   repo=repo, attempt=attempts,
                                   retry_limit=RETRY_LIMIT)
            except Exception as e_notify:
                sys.stderr.write(f"[issue_bridge] Alert failed for #{num}: {e_notify}\n")
            processed.add(num)
            continue

        if task_result["status"] == "success":
            # NEW: run the code before the critic speaks (campus.md #3).
            # Compile/typecheck/test failures become CRITICAL findings fed
            # into the adversarial reviewer, so broken code can't pass review.
            verify_result = _run_verify_gate(repo_path, issue)

            # Loudness for direct/manual callers: when the reusable gate could
            # not run at all (Nix missing / no verify commands), say so in the
            # loop log AND in the durable record. The scheduled school-loop
            # rejects missing Nix earlier in its workflow preflight.
            verify_skipped = bool(verify_result and verify_result.get("skipped"))
            if verify_skipped:
                _reason = ((verify_result.get("failures") or [{}])[0].get("stderr") or "n/a")[:120]
                sys.stderr.write(f"[issue_bridge] verify gate SKIPPED for #{num}: {_reason}\n")

            # Entire pre-merge sensor (non-blocking, U6): intent-aware review
            # of the student's diff via `entire review`. Findings are surfaced
            # on the result + durable record, but never override the verdict —
            # the adversarial (two-judge) review below remains the semantic
            # gate.
            entire_review = _run_entire_sensor(repo_path)
            entire_summary = None
            if entire_review:
                entire_summary = {
                    "status": entire_review.get("status"),
                    "findings": len(entire_review.get("findings") or []),
                }
                if entire_summary["status"] == "fail":
                    sys.stderr.write(
                        f"[issue_bridge] entire review FAIL for #{num}: "
                        f"{entire_summary['findings']} finding(s)\n"
                    )

            # Run adversarial review before verification
            adversarial_review = _run_adversarial_review(
                task_result=task_result,
                issue=issue,
                codebase_ctx=codebase_ctx,
            )

            # Merge verify-gate failures into the review as CRITICAL findings.
            # This is the enforcement of campus.md #3: the compiler runs before
            # the critic speaks; a broken build cannot earn a PASS.
            # Only override for real test failures (commands that ran), not
            # soft-SKIPPED verdicts (no commands found, or Nix missing — the
            # reusable gate reports those with ran == 0) and not infrastructure
            # failures (e.g. a command not found inside the shell → exit 127).
            # The scheduled school-loop handles its missing infrastructure at
            # the workflow preflight boundary; VERIFY_GATE_STRICT escalates
            # unrunnable internal/direct invocations explicitly.
            def _is_infrastructure_failure(f: dict) -> bool:
                """Check if a verify failure is from missing infrastructure (Nix, etc.)."""
                exit_code = f.get("exit")
                stderr = (f.get("stderr") or "").lower()
                return (
                    exit_code == 127
                    or "command not found" in stderr
                )

            if verify_result and not verify_result.get("passed") and (
                verify_result.get("ran", 0) > 0
                or verify_result.get("strict_escalated")  # VERIFY_GATE_STRICT
            ):
                # Strict-escalated verdicts (gate could not run at all) are real
                # failures by definition — the infra filter must not swallow
                # them back into a soft pass.
                verify_escalated = bool(verify_result.get("strict_escalated"))
                real_failures = [
                    f for f in verify_result.get("failures", [])
                    if (not _is_infrastructure_failure(f)) or verify_escalated
                ]
                if real_failures:
                    from adversarial_reviewer import Finding, Severity
                    for f in real_failures:
                        adversarial_review.setdefault("findings", []).append({
                            "section": "build/verify",
                            "issue_class": "compile_error",
                            "severity": "CRITICAL",
                            "citation": f['cmd'],
                            "description": (f.get("stderr") or "verify command failed")[:500],
                            "suggestion": "Fix the build/typecheck/test failure before review.",
                        })
                    adversarial_review["verdict"] = "FAIL"
                    adversarial_review["score"] = 0.0

            # Verify output correctness with codebase context
            verification = verify_task_output(
                original_prompt=enriched_prompt,
                agent_response=task_result["response"],
                domain=issue["domain"],
                difficulty=issue["difficulty"],
                codebase_context=codebase_ctx,
            )

            # Combined score: execution * 0.5 + review * 0.3 + heuristic * 0.2
            execution_score = verification["score"]
            review_score = adversarial_review.get("score", execution_score)
            heuristic_score = _heuristic_score(task_result, issue)
            combined_score = (
                execution_score * 0.5
                + review_score * 0.3
                + heuristic_score * 0.2
            )

            sys.stderr.write(
                f"[issue_bridge] Verification: {verification['verdict']} "
                f"(exec={execution_score}, review={review_score}, "
                f"heuristic={heuristic_score:.1f}, combined={combined_score:.1f})\n"
            )

            task_result["adversarial_review"] = adversarial_review
            updated = evaluate_and_update(task_result, combined_score, store=store)
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "success",
                "agent": task_result.get("agent"),
                "old_score": updated.get("old_score"),
                "new_score": updated.get("new_score"),
                "gate_crossed": updated.get("gate_crossed"),
                "verification": verification,
                "adversarial_review": adversarial_review,
                "verify_skipped": verify_skipped,
                "entire_review": entire_review,
            })
            try:
                record_run(
                    PROCESSED_FILE.parent / "last_run.json",
                    {
                        "issue": num,
                        "status": "success",
                        "agent": task_result.get("agent"),
                        "score": combined_score,
                        "trajectory": task_result.get("trajectory"),
                        # title/domain/difficulty let the board re-render the
                        # card after the issue is auto-closed (it leaves the
                        # open-issues cache once closed).
                        "title": issue["title"],
                        "domain": issue["domain"],
                        "difficulty": issue["difficulty"],
                        # verify_skipped lets the board/reports distinguish
                        # "compiler ran and passed" from "compiler never ran".
                        "verify_skipped": verify_skipped,
                        # entire: compact pre-merge sensor summary (status +
                        # finding count) so the board can surface it; None when
                        # the CLI/clone was unavailable.
                        "entire": entire_summary,
                    },
                )
            except Exception as e_rec:
                sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
            _mark_github_issue(repo, num, "success", score=combined_score)
            retries.pop(num, None)
            processed.add(num)
        else:
            err = task_result.get("error")
            attempts = retries.get(num, 0) + 1
            if attempts < RETRY_LIMIT:
                # Transient failure — schedule a retry on the next cycle.
                retries[num] = attempts
                results.append({
                    "issue_number": num,
                    "title": issue["title"],
                    "domain": issue["domain"],
                    "difficulty": issue["difficulty"],
                    "status": "retry",
                    "retry_attempt": attempts,
                    "error": err,
                })
                try:
                    record_run(
                        PROCESSED_FILE.parent / "last_run.json",
                        {
                            "issue": num,
                            "status": "retry",
                            "agent": task_result.get("agent"),
                            "score": None,
                            "trajectory": None,
                        },
                    )
                except Exception as e_rec:
                    sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
                try:
                    notify_issue_alert(num, issue["title"], "retry", error=err,
                                       repo=repo, attempt=attempts,
                                       retry_limit=RETRY_LIMIT)
                except Exception as e_notify:
                    sys.stderr.write(f"[issue_bridge] Alert failed for #{num}: {e_notify}\n")
            else:
                # Retry budget exhausted — final failure: school-failed + processed.
                retries.pop(num, None)
                results.append({
                    "issue_number": num,
                    "title": issue["title"],
                    "domain": issue["domain"],
                    "difficulty": issue["difficulty"],
                    "status": task_result.get("status", "error"),
                    "error": err,
                })
                try:
                    record_run(
                        PROCESSED_FILE.parent / "last_run.json",
                        {
                            "issue": num,
                            "status": task_result.get("status", "error"),
                            "agent": task_result.get("agent"),
                            "score": None,
                            "trajectory": None,
                        },
                    )
                except Exception as e_rec:
                    sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
                _mark_github_issue(repo, num, "error")
                try:
                    notify_issue_alert(num, issue["title"], "school-failed", error=err,
                                       repo=repo, attempt=attempts,
                                       retry_limit=RETRY_LIMIT)
                except Exception as e_notify:
                    sys.stderr.write(f"[issue_bridge] Alert failed for #{num}: {e_notify}\n")
                processed.add(num)

    _save_retries(retries)
    _save_processed(processed)
    return results


def bridge_poll(repo: Optional[str] = None, interval: int = 300, labels: Optional[List[str]] = None, force_agent: Optional[str] = None) -> None:
    """Polling loop: fetch and bridge issues every `interval` seconds.

    Reads repo from config/github.yaml if not provided. Falls back to
    :func:`repo_default.default_repo` (self-configuring from the current
    checkout's origin remote, overridable via AGENT_SCHOOL_REPO) when
    neither arg nor config yields a value.
    """
    cfg = load_config()
    repo = repo or cfg.get("repo", "")
    if not repo:
        from repo_default import default_repo
        repo = default_repo()
    if not repo:
        sys.stderr.write("[issue_bridge] No repo configured. Set repo in config/github.yaml, use __self__ in target_repos, or pass --repo\n")
        return

    labels = labels or cfg.get("labels")
    print(f"[issue_bridge] Polling {repo} every {interval}s (labels={labels})")
    print(f"[issue_bridge] Press Ctrl+C to stop")

    try:
        while True:
            results = bridge_issues(repo, labels, force_agent=force_agent)
            if results:
                ok = sum(1 for r in results if r["status"] == "success")
                retried = sum(1 for r in results if r["status"] == "retry")
                fail = sum(1 for r in results if r["status"] not in ("success", "retry", "dry_run"))
                print(f"[issue_bridge] Round complete: {len(results)} issues "
                      f"({ok} ok, {retried} retried, {fail} failed)")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[issue_bridge] Stopped.")


def _heuristic_score(task_result: dict, issue: dict) -> float:
    """Compute a heuristic score from response metadata.

    Factors: response length relative to difficulty, domain signals.
    """
    response = task_result.get("response", "")
    difficulty = issue.get("difficulty", "medium")

    length_score = min(100.0, len(response) / 10.0)

    difficulty_weights = {"easy": 0.6, "medium": 0.8, "hard": 1.0, "diploma": 1.0}
    weight = difficulty_weights.get(difficulty, 0.8)

    return length_score * weight


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Issue→Task Bridge")
    parser.add_argument("--repo", help="Repository to poll (owner/repo)")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds")
    parser.add_argument("--labels", help="Comma-separated label filter")
    parser.add_argument("--force-agent", help="Skip readiness checks; use a specific agent directly")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be bridged without executing")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle then exit")
    args = parser.parse_args()

    labels = args.labels.split(",") if args.labels else None
    if args.once:
        results = bridge_issues(args.repo, labels, force_agent=args.force_agent, dry_run=args.dry_run)
        if not results:
            print("No new issues to bridge.")
        else:
            for r in results:
                print(f"  #{r['issue_number']} [{r['domain']}/{r['difficulty']}] {r['title'][:60]} → {r['status']}")
    else:
        bridge_poll(args.repo, args.interval, labels, force_agent=args.force_agent)
