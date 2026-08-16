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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from github_fetcher import fetch_issues, load_config, _gh_command
from executor import call_model, COMBO_MAP, ExecutorError, get_role_for_domain
from capabilities import CapabilityBundle, resolve_capability
from resilience import sanitize_input_text
from scoring import ScoreStore
from school_mail import notify_issue_alert
from pipeline_metrics import PipelineMetrics
from review_packet import ReviewPacket
from compound_learning import CompoundLearningStore
from crew_admission import decide_admission
from shadow_routing import load_shadow_history
from scripts.ce_router import route_id_for
from bridge_seams import (
    build_evidence_join as _build_evidence_join,
    build_shadow_routing_packet as _build_shadow_routing_packet,
    observability_fields as _observability_fields,
    outcome_fields as _outcome_fields,
    strict_gate_failure as _strict_gate_failure,
)
# U8: crew dispatch (FirstMate -> Orca). Imported at module level so tests can
# monkeypatch the symbols; the crew module itself stays dependency-free of the
# bridge.
from crew_dispatch import (
    CrewResult,
    CrewUnavailableError,
    CREW_RUNS_FILE as CREW_RUNS_FILE,
    DEFAULT_TIMEOUT as CREW_DEFAULT_TIMEOUT,
    dispatch_crew as dispatch_crew,
    sweep_stale_runs as sweep_stale_runs,
)

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

# U8: crew dispatch flag. Read once per cycle (not per issue) so a cycle is
# internally consistent. Default OFF in tests and for direct callers; the
# scheduled school-loop turns it on via CREW_ENABLED=1.
CREW_ENABLED_DEFAULT = False
# Per-cycle cap on crew dispatches (each crew run polls for minutes and the
# job is under a 30-min timeout). Default 1.
CREW_MAX_PER_CYCLE_DEFAULT = 1
# Crew statuses that mean "still active — do not start a second one this cycle".
_CREW_ACTIVE_STATUSES = {"running", "blocked"}


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


_COMMENT_HOME_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+")
_COMMENT_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{12,})\b"
)


def _scrub_comment_text(text: str, limit: int = 160) -> str:
    """Sanitize a text excerpt for a public GitHub comment.

    Home paths are shortened to ``~`` and credential-shaped tokens are
    redacted — the close comment is visible to anyone with repo read access,
    so it must not leak PII (same discipline as scripts/sanitize_data.py).
    """
    text = text or ""
    text = text.replace("\n", " ").strip()
    text = _COMMENT_HOME_RE.sub("~", text)
    text = _COMMENT_TOKEN_RE.sub("[redacted]", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _build_school_comment(
    issue: dict,
    task_result: dict,
    verification: dict,
    adversarial_review: dict,
    verify_skipped: bool,
    entire_summary: Optional[dict],
    combined_score: float,
    crew_used: bool,
    crew_fallback_reason: Optional[str],
) -> str:
    """Render a compact STE close comment from evidence already at hand.

    No extra LLM call: the verdicts, tool outcomes, and the bookbag are all
    recorded by the time the issue closes, so the comment is deterministic
    and cheap. Written for a human reader AND a future agent — it says what
    was done, which tools produced the evidence, and why the school concluded
    success, with an ELI5 block at the bottom (docs/notification-style-guide.md
    vocabulary: "the school", "review", "pre-merge check").

    The bookbag is read best-effort from ``task_result["bookbag"]``; a missing
    or unreadable bookbag omits that section instead of failing the close.
    """
    review = task_result.get("review") or {}
    title = _scrub_comment_text(issue.get("title"), 90) or "(untitled)"
    agent = task_result.get("agent") or issue.get("agent") or "auto"
    domain = task_result.get("domain") or issue.get("domain") or "_default"
    difficulty = task_result.get("difficulty") or issue.get("difficulty") or "medium"
    response = _scrub_comment_text(task_result.get("response"), 220)

    # ── Evidence bullets ──
    cto = review.get("cto_verdict") or "n/a"
    coo = review.get("coo_verdict") or "n/a"
    review_line = f"- Review: CTO {cto} / COO {coo}"
    if review.get("accepted") is not None:
        review_line += f" — {'accepted' if review.get('accepted') else 'not accepted'}"

    adv = adversarial_review or {}
    adv_line = "- Adversarial review: not run"
    if adv.get("verdict") is not None:
        n_findings = len(adv.get("findings") or [])
        adv_line = (
            f"- Adversarial review: {adv.get('verdict')} "
            f"(score {float(adv.get('score') or 0):.0f}, {n_findings} finding(s))"
        )

    if verify_skipped:
        verify_line = "- Verify gate: skipped (no compiler/commands)"
    else:
        ran = (verification or {}).get("ran")
        v_verdict = (verification or {}).get("verdict", "PASS")
        verify_line = f"- Verify gate: {v_verdict}"
        if ran is not None:
            verify_line += f" ({ran} command(s))"

    if entire_summary:
        e_status = entire_summary.get("status") or "n/a"
        e_findings = entire_summary.get("findings") or 0
        entire_line = f"- Pre-merge check: {e_status} ({e_findings} finding(s))"
    else:
        entire_line = "- Pre-merge check: not run"

    if crew_used:
        crew_line = "- Crew: yes (FirstMate/Orca worktree)"
    elif crew_fallback_reason:
        crew_line = f"- Crew: fell back to direct ({_scrub_comment_text(crew_fallback_reason, 60)})"
    else:
        crew_line = "- Crew: not used (direct path)"

    # ── Bookbag summary (best-effort) ──
    bag_lines: list = []
    bag_path = task_result.get("bookbag")
    if bag_path:
        try:
            bag = json.loads(Path(bag_path).read_text())
            bag_summary = (bag.get("summary") or "").strip()
            if not bag_summary and bag.get("output"):
                bag_summary = str(bag.get("output"))[:220]
            if bag_summary:
                bag_lines.append(_scrub_comment_text(bag_summary, 300))
            n_files = len(bag.get("files_changed") or [])
            n_ac = len(bag.get("ac_met") or [])
            n_block = len(bag.get("blockers") or [])
            bag_lines.append(
                f"- Files changed: {n_files} · Acceptance criteria met: {n_ac} · Blockers: {n_block}"
            )
        except Exception:
            bag_lines = []  # unreadable bookbag — omit the section

    # ── Per-judge narrative collapsible sections (best-effort) ──
    # Synthesized at review time (director) and persisted on the review dict
    # and the bookbag. Absent narratives (legacy runs / synthesis failure)
    # simply omit the collapsible blocks — the compact bullets above always
    # carry the verdicts.
    judge_blocks: list = []
    coo_narrative = (review.get("coo_narrative") or {})
    cto_narrative = (review.get("cto_narrative") or {})
    if coo_narrative:
        judge_blocks.append(
            _render_judge_block(
                "COO", "completeness + acceptance", review,
                coo_narrative, "conversational",
            )
        )
    if cto_narrative:
        judge_blocks.append(
            _render_judge_block(
                "CTO", "correctness + security", review,
                cto_narrative, "technical", lesson=True,
            )
        )

    lines = [
        f"✅ Processed by the school — status: success — score: {combined_score:.1f}",
        "",
        "**What the school did**",
        f"- Issue: {title} ({domain}, {difficulty})",
        f"- Agent: {agent}",
        review_line,
        adv_line,
        verify_line,
        entire_line,
        crew_line,
    ]
    if response:
        lines.append(f"- Answer (excerpt): {response}")
    if bag_lines:
        lines += ["", "**Bookbag**", *bag_lines]
    if judge_blocks:
        lines += ["", "**Judge notes**", *judge_blocks]
    lines += [
        "",
        "**In plain words**",
        "What happened: the school read the issue, produced an answer, and "
        "checked it. Two reviewers approved it, so the issue is now closed. "
        "Next step: open the issue to see the details.",
    ]
    return "\n".join(lines)


def _render_judge_block(
    judge: str,
    lenses: str,
    review: dict,
    narrative: dict,
    tone: str,
    lesson: bool = False,
) -> str:
    """Render one judge's narrative as a GitHub collapsible ``<details>`` block.

    Each judge gets its own persona: ``tone`` names the voice (conversational
    for COO, technical for CTO), and the CTO block additionally carries a
    "what to learn from this" line. Everything is scrubbed for the public
    comment. Returns an empty string if the narrative carries no content.
    """
    verdict = review.get("cto_verdict") if judge == "CTO" else review.get("coo_verdict")
    score = review.get("cto_score") if judge == "CTO" else review.get("coo_score")
    score_txt = f"score {float(score):.0f}" if isinstance(score, (int, float)) else ""
    # Distinct per-judge marker: CTO is technical (👔), COO conversational (🗣️).
    marker = "👔" if tone == "technical" else "🗣️"
    lines = [
        f"<details>",
        f"<summary>{marker} {judge} review — {lenses} ({verdict or 'n/a'}{', ' + score_txt if score_txt else ''})</summary>",
        "",
    ]
    if narrative.get("summary"):
        lines.append(f"**Lens summary:** {_scrub_comment_text(narrative['summary'], 400)}")
        lines.append("")
    for label, key in (("What I liked", "liked"), ("Could do better", "improve")):
        if narrative.get(key):
            lines.append(f"- **{label}:** {_scrub_comment_text(narrative[key], 300)}")
    for label, key in (("Why it passed", "why_passed"), ("Why it failed", "why_failed")):
        if narrative.get(key):
            lines.append(f"- **{label}:** {_scrub_comment_text(narrative[key], 300)}")
    if lesson and narrative.get("lesson"):
        lines.append(f"- **What to learn from this:** {_scrub_comment_text(narrative['lesson'], 300)}")
    lines.append("</details>")
    return "\n".join(lines)


def _mark_github_issue(repo: str, issue_number: int, status: str,
                       score: Optional[float] = None,
                       comment: Optional[str] = None) -> None:
    """Reflect a processed issue on GitHub so the repo list shows the school's work.

    - ``status == "success"`` → add the ``school-done`` label and close the
      issue, with the combined score in the close comment (or the provided
      rich comment when given).
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
            if comment is None:
                comment = "✅ Processed by the school — status: success"
                if score is not None:
                    comment += f" — score: {score:.1f}"
            _gh_command(["issue", "close", str(issue_number), "--repo", repo, "--comment", comment])
    except Exception as e:
        sys.stderr.write(f"[issue_bridge] Failed to update GitHub issue #{issue_number}: {e}\n")


def _prepare_run_entry(
    entry: dict,
    metrics: Optional[PipelineMetrics] = None,
) -> dict:
    """Add the server timestamp and optional bounded metrics packet once."""
    persistence_started = time.perf_counter()
    if "timestamp" not in entry:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    if metrics is not None:
        metrics.record_stage_duration(
            "persistence",
            (time.perf_counter() - persistence_started) * 1000.0,
        )
        entry["pipeline_metrics"] = metrics.snapshot()
    return entry


def _load_run_entries(path: Path) -> list[dict]:
    """Load a JSON-list run log, treating missing/corrupt state as empty."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _write_run_entries(path: Path, entries: list[dict]) -> None:
    """Atomically replace a JSON-list run log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(tmp, path)


def _record_compound_observation(
    *,
    bead_id: str,
    trigger: str,
    evidence: dict,
) -> Optional[dict]:
    """Start the bounded post-bead learning loop without changing policy."""
    try:
        path = PROCESSED_FILE.parent / "compound_learning.json"
        return CompoundLearningStore(path).observe(
            bead_id=bead_id,
            trigger=trigger,
            evidence=evidence,
        )
    except Exception as exc:  # learning must not change task acceptance
        sys.stderr.write(f"[issue_bridge] compound observation skipped: {exc}\n")
        return None


class RunBatch:
    """Accumulate one bridge cycle's run entries and flush them atomically.

    The bridge still owns retry/processed/score checkpoints separately. This
    narrow batch only removes repeated read/modify/replace cycles for the
    append-only ``last_run.json`` journal while preserving its JSON shape and
    corruption recovery behavior.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._pending: list[dict] = []

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def append(
        self,
        entry: dict,
        metrics: Optional[PipelineMetrics] = None,
    ) -> None:
        self._pending.append(_prepare_run_entry(entry, metrics))

    def flush(self) -> None:
        """Append pending entries with one atomic read/modify/replace."""
        if not self._pending:
            return
        existing = _load_run_entries(self.path)
        existing.extend(self._pending)
        _write_run_entries(self.path, existing)
        self._pending.clear()


def record_run(
    path: Path,
    entry: dict,
    metrics: Optional[PipelineMetrics] = None,
) -> None:
    """Append one entry to a JSON-list run log with atomic replacement."""
    prepared = _prepare_run_entry(entry, metrics)
    existing = _load_run_entries(path)
    existing.append(prepared)
    _write_run_entries(path, existing)


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


def _select_verification(
    *,
    crew_used: bool,
    crew_premerge_verification: Optional[dict],
    canonical_packet: Optional[object],
    issue: dict,
    repo_path: Optional[Path],
    metrics: "PipelineMetrics",
) -> Optional[dict]:
    """Select the verify-gate result, enforcing the fc7.3 / worst-day-ever N5.3
    invariant: a CREW run must reuse its live-worktree verification and NEVER
    re-run the gate on the clean cached base (repo_path) after teardown.

    Re-running on the base would produce a FALSE PASS — the student's diff is
    gone, so the gate checks an empty tree. This function makes that contract
    explicit and un-branchable: when `crew_used` is True, the only allowed
    outcomes are (a) reuse the crew's pre-merge verification, or (b) a strict
    gate failure if it is missing. `_run_verify_gate(repo_path, ...)` is reached
    ONLY for the direct/manual (non-crew) path.

    Returns a verify result dict, or None if no gate is applicable (direct path
    with no verifiable project). A strict failure is a non-None dict with
    passed=False.
    """
    if crew_used:
        # Mandatory reuse. The cached repo_path is the clean base after teardown
        # and MUST NOT be re-gated here.
        metrics.record_call("verify_gate_reused")
        with metrics.stage("verify"):
            return crew_premerge_verification or _strict_gate_failure(
                "crew pre-merge verification was unavailable after teardown"
            )
    if (
        canonical_packet is not None
        and getattr(canonical_packet, "is_authoritative", False)
        and getattr(canonical_packet, "is_verification_authoritative", False)
    ):
        # The director already ran the authoritative build gate; reuse it.
        metrics.record_call("verify_gate_reused")
        with metrics.stage("verify"):
            return getattr(canonical_packet, "verification", None)
    # Direct/manual path only — here (and ONLY here) may we run the gate on the
    # checkout. This branch is unreachable for crew runs by construction.
    metrics.record_call("verify_gate")
    metrics.record_verification(invocations=1)
    with metrics.stage("verify"):
        return _run_verify_gate(repo_path, issue)


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


def _crew_enabled_from_env() -> bool:
    """Parse CREW_ENABLED with lenient truthiness (1/true/yes/on → on).

    Anything else — absent, 0, false, or garbage — is OFF. Invalid values must
    fail closed: an unparseable flag must never silently enable crew dispatch.
    """
    raw = os.environ.get("CREW_ENABLED", "")
    if not raw:
        return CREW_ENABLED_DEFAULT
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _quarantine_corrupt_registry(crew_runs_file) -> None:
    """Move a corrupted registry aside so it can't silently re-trigger.

    F6-concurrency / worst-day-ever N5.1: a truncated registry previously made
    admission think ZERO crews were in flight, defeating the cap (over-admit).
    We never swallow corruption — we quarantine it and let the caller fail CLOSED.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        corrupt = crew_runs_file.with_suffix(crew_runs_file.suffix + f".corrupt-{ts}")
        crew_runs_file.replace(corrupt)
        sys.stderr.write(
            f"[issue_bridge] QUARANTINED corrupt crew registry {crew_runs_file} -> {corrupt}\n"
        )
    except OSError as e:
        sys.stderr.write(f"[issue_bridge] failed to quarantine corrupt registry: {e}\n")


def _crew_active_count(crew_runs_file) -> int:
    """Count durable active crew claims without exposing paths or payloads.

    F6-concurrency / worst-day-ever N5.1 (fail-closed): on a corrupted registry
    we QUARANTINE it and return a value larger than any cap so admission DENIES
    (assume everything is in flight) instead of the old behavior of returning 0
    and over-admitting. A missing file is the only valid empty state.
    """
    if not crew_runs_file.exists():
        return 0
    try:
        raw = json.loads(crew_runs_file.read_text())
    except json.JSONDecodeError:
        _quarantine_corrupt_registry(crew_runs_file)
        return sys.maxsize  # fail closed: behave as if fully saturated
    runs = raw if isinstance(raw, list) else []
    return sum(1 for entry in runs if entry.get("status") in _CREW_ACTIVE_STATUSES)


def _crew_active_issue(crew_runs_file, issue_number: int) -> bool:
    """True when the durable registry has an active (running/blocked) record.

    Matches by ``issue_number`` — NOT by crew_id. crew_id embeds the cycle's
    session id (``fm-loop-<cycle>-<issue>``), so matching it would only ever
    fire within the cycle that wrote the record; an interrupted prior cycle
    would never be seen again. The registry is written by crew_dispatch (U7)
    and checkpointed, so a leftover active record means the crew may still
    hold the issue's worktree — starting a second crew would double-spawn.
    Skip the issue this cycle and let the stale sweep / next cycle reclaim it.

    F6-concurrency / worst-day-ever N5.1 (fail-closed): a corrupted registry is
    quarantined and treated as "this issue is active" so we SKIP rather than
    risk a double-spawn. Only a missing file is a valid empty state.
    """
    if not crew_runs_file.exists():
        return False
    try:
        raw = json.loads(crew_runs_file.read_text())
    except json.JSONDecodeError:
        _quarantine_corrupt_registry(crew_runs_file)
        return True  # fail closed: skip the issue, avoid double-spawn
    runs = raw if isinstance(raw, list) else []
    return any(
        int(entry.get("issue_number", -1)) == int(issue_number)
        and entry.get("status") in _CREW_ACTIVE_STATUSES
        for entry in runs
    )


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _resolve_crew_capability(
    issue: dict,
    store: ScoreStore,
    force_agent: Optional[str] = None,
) -> Optional[CapabilityBundle]:
    """Resolve the canonical role/profile/tool policy before crew launch.

    The bridge must choose the bundle before FirstMate creates the worktree.
    ``force_agent`` preserves the existing manual override; otherwise the
    domain map supplies the deterministic task role and the score store only
    supplies the school-rank input to the same canonical resolver used by
    direct student leaves.
    """
    try:
        domain = issue["domain"]
        task_role = force_agent or get_role_for_domain(domain)
        score = store.get_score(task_role, domain)
        return resolve_capability(
            domain,
            score,
            task_role=task_role,
            difficulty=issue.get("difficulty", "medium"),
        )
    except Exception as exc:
        sys.stderr.write(
            f"[issue_bridge] crew capability resolution skipped: {exc}\n"
        )
        return None


def _crew_report_content(report_path) -> Optional[str]:
    """Read a bounded crew report.md into the student deliverable.

    Bounded read guards against a pathologically large report; an unreadable
    report falls back to direct execution (the crew produced no usable
    deliverable).
    """
    if report_path is None:
        return None
    try:
        report = Path(report_path)
        if not report.exists():
            return None
        if report.stat().st_size > 512 * 1024:  # 512 KiB hard bound
            return None
        content = report.read_text(encoding="utf-8")
    except OSError:
        return None
    return content if content.strip() else None


def bridge_issues(
    repo: str,
    labels: Optional[List[str]] = None,
    force_agent: Optional[str] = None,
    dry_run: bool = False,
    store: Optional[ScoreStore] = None,
    crew_enabled: Optional[bool] = None,
    crew_max_per_cycle: Optional[int] = None,
    cycle_session_id: Optional[str] = None,
) -> list[dict]:
    """Fetch actionable issues and dispatch each as a Director task.

    Returns list of result dicts, one per issue processed.

    `store` is injectable for test isolation; when omitted a live ScoreStore
    (data/scores.json) is used in production. Tests MUST pass a temp store to
    avoid polluting the real scores file.

    `crew_enabled` is an explicit knob; None reads CREW_ENABLED from the
    environment once per cycle (tests pass False to stay on today's path).
    `crew_max_per_cycle` caps crew dispatches per cycle (default 1).
    `cycle_session_id` overrides the auto-generated loop-* id (tests use a
    fixed id so crew_ids and session threading are deterministic).
    """
    from director import run_task, evaluate_and_update

    if store is None:
        store = ScoreStore()
    # U8: read the flag once per cycle so a cycle is internally consistent.
    crew_enabled = (
        _crew_enabled_from_env() if crew_enabled is None else bool(crew_enabled)
    )
    crew_max_per_cycle = (
        CREW_MAX_PER_CYCLE_DEFAULT if crew_max_per_cycle is None else int(crew_max_per_cycle)
    )
    if not repo:
        # school-loop passes --repo "$SCHOOL_REPO" (usually empty) → resolve the
        # repo from the current checkout's origin remote, same as bridge_poll.
        from repo_default import default_repo
        repo = default_repo()
    issues = fetch_issues(repo, labels)
    processed = _load_processed()
    retries = _load_retries()
    results = []
    crew_dispatched = 0
    cycle_started = time.monotonic()
    runner_slots = _env_int("CREW_RUNNER_SLOTS", crew_max_per_cycle, minimum=0)
    cycle_budget_seconds = _env_float("CREW_CYCLE_BUDGET_SECONDS", 1800.0, minimum=0.0)
    retry_pressure_limit = _env_int("CREW_RETRY_PRESSURE_LIMIT", 2, minimum=1)

    # U1: one session_id per cycle (not per issue) so Layer 3 archival
    # context can accumulate across the school-loop's sleep/wake cycles and
    # the orchestrator's `if session_id:` gate actually fires. Seconds-level
    # granularity keeps a manual dispatch + the scheduled cron in the same
    # minute from sharing a key (which would clobber the same consolidation
    # dir if the write side ever runs under a loop-* id).
    cycle_session_id = cycle_session_id or f"loop-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    if not issues:
        sys.stderr.write("[issue_bridge] No actionable issues found.\n")
        return results

    # Dry-run is an inspection mode, not a partial execution. It must not
    # refresh/delete the repo cache, clone anything, or write processed/last-run
    # state. Fetching and classification above are the complete read-only
    # preflight; report that codebase context was intentionally not collected.
    if dry_run:
        for issue in issues:
            num = issue["issue_number"]
            if num in processed:
                continue
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "dry_run",
                "codebase_context_chars": 0,
                "codebase_context_collected": False,
            })
        return results

    # Clone repo and build codebase context for enrichment
    from repo_reader import clone_repo, build_codebase_context, cleanup_stale_caches
    cleanup_stale_caches()
    repo_path = clone_repo(repo)
    # Shadow evidence is observational and bounded. Load the same snapshot
    # once per bridge cycle rather than reparsing last_run.json for every
    # issue; this also gives all issues in a cycle a consistent baseline.
    shadow_history = load_shadow_history(PROCESSED_FILE.parent / "last_run.json")
    run_batch = RunBatch(PROCESSED_FILE.parent / "last_run.json")

    for issue in issues:
        num = issue["issue_number"]
        metrics = PipelineMetrics()
        if num in processed:
            continue

        # Build codebase context for this issue
        # N1.1 (worst-day-ever): scrub the issue title/body at the curriculum
        # edge so RTL overrides, null bytes, and oversized bodies can't reach a
        # crew brief or agent prompt. The shell-quoting contract (N1.2) is
        # handled by dispatch_crew's list-args subprocess call.
        issue_text = (
            f"{sanitize_input_text(issue['title'])}\n\n"
            f"{sanitize_input_text(issue.get('body', ''))}"
        )
        codebase_ctx = ""
        with metrics.stage("context"):
            if repo_path:
                codebase_ctx = build_codebase_context(repo_path, issue_text)
        metrics.record_context("codebase", hit=bool(codebase_ctx))

        # Enrich prompt with codebase context
        # N1.1 (worst-day-ever): sanitize the issue prompt before it becomes the
        # crew brief — same curriculum-edge scrub as the title/body above.
        enriched_prompt = sanitize_input_text(issue["prompt"])
        if codebase_ctx:
            enriched_prompt = f"{codebase_ctx}\n\n## Issue\n{enriched_prompt}"

        # ── U8: crew dispatch path ──────────────────────────────────────
        # When enabled, route the student-task through a real code-producing
        # crew (FirstMate -> Orca) before the direct model path:
        #  - done → the crew's report.md becomes the student deliverable and
        #    flows through the normal review/scoring (no student model call).
        #  - CrewUnavailableError (spawn failure) or a fast ordinary failed
        #    status → same-cycle fallback to the direct path;
        #  - timeout/blocked → bounded retry after teardown, avoiding a second
        #    full-length attempt in the same supervisor window;
        #    the fallback_reason is recorded on the result.
        #  - Fallback itself fails → existing retry-once semantics carry.
        #  - An active crew record (interrupted prior cycle) skips the issue;
        #    a per-cycle cap keeps serial crew polling inside the job timeout.
        crew_id = f"fm-{cycle_session_id}-{num}"
        crew_capability = _resolve_crew_capability(issue, store, force_agent) if crew_enabled else None
        crew_result: Optional[CrewResult] = None
        crew_fallback_reason: Optional[str] = None
        crew_used = False
        crew_skip_reason: Optional[str] = None
        defer_direct_fallback = False

        if crew_enabled and crew_capability is None:
            crew_skip_reason = "capability_resolution_failure"
            crew_fallback_reason = crew_skip_reason
            sys.stderr.write(
                f"[issue_bridge] #{num}: capability policy unavailable — direct fallback\n"
            )

        if crew_enabled and crew_skip_reason is None and _crew_active_issue(CREW_RUNS_FILE, num):
            metrics.record_crew("in_flight")
            crew_skip_reason = "crew_in_flight"
            sys.stderr.write(f"[issue_bridge] #{num}: crew {crew_id} still active — skipping this cycle\n")
            # A stale record (crew aborted > CREW_TIMEOUT_SECONDS ago) can
            # otherwise strand the issue: the sweep only runs inside
            # dispatch_crew, which the skip prevents. Sweep now so a stale
            # record is reclaimed and the next cycle can retry the issue.
            # Best-effort — never blocks the skip path.
            try:
                sweep_stale_runs(path=CREW_RUNS_FILE, stale_after=CREW_DEFAULT_TIMEOUT)
            except Exception as _e_sweep:
                sys.stderr.write(f"[issue_bridge] crew stale sweep failed for #{num}: {_e_sweep}\n")
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "crew_in_flight",
                "crew_skip_reason": crew_skip_reason,
                "crew_id": crew_id,
            })
            continue

        if crew_enabled and crew_skip_reason is None:
            # F6-concurrency (fc7.3 verify-contract repair): admission must
            # count CREWS THAT ARE CURRENTLY IN FLIGHT, not a local int that is
            # only incremented AFTER dispatch_crew() returns. dispatch_crew
            # writes a `running` record to CREW_RUNS_FILE the moment it spawns
            # (before the long poll), so reading the durable, lock-safe registry
            # here makes admission reflect live in-flight crews. This is what
            # keeps configured_cap honest once the loop dispatches more than one
            # crew before any returns — the stale local counter would let every
            # concurrent decision see `dispatched=0` and over-admit.
            live_active = _crew_active_count(CREW_RUNS_FILE)
            admission = decide_admission(
                dispatched=live_active,
                configured_cap=crew_max_per_cycle,
                runner_slots=runner_slots,
                active_claims=live_active,
                remaining_seconds=cycle_budget_seconds - (time.monotonic() - cycle_started),
                crew_timeout_seconds=CREW_DEFAULT_TIMEOUT,
                retry_pressure=len(retries),
                retry_pressure_limit=retry_pressure_limit,
            )
            if not admission.admitted:
                crew_skip_reason = admission.reason
                crew_fallback_reason = crew_skip_reason
                sys.stderr.write(
                    f"[issue_bridge] #{num}: crew admission denied ({crew_skip_reason}) — direct path\n"
                )

        if crew_enabled and crew_skip_reason is None:
            metrics.record_crew("spawn")
            crew_dispatched += 1
            try:
                crew_result = dispatch_crew(
                    issue_number=num,
                    # The student has the actual checkout and can inspect it;
                    # do not duplicate the potentially huge codebase context in
                    # the Hermes launch brief. Keep enriched_prompt for the
                    # director's review path below.
                    task_text=issue["prompt"],
                    project_dir=repo_path or Path.cwd(),
                    cycle_session_id=cycle_session_id,
                    capability=crew_capability,
                )
                metrics.record_crew("poll")
                if crew_result.teardown_ok:
                    metrics.record_crew("teardown")
            except CrewUnavailableError as e:
                crew_fallback_reason = "spawn_failure"
                sys.stderr.write(
                    f"[issue_bridge] #{num}: crew spawn failed ({e}) — direct fallback\n"
                )
            except Exception as e:
                crew_fallback_reason = "crew_unexpected"
                sys.stderr.write(
                    f"[issue_bridge] #{num}: crew dispatch raised ({e}) — direct fallback\n"
                )

        deliverable: Optional[str] = None
        if crew_result is not None and crew_result.status == "done":
            deliverable = _crew_report_content(crew_result.report_path)
            if deliverable is None:
                crew_fallback_reason = crew_result.fallback_reason or "report_unusable"
                sys.stderr.write(
                    f"[issue_bridge] #{num}: crew done but no usable report ({crew_fallback_reason}) — direct fallback\n"
                )
                crew_result = None
        elif crew_result is not None:
            # Fast ordinary failures can use the direct path, but a timeout or
            # explicit blocked state must not launch a second full-length model
            # attempt in the same cycle. That used to exceed the outer
            # supervisor window and hid the original runtime failure. Preserve
            # the crew evidence and let the existing retry-once path record a
            # bounded retry after teardown.
            crew_fallback_reason = crew_result.fallback_reason or crew_result.status
            defer_direct_fallback = crew_result.status in {"timeout", "blocked"}
            action = "schedule bounded retry" if defer_direct_fallback else "direct fallback"
            sys.stderr.write(
                f"[issue_bridge] #{num}: crew {crew_result.status} ({crew_fallback_reason}) — {action}\n"
            )

        if crew_fallback_reason:
            metrics.record_crew("fallback")

        # Capture authoritative sensors before the bridge asks the director
        # to review the report. These were run against the live student
        # worktree by dispatch_crew, before teardown; the cached repo_path is
        # only the clean base and must never replace them for a crew run.
        crew_premerge_verification = (
            getattr(crew_result, "verification", None)
            if crew_result is not None and crew_result.status == "done"
            else None
        )
        crew_premerge_entire = (
            getattr(crew_result, "entire_review", None)
            if crew_result is not None and crew_result.status == "done"
            else None
        )

        # Keep the direct review path on the same task role selected for the
        # crew. This prevents an A2A/readiness reroute from silently changing
        # the persona after a crew fallback or provided report.
        dispatch_force_agent = (
            force_agent
            or (crew_capability.task_role if crew_enabled and crew_capability else None)
        )
        try:
            if defer_direct_fallback:
                raise RuntimeError(
                    f"crew {crew_result.status}: {crew_fallback_reason or crew_result.status}"
                )
            with metrics.stage("student_generation"):
                if crew_result is not None and crew_result.status == "done":
                    # The crew's report.md IS the student deliverable: substitution
                    # through run_task keeps review/scoring/bookbag on one path.
                    crew_used = True
                    task_result = run_task(
                        prompt=enriched_prompt,
                        domain=issue["domain"],
                        difficulty=issue["difficulty"],
                        force_agent=dispatch_force_agent,
                        store=store,
                        session_id=cycle_session_id,
                        repo=repo,
                        repo_path=repo_path,
                        provided_student_output=deliverable,
                        preverified_verification=crew_premerge_verification,
                        pipeline_metrics=metrics,
                    )
                else:
                    task_result = run_task(
                        prompt=enriched_prompt,
                        domain=issue["domain"],
                        difficulty=issue["difficulty"],
                        force_agent=dispatch_force_agent,
                        store=store,
                        session_id=cycle_session_id,
                        repo=repo,
                        repo_path=repo_path,
                        pipeline_metrics=metrics,
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
                    "crew_id": crew_result.crew_id if crew_result else None,
                    "crew_used": crew_used,
                    "crew_fallback_reason": crew_fallback_reason,
                    "teardown_ok": crew_result.teardown_ok if crew_result else None,
                    **_outcome_fields(
                        status="retry",
                        error=err,
                        fallback_reason=crew_fallback_reason,
                        retry_attempt=attempts,
                    ),
                })
                try:
                    run_batch.append(
                        {
                            "issue": num,
                            "status": "retry",
                            "agent": None,
                            "score": None,
                            "trajectory": None,
                            **_outcome_fields(
                                status="retry",
                                error=err,
                                fallback_reason=crew_fallback_reason,
                                retry_attempt=attempts,
                            ),
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
                continue
            retries.pop(num, None)
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "error",
                "error": err,
                "crew_id": crew_result.crew_id if crew_result else None,
                "crew_used": crew_used,
                "crew_fallback_reason": crew_fallback_reason,
                "teardown_ok": crew_result.teardown_ok if crew_result else None,
                **_outcome_fields(
                    status="error",
                    error=err,
                    fallback_reason=crew_fallback_reason,
                    retry_attempt=attempts,
                ),
            })
            try:
                run_batch.append(
                    {
                        "issue": num,
                        "status": "error",
                        "agent": None,
                        "score": None,
                        "trajectory": None,
                        **_outcome_fields(
                            status="error",
                            error=err,
                            fallback_reason=crew_fallback_reason,
                            retry_attempt=attempts,
                        ),
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
            continue

        if task_result["status"] == "success":
            canonical_packet = ReviewPacket.from_dict(task_result.get("review_packet"))
            # ── Two-judge acceptance gate ──
            # run_task already ran the CTO+COO review; both must PASS at
            # score >= 50 with no CRITICAL finding for accepted=True. The
            # bridge must honor that verdict at CLOSE time — a rejected
            # review (low score / FAIL verdict / CRITICAL finding) is a real
            # quality failure, not a pass. Rejected → school-failed + left
            # open for human triage, exactly like the exception path.
            # (Observed 2026-08-12: issues #51/#52 scored 33/35 and were
            # closed school-done because the verdict was never consulted.)
            # A missing review (legacy/async fixtures) passes through: the
            # async skip_review path intentionally carries empty verdicts.
            _review = task_result.get("review") or {}
            if canonical_packet is not None and canonical_packet.is_authoritative:
                _review = dict(_review)
                _review["accepted"] = canonical_packet.accepted
            _reviewed = bool(
                canonical_packet is not None and canonical_packet.is_authoritative
            ) or bool(_review.get("cto_verdict") or _review.get("coo_verdict"))
            if _reviewed and _review.get("accepted") is False:
                _reject_reason = (
                    f"two-judge review rejected: cto={_review.get('cto_verdict')} "
                    f"coo={_review.get('coo_verdict')} "
                    f"combined={_review.get('combined_score')}"
                )
                sys.stderr.write(f"[issue_bridge] #{num}: {_reject_reason} — school-failed\n")
                rejection_outcome = _outcome_fields(
                    status="error",
                    task_result=task_result,
                    error=_reject_reason,
                    review=_review,
                    fallback_reason=crew_fallback_reason,
                )
                rejection_observation = _record_compound_observation(
                    bead_id=str(issue.get("bd_id") or task_result.get("bead") or f"issue-{num}"),
                    trigger="bead_failed",
                    evidence={"outcome": rejection_outcome},
                )
                # Score store reflects the director's designed penalty
                # (task_score is min(40, combined) for a rejection).
                evaluate_and_update(task_result, task_result.get("task_score", 0.0), store=store)
                results.append({
                    "issue_number": num,
                    "title": issue["title"],
                    "domain": issue["domain"],
                    "difficulty": issue["difficulty"],
                    "status": "error",
                    "error": _reject_reason,
                    "capability": task_result.get("capability"),
                    "teacher_evidence": task_result.get("teacher_evidence"),
                    "crew_id": crew_result.crew_id if crew_result else None,
                    "crew_used": crew_used,
                    "crew_fallback_reason": crew_fallback_reason,
                    "teardown_ok": crew_result.teardown_ok if crew_result else None,
                    "compound_observation_id": (
                        rejection_observation or {}
                    ).get("observation_id"),
                    **rejection_outcome,
                })
                try:
                    run_batch.append(
                        {
                            "issue": num,
                            "status": "school-failed",
                            "agent": task_result.get("agent"),
                            "score": _review.get("combined_score"),
                            "rejection": _reject_reason,
                            "trajectory": task_result.get("trajectory"),
                            "capability": task_result.get("capability"),
                            "teacher_evidence": task_result.get("teacher_evidence"),
                            "compound_observation_id": (
                                rejection_observation or {}
                            ).get("observation_id"),
                            **rejection_outcome,
                        },
                    )
                except Exception as e_rec:
                    sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
                _mark_github_issue(repo, num, "error")
                try:
                    notify_issue_alert(num, issue["title"], "school-failed",
                                       error=_reject_reason,
                                       repo=repo, retry_limit=RETRY_LIMIT)
                except Exception as e_notify:
                    sys.stderr.write(f"[issue_bridge] Alert failed for #{num}: {e_notify}\n")
                retries.pop(num, None)
                processed.add(num)
                continue

            # NEW: run the code before the critic speaks (campus.md #3).
            # Compile/typecheck/test failures become CRITICAL findings fed
            # into the adversarial reviewer, so broken code can't pass review.
            # _select_verification enforces the fc7.3 / worst-day-ever N5.3
            # invariant: a CREW run reuses its live-worktree verification and
            # never re-gates the clean base after teardown (false pass).
            verify_result = _select_verification(
                crew_used=crew_used,
                crew_premerge_verification=crew_premerge_verification,
                canonical_packet=canonical_packet,
                issue=issue,
                repo_path=repo_path,
                metrics=metrics,
            )
            if verify_result:
                gate_metrics = verify_result.get("telemetry") or {}
                metrics.record_verification(
                    shell_starts=gate_metrics.get("shell_starts", 0),
                    commands=gate_metrics.get("commands", 0),
                    copied_bytes=gate_metrics.get("copied_bytes", 0),
                )

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
            if crew_used:
                # Same lifecycle rule as verification: do not inspect the clean
                # base after the student's worktree has been torn down.
                metrics.record_call("entire_reused")
                with metrics.stage("entire"):
                    entire_review = crew_premerge_entire
            else:
                metrics.record_call("entire")
                with metrics.stage("entire"):
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
            if canonical_packet is not None and canonical_packet.is_authoritative:
                canonical_packet.attach_entire(entire_summary)

            # Reuse the director's canonical CTO+COO result when available;
            # legacy task results retain the bridge's old review fallback.
            if canonical_packet is not None and canonical_packet.is_authoritative:
                metrics.record_call("adversarial_review_reused")
                with metrics.stage("review"):
                    adversarial_review = canonical_packet.adversarial_summary()
            else:
                metrics.record_call("adversarial_review")
                with metrics.stage("review"):
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
            metrics.record_call("output_verification")
            with metrics.stage("output_verification"):
                verification = verify_task_output(
                    original_prompt=enriched_prompt,
                    agent_response=task_result["response"],
                    domain=issue["domain"],
                    difficulty=issue["difficulty"],
                    codebase_context=codebase_ctx,
                )

            if canonical_packet is not None and canonical_packet.is_authoritative:
                canonical_packet.attach_output_verification(verification)
                task_result["review_packet"] = canonical_packet.to_dict()

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
            try:
                shadow_candidates = store.list_agents()
            except Exception:
                shadow_candidates = [task_result.get("agent")]
            shadow_routing = _build_shadow_routing_packet(
                task_result,
                issue,
                combined_score,
                retries.get(num, 0),
                shadow_history,
                shadow_candidates,
            )
            task_result["shadow_routing"] = shadow_routing
            with metrics.stage("scoring"):
                updated = evaluate_and_update(task_result, combined_score, store=store)
            review_evidence = task_result.get("review") or {}
            critical_findings = sum(
                1 for finding in (review_evidence.get("findings") or [])
                if finding.get("severity") == "CRITICAL"
            )
            critical_findings += sum(
                1 for finding in (adversarial_review.get("findings") or [])
                if finding.get("severity") == "CRITICAL"
            )
            metrics.record_quality(
                accepted=review_evidence.get("accepted"),
                critical_findings=critical_findings,
                retry_count=retries.get(num, 0),
            )
            outcome = _outcome_fields(
                status="success",
                task_result=task_result,
                review=review_evidence,
                entire=entire_summary,
                verification={
                    **verification,
                    **(verify_result or {}),
                },
                fallback_reason=crew_fallback_reason,
                retry_attempt=retries.get(num, 0),
            )
            project_gate = (
                "pass" if verify_result and verify_result.get("passed")
                else "skipped" if verify_skipped
                else "fail" if verify_result else "unavailable"
            )
            artifact_identity = (
                getattr(crew_result, "artifact_identity", None) or {}
                if crew_result is not None
                else {}
            )
            evidence_join = _build_evidence_join(
                control={
                    "route_id": task_result.get("route_id") or route_id_for(
                        task_result.get("bead") or issue.get("bd_id")
                    ),
                    "bd_id": issue.get("bd_id"),
                    "plan_id": issue.get("plan_id"),
                    "plan_unit": issue.get("plan_unit"),
                    "wayfinder_id": issue.get("wayfinder_id"),
                    "knowledge_anchor": issue.get("knowledge_anchor"),
                    "primary_workflow": task_result.get("primary_workflow"),
                    "chosen_skill": task_result.get("chosen_skill"),
                },
                runtime={
                    "dispatcher": "firstmate" if crew_used else "direct-orca",
                    "cycle_session_id": cycle_session_id,
                    "firstmate_crew_id": crew_result.crew_id if crew_result else None,
                    "orca_worktree_id": crew_result.orca_worktree_id if crew_result else None,
                    "hermes_session_id": task_result.get("hermes_session_id"),
                },
                artifact={
                    "repository": repo,
                    "base_ref": artifact_identity.get("base"),
                    "branch": artifact_identity.get("branch"),
                    "commit": artifact_identity.get("commit"),
                    "changed_files": artifact_identity.get("changed_files"),
                    "trajectory_ref": task_result.get("trajectory"),
                    "bookbag_ref": task_result.get("bookbag"),
                    "report_ref": str(crew_result.report_path) if crew_result and crew_result.report_path else None,
                },
                verification={
                    "project_gate": project_gate,
                    "project_gate_reason": ((verify_result or {}).get("failures") or [{}])[0].get("stderr"),
                    "entire_status": entire_summary.get("status") if entire_summary else None,
                    "entire_finding_count": entire_summary.get("findings", 0) if entire_summary else 0,
                    "entire_findings": (entire_review or {}).get("findings", []) if entire_review else [],
                },
                judgment={
                    **review_evidence,
                    "score": review_evidence.get("combined_score", combined_score),
                },
                outcome=outcome,
            )
            compound_observation = _record_compound_observation(
                bead_id=str(issue.get("bd_id") or task_result.get("bead") or f"issue-{num}"),
                trigger="bead_completed",
                evidence=evidence_join,
            )
            results.append({
                "issue_number": num,
                "title": issue["title"],
                "domain": issue["domain"],
                "difficulty": issue["difficulty"],
                "status": "success",
                "agent": task_result.get("agent"),
                **_observability_fields(task_result),
                "old_score": updated.get("old_score"),
                "new_score": updated.get("new_score"),
                "gate_crossed": updated.get("gate_crossed"),
                "verification": verification,
                "adversarial_review": adversarial_review,
                "review_packet": task_result.get("review_packet"),
                "verify_skipped": verify_skipped,
                "entire_review": entire_review,
                "crew_id": crew_result.crew_id if crew_result else None,
                "crew_used": crew_used,
                "crew_fallback_reason": crew_fallback_reason,
                "teardown_ok": crew_result.teardown_ok if crew_result else None,
                "shadow_routing": shadow_routing,
                "pipeline_metrics": metrics.snapshot(),
                "evidence_join": evidence_join,
                "compound_observation_id": (
                    compound_observation or {}
                ).get("observation_id"),
                **outcome,
            })
            try:
                run_batch.append(
                    {
                        "issue": num,
                        "status": "success",
                        "agent": task_result.get("agent"),
                        "score": combined_score,
                        "trajectory": task_result.get("trajectory"),
                        **_observability_fields(task_result),
                        "review_packet": task_result.get("review_packet"),
                        # F7 is observational: this packet is for offline
                        # evaluation only and never feeds route_task.
                        "shadow_routing": shadow_routing,
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
                        # U8: crew path metadata for surfacing (U9): crew_used
                        # true when the crew report was the deliverable;
                        # fallback_reason names spawn/timeout/cap skips.
                        "crew_id": crew_result.crew_id if crew_result else None,
                        "crew_used": crew_used,
                        "crew_fallback_reason": crew_fallback_reason,
                        "teardown_ok": crew_result.teardown_ok if crew_result else None,
                        "evidence_join": evidence_join,
                        "compound_observation_id": (
                            compound_observation or {}
                        ).get("observation_id"),
                        **outcome,
                    },
                    metrics=metrics,
                )
            except Exception as e_rec:
                sys.stderr.write(f"[issue_bridge] Failed to record run for #{num}: {e_rec}\n")
            _mark_github_issue(
                repo, num, "success", score=combined_score,
                comment=_build_school_comment(
                    issue, task_result, verification, adversarial_review,
                    verify_skipped, entire_summary, combined_score,
                    crew_used, crew_fallback_reason,
                ),
            )
            retries.pop(num, None)
            processed.add(num)
        else:
            err = task_result.get("error")
            attempts = retries.get(num, 0) + 1
            if attempts < RETRY_LIMIT:
                outcome = _outcome_fields(
                    status="retry",
                    task_result=task_result,
                    error=err,
                    fallback_reason=crew_fallback_reason,
                    retry_attempt=attempts,
                )
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
                    "crew_id": crew_result.crew_id if crew_result else None,
                    "crew_used": crew_used,
                    "crew_fallback_reason": crew_fallback_reason,
                    "teardown_ok": crew_result.teardown_ok if crew_result else None,
                    **outcome,
                })
                try:
                    run_batch.append(
                        {
                            "issue": num,
                            "status": "retry",
                            "agent": task_result.get("agent"),
                            "score": None,
                            "trajectory": task_result.get("trajectory"),
                            "capability": task_result.get("capability"),
                            "teacher_evidence": task_result.get("teacher_evidence"),
                            **outcome,
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
                outcome = _outcome_fields(
                    status=task_result.get("status", "error"),
                    task_result=task_result,
                    error=err,
                    fallback_reason=crew_fallback_reason,
                    retry_attempt=attempts,
                )
                results.append({
                    "issue_number": num,
                    "title": issue["title"],
                    "domain": issue["domain"],
                    "difficulty": issue["difficulty"],
                    "status": task_result.get("status", "error"),
                    "error": err,
                    "capability": task_result.get("capability"),
                    "teacher_evidence": task_result.get("teacher_evidence"),
                    "crew_id": crew_result.crew_id if crew_result else None,
                    "crew_used": crew_used,
                    "crew_fallback_reason": crew_fallback_reason,
                    "teardown_ok": crew_result.teardown_ok if crew_result else None,
                    **outcome,
                })
                try:
                    run_batch.append(
                        {
                            "issue": num,
                            "status": task_result.get("status", "error"),
                            "agent": task_result.get("agent"),
                            "score": None,
                            "trajectory": task_result.get("trajectory"),
                            "capability": task_result.get("capability"),
                            "teacher_evidence": task_result.get("teacher_evidence"),
                            **outcome,
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

    # Flush the append-only journal once after all issue outcomes are queued.
    # Retry, processed, score, and crew checkpoints retain their existing
    # boundaries; only last_run.json is batched here. Preserve the legacy
    # non-fatal write behavior if the journal is temporarily unavailable.
    try:
        run_batch.flush()
    except Exception as e_rec:
        sys.stderr.write(
            f"[issue_bridge] Failed to flush run batch at {run_batch.path}: "
            f"{type(e_rec).__name__}: {e_rec}\n"
        )
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
