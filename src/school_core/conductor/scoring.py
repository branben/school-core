"""conductor/scoring.py — Score aggregation and persistence.

Handles task-score computation, round scoring/printing, leaderboard display,
and batch scoring of reviewed bookbags.
"""

from __future__ import annotations

from bookbag import read_bookbag
from director import evaluate_and_update
from scoring import ScoreStore


def _compute_task_score(bag: dict) -> float:
    """Compute a task score from bookbag verdict + scores.

    Uses the same formula as the async loop: accepted → max(60, combined),
    rejected → min(40, combined). Shared by _run_async_loop() and
    _score_reviewed_bookbags().
    """
    cto_score = bag.get("cto_score", 0)
    coo_score = bag.get("coo_score", 0)
    combined = (cto_score + coo_score) / 2.0
    accepted = bag.get("accepted", bag.get("cto_verdict") == "PASS" and bag.get("coo_verdict") == "PASS")
    return max(60, combined) if accepted else min(40, combined)


def _score_reviewed_bookbags(bookbags: list[dict], store: ScoreStore) -> None:
    """Score all reviewed bookbags via evaluate_and_update."""
    print(f"\U0001f4ca Scoring {len(bookbags)} reviewed bookbags...")

    scored = 0
    for bag in bookbags:
        bead = bag.get("bead", "unknown")
        student = bag.get("student", "unknown")
        domain = bag.get("domain", "unknown")
        cto_v = bag.get("cto_verdict", "?")
        coo_v = bag.get("coo_verdict", "?")
        task_score = _compute_task_score(bag)

        result = {
            "status": "success",
            "agent": student,
            "domain": domain,
            "review": {
                "cto_verdict": cto_v,
                "coo_verdict": coo_v,
                "cto_score": bag.get("cto_score", 0),
                "coo_score": bag.get("coo_score", 0),
                "findings": bag.get("findings", []),
                "accepted": bag.get("accepted", False),
            },
            "task_score": task_score,
        }

        updated = evaluate_and_update(result, task_score, store=store)
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f393 {crossed}!" if crossed else ""
        scored += 1
        print(f"  {scored}/{len(bookbags)} {bead[:30]} [{student}/{domain}] "
              f"{old_s:.1f}→{new_s:.1f}{gate_msg}")

    print(f"  ✅ Scored: {scored}")
    print()


def _score_and_print_round(result: dict, store: ScoreStore) -> None:
    """Score a single round's result and print it (used by sync loop)."""
    task_score = result.get("task_score", 0)
    updated = evaluate_and_update(result, task_score, store=store)

    status = result.get("status", "error")
    if status == "success":
        review = result.get("review", {})
        cto = review.get("cto_verdict", "?")
        coo = review.get("coo_verdict", "?")
        accepted = review.get("accepted", False)
        findings = len(review.get("findings", []))
        old_s = updated.get("old_score", 0)
        new_s = updated.get("new_score", 0)
        crossed = updated.get("gate_crossed", "")
        gate_msg = f" \U0001f393 {crossed}!" if crossed else ""
        print(f"  ✅ CTO={cto} COO={coo} | {'Accepted' if accepted else 'Rejected'} | "
              f"Score: {old_s:.1f}→{new_s:.1f}{gate_msg} | {findings} findings")
    else:
        error = result.get("error", result.get("status", "unknown"))
        print(f"  ❌ {status}: {error}")


def _print_leaderboard(store: ScoreStore) -> None:
    """Print the final leaderboard."""
    print("=" * 60)
    print("\U0001f4ca Final Leaderboard:")
    for rank, (agent, score) in enumerate(store.leaderboard("_default"), 1):
        gate = store.gate_for_score(score)
        print(f"  {rank}. {agent:12s} {score:6.1f} ({gate})")
    print()
    print(f"\U0001f4a1 To clean up: python conductor.py --clean-worktrees")
