import dataclasses
from typing import Optional
from scoring import ScoreStore, GATES


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
            best = max(eligible, key=lambda a: store.get_score(a, domain))
            return RouteResult(
                domain=domain,
                difficulty=difficulty,
                chosen_agent=best,
                score=store.get_score(best, domain),
                eligible_count=len(eligible),
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
        )

    eligible = store.qualifying_agents(domain, difficulty)
    if not eligible:
        return RouteResult(
            domain=domain,
            difficulty=difficulty,
            blocked=True,
        )

    best = max(eligible, key=lambda a: store.get_score(a, domain))
    score = store.get_score(best, domain)
    return RouteResult(
        domain=domain,
        difficulty=difficulty,
        chosen_agent=best,
        score=score,
        eligible_count=len(eligible),
    )
