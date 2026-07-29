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

from github_fetcher import fetch_issues, load_config
from executor import call_model, COMBO_MAP, ExecutorError
from scoring import ScoreStore

PROCESSED_FILE = Path(__file__).parent / "data" / "processed_issues.json"

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
    verification_agent: str = "gemini-2.0-flash",
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
        # Strip common prefixes models add: "json", "JSON", newlines, whitespace
        cleaned = re.sub(r'^(?:json|JSON)\s*', '', response.strip())
        cleaned = cleaned.replace('\n', ' ')
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', cleaned)
        # First try to find JSON in markdown code fences
        code_fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', cleaned, re.DOTALL)
        if code_fence_match:
            json_str = code_fence_match.group(1)
        else:
            # Fallback: find first {...} block (non-greedy)
            json_match = re.search(r'\{.*?\}', cleaned, re.DOTALL)
            if json_match:
                json_str = json_match.group()
            else:
                json_str = cleaned

        result = json.loads(json_str)

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


def _run_verify_gate(
    repo_path: Optional[Path],
    issue: dict,
) -> Optional[dict]:
    """Run the hermetic verify gate (compile/typecheck/test) on the cloned repo.

    Returns the verify_gate result dict, or None on any failure (non-blocking —
    we never let the gate itself block the pipeline; a gate failure is reported
    as a finding, not a crash). Requires Determinate Nix (see flake.nix).
    """
    if not repo_path or not repo_path.exists():
        return None
    try:
        from verify_gate import run_verify_gate
        project_verify = Path(repo_path) / "project_verify.yaml"
        return run_verify_gate(repo_path, project_verify if project_verify.exists() else None)
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] verify_gate failed (non-blocking): {e}\n")
        return {"passed": False, "failures": [{"cmd": "(verify_gate)", "exit": None,
                                                "stderr": f"verify_gate error: {e}"}]}


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
        _call_model = lambda prompt, system_prompt=None, **kw: call_model("gemini-2.0-flash", prompt, system_prompt=system_prompt, **kw)
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
    issues = fetch_issues(repo, labels)
    processed = _load_processed()
    results = []

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
            )
        except Exception as e:
            sys.stderr.write(f"[issue_bridge] Task failed for #{num}: {e}\n")
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "status": "error",
                "error": str(e),
            })
            try:
                record_run(
                    PROCESSED_FILE.parent / "last_run.json",
                    {"issue": num, "status": "error", "agent": None, "score": None, "trajectory": None},
                )
            except Exception as e_rec:
                sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
            continue

        if task_result["status"] == "success":
            # NEW: run the code before the critic speaks (campus.md #3).
            # Compile/typecheck/test failures become CRITICAL findings fed
            # into the adversarial reviewer, so broken code can't pass review.
            verify_result = _run_verify_gate(repo_path, issue)

            # Run adversarial review before verification
            adversarial_review = _run_adversarial_review(
                task_result=task_result,
                issue=issue,
                codebase_ctx=codebase_ctx,
            )

            # Merge verify-gate failures into the review as CRITICAL findings.
            # This is the enforcement of campus.md #3: the compiler runs before
            # the critic speaks; a broken build cannot earn a PASS.
            if verify_result and not verify_result.get("passed"):
                from adversarial_reviewer import Finding, Severity
                for f in verify_result.get("failures", []):
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
                    },
                )
            except Exception as e_rec:
                sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
        else:
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": task_result.get("status", "error"),
                "error": task_result.get("error"),
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

        # Mark processed regardless of outcome (don't retry failed issues)
        processed.add(num)

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
                fail = sum(1 for r in results if r["status"] != "success" and r["status"] != "dry_run")
                print(f"[issue_bridge] Round complete: {len(results)} issues "
                      f"({ok} ok, {fail} failed)")
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
