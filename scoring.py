import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

from execution_scorer import ExecutionScorer
from heuristic_scorer import HeuristicScorer

SEED_AGENTS = {
    # Cloud models (via OmniRoute)
    "gemini-3-flash-preview": {"_default": 30},
    "gemma-4-31b-it:free": {"_default": 30},
    "owl-alpha": {"_default": 25, "agentic-coding": 30},
    "gemini-2.0-flash": {"_default": 25},
    "kimi-k2.6:free": {"_default": 20},
    "always-on-max": {"_default": 35},
    "always-on-free": {"_default": 20},
    "north-coding": {"_default": 30, "python-coding": 35},
    # Local models (via Foundry Local — GPU-accelerated)
    "foundry-coder-0.5b": {"_default": 15},
    "foundry-coder-1.5b": {"_default": 20},
    "foundry-coder-7b": {"_default": 25},
    "foundry-smollm3-3b": {"_default": 10},
    "foundry-phi4": {"_default": 10},
}

# Repo namespace for the score store. When the school runs against a single
# repo (no per-repo teacher pairs), scores stay in the legacy global file.
# When multi-repo dispatch is enabled, each repo gets its OWN scores file so a
# role's learned capacity on one repo does not leak/pollute another repo.
REPO_GLOBAL = "__global__"


def _scores_file_for(repo: str) -> Path:
    """Resolve the per-repo scores.json path.

    Global (default) → ``data/scores.json`` (legacy filename, preserved).
    Per-repo         → ``data/scores-<safe_repo>.json`` (filesystem-safe).
    """
    base = Path(__file__).parent / "data"
    if repo == REPO_GLOBAL:
        return base / "scores.json"
    safe = repo.replace("/", "__")
    return base / f"scores-{safe}.json"


GATES = {"easy": 0, "medium": 25, "hard": 50, "diploma": 75}

MAX_DELTA = 15.0
HUMAN_CONFIRM_THRESHOLD = 10.0


@dataclass
class GroundedScore:
    """Combined grounded score from three tiers."""
    execution_score: Optional[float]
    heuristic_score: float
    llm_score: Optional[float]
    combined: float
    details: Dict[str, Any] = field(default_factory=dict)


class GroundedScoreCalculator:
    """Combines Tier 1 (execution) + Tier 2 (heuristic) + Tier 3 (LLM) scoring.

    Combined formula: (exec or 0.5) * 0.5 + heuristic * 0.3 + (llm or 0.5) * 0.2
    Missing tiers default to 0.5 (neutral) so they don't skew results.
    """

    def __init__(self):
        self._execution_scorer = ExecutionScorer()
        self._heuristic_scorer = HeuristicScorer()

    def calculate(
        self,
        output: str,
        codebase_context: str = "",
        llm_score: Optional[float] = None,
    ) -> GroundedScore:
        """Calculate the combined grounded score."""
        exec_score = self._execution_scorer.score(output, codebase_context)
        heur_score = self._heuristic_scorer.score(output, codebase_context)

        exec_normalized = (exec_score / 100.0) if exec_score is not None else 0.5
        llm_normalized = (llm_score / 100.0) if llm_score is not None else 0.5

        combined = (
            exec_normalized * 0.5
            + (heur_score / 100.0) * 0.3
            + llm_normalized * 0.2
        ) * 100.0

        return GroundedScore(
            execution_score=exec_score,
            heuristic_score=heur_score,
            llm_score=llm_score,
            combined=round(combined, 2),
            details={
                "exec_weight": 0.5,
                "heuristic_weight": 0.3,
                "llm_weight": 0.2,
                "exec_used": exec_score is not None,
                "llm_used": llm_score is not None,
            },
        )


@dataclass
class ScoreRecommendation:
    agent: str
    domain: str
    suggested_delta: float
    reason: str
    source_plugin: str

class ScoreStore:
    def __init__(self, file_path: str = None, repo: str = "__global__"):
        if file_path is not None:
            self.file_path = Path(file_path)
        else:
            self.file_path = _scores_file_for(repo)
        self.repo = repo
        self.scores: Dict[str, Dict[str, float]] = {}
        self._audit_log: list = []
        self._difficulty_weights: Dict[str, Dict[str, float]] = {}
        self.load()

    def load(self) -> None:
        if not self.file_path.exists():
            # seed and save
            self.scores = {agent: {domain: float(val) for domain, val in domains.items()} for agent, domains in SEED_AGENTS.items()}
            self.save()
        else:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                self.scores = {}
                self._difficulty_weights = {}
                for agent, domains in data.items():
                    self.scores[agent] = {}
                    self._difficulty_weights[agent] = {}
                    for key, val in domains.items():
                        if key.startswith("_difficulty_"):
                            domain_key = key[len("_difficulty_"):]
                            self._difficulty_weights[agent][domain_key] = float(val)
                        else:
                            self.scores[agent][key] = float(val)

    def save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        merged = {}
        for agent, domains in self.scores.items():
            merged[agent] = dict(domains)
            if agent in self._difficulty_weights:
                for domain, weight in self._difficulty_weights[agent].items():
                    merged[agent][f"_difficulty_{domain}"] = weight
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(merged, f, indent=4, sort_keys=True)

    def get_score(self, agent_name: str, domain: str) -> float:
        return self.scores.get(agent_name, {}).get(domain, self.scores.get(agent_name, {}).get("_default", 0.0))

    def set_score(self, agent_name: str, domain: str, value: float) -> None:
        if agent_name not in self.scores:
            self.scores[agent_name] = {}
        self.scores[agent_name][domain] = max(0.0, min(100.0, float(value)))
        self.save()

    # EMA score update — new score = 70% old + 30% task
    def update_score(self, agent_name: str, domain: str, task_score: float, difficulty_weight: float = 1.0) -> float:
        old = self.get_score(agent_name, domain)
        new = old * 0.7 + task_score * 0.3
        new = max(0.0, min(100.0, new))
        self.set_score(agent_name, domain, new)
        self.set_difficulty_weight(agent_name, domain, difficulty_weight)
        return new

    def set_difficulty_weight(self, agent_name: str, domain: str, weight: float) -> None:
        if agent_name not in self._difficulty_weights:
            self._difficulty_weights[agent_name] = {}
        self._difficulty_weights[agent_name][domain] = max(0.0, min(2.0, float(weight)))
        self.save()

    def get_difficulty_weight(self, agent_name: str, domain: str) -> float:
        return self._difficulty_weights.get(agent_name, {}).get(domain, 1.0)

    def add_agent(self, agent_name: str, initial_scores: Dict[str, float] = None) -> None:
        if agent_name in self.scores:
            return
        self.scores[agent_name] = {}
        if initial_scores:
            for d, v in initial_scores.items():
                self.scores[agent_name][d] = max(0.0, min(100.0, float(v)))
        self.save()

    def list_agents(self) -> List[str]:
        return list(self.scores.keys())

    def get_all_scores(self) -> Dict[str, Dict[str, float]]:
        return self.scores

    def leaderboard(self, domain: str = "_default") -> List[tuple]:
        lst = [(agent, self.get_score(agent, domain)) for agent in self.list_agents()]
        lst.sort(key=lambda x: x[1], reverse=True)
        return lst

    def gate_for_score(self, score: float) -> str:
        # return highest gate name the score qualifies for
        qualified = [name for name, thr in GATES.items() if score >= thr]
        return max(qualified, key=lambda n: GATES[n]) if qualified else "easy"

    def qualifying_agents(self, domain: str, gate_name: str) -> List[str]:
        thr = GATES.get(gate_name, 0)
        return [agent for agent in self.list_agents() if self.get_score(agent, domain) >= thr]

    def domains(self) -> List[str]:
        doms = set()
        for scores in self.scores.values():
            doms.update(scores.keys())
        return list(doms)

    def apply_recommendation(self, rec: ScoreRecommendation) -> Optional[float]:
        if abs(rec.suggested_delta) > MAX_DELTA:
            raise ValueError(f"Delta {rec.suggested_delta} exceeds max {MAX_DELTA}")
        if abs(rec.suggested_delta) > HUMAN_CONFIRM_THRESHOLD:
            self._queue_for_confirmation(rec)
            return None
        old = self.get_score(rec.agent, rec.domain)
        new = max(0.0, min(100.0, old + rec.suggested_delta))
        self.set_score(rec.agent, rec.domain, new)
        self._audit_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "plugin": rec.source_plugin,
            "agent": rec.agent,
            "domain": rec.domain,
            "old": old,
            "new": new,
            "reason": rec.reason,
        })
        return new

    def _queue_for_confirmation(self, rec: ScoreRecommendation):
        pass
