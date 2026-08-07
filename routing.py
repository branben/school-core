import dataclasses
from typing import Optional
from scoring import ScoreStore, GATES, COST_TIERS, COST_PENALTY_BY_DIFFICULTY


@dataclasses.dataclass
class RouteResult:
    domain: str
    difficulty: str
    chosen_agent: Optional[str] = None
    score: Optional[float] = None
    eligible_count: int = 0
    blocked: bool = False
    escalation: bool = False
    gate_crossed: Optional[str] = None
    cost_tier: Optional[int] = None  # Cost tier of chosen agent (U2)


def route_task(
    store: ScoreStore,
    domain: str,
    difficulty: str,
    force_agent: Optional[str] = None,
) -> RouteResult:
    difficulty = difficulty.lower()

    # blocker is a special escalation case, not a gate level
    if difficulty == "blocker":
        eligible = store.qualifying_agents(domain, "diploma")
        if eligible:
            best = _best_cost_aware(eligible, store, domain, difficulty)
            return RouteResult(
                domain=domain,
                difficulty=difficulty,
                chosen_agent=best,
                score=store.get_score(best, domain),
                eligible_count=len(eligible),
                cost_tier=COST_TIERS.get(best, 4),
            )
        return RouteResult(
            domain=domain,
            difficulty=difficulty,
            escalation=True,
        )

    if difficulty not in GATES:
        raise ValueError(f"Invalid difficulty '{difficulty}'. Valid: easy, medium, hard, blocker")

    if force_agent:
        if force_agent not in store.list_agents():
            raise ValueError(f"Unknown agent '{force_agent}'")
        chosen = force_agent
        score = store.get_score(chosen, domain)
        return RouteResult(
            domain=domain,
            difficulty=difficulty,
            chosen_agent=chosen,
            score=score,
            eligible_count=1,
            cost_tier=COST_TIERS.get(chosen, 4),
        )

    eligible = store.qualifying_agents(domain, difficulty)
    if not eligible:
        return RouteResult(
            domain=domain,
            difficulty=difficulty,
            blocked=True,
        )

    # Cost-aware selection (U2): prefer cheaper models when scores are close
    best = _best_cost_aware(eligible, store, domain, difficulty)
    score = store.get_score(best, domain)
    return RouteResult(
        domain=domain,
        difficulty=difficulty,
        chosen_agent=best,
        score=score,
        eligible_count=len(eligible),
        cost_tier=COST_TIERS.get(best, 4),
    )


def _best_cost_aware(
    eligible: list[str],
    store: ScoreStore,
    domain: str,
    difficulty: str,
) -> str:
    """Select the best agent from *eligible* using cost-aware scoring.

    Applies a cost penalty that scales with both the model's cost tier and
    the task difficulty. Easy tasks get a large penalty on expensive models,
    hard tasks get none (quality over cost).

    Selection score = base_score - (cost_tier * difficulty_penalty)
    """
    penalty = COST_PENALTY_BY_DIFFICULTY.get(difficulty, 0)

    def _selection_key(agent: str) -> tuple:
        """Compute (score_after_penalty, -tier) for tiebreaking.

        Primary sort: higher score_after_penalty wins.
        Tiebreaker: on equal scores, prefer cheaper model (lower tier).
        """
        base = store.get_score(agent, domain)
        tier = COST_TIERS.get(agent, 4)  # default to most expensive tier
        score_after_penalty = base - (tier * penalty)
        return (score_after_penalty, -tier)  # -tier so lower tier wins ties

    return max(eligible, key=_selection_key)
