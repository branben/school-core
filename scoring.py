import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

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

GATES = {"easy": 0, "medium": 25, "hard": 50, "diploma": 75}

MAX_DELTA = 15.0
HUMAN_CONFIRM_THRESHOLD = 10.0


@dataclass
class ScoreRecommendation:
    agent: str
    domain: str
    suggested_delta: float
    reason: str
    source_plugin: str

class ScoreStore:
    def __init__(self, file_path: str = None):
        self.file_path = Path(file_path) if file_path else Path(__file__).parent / "data" / "scores.json"
        self.scores: Dict[str, Dict[str, float]] = {}
        self._audit_log: list = []
        self.load()

    def load(self) -> None:
        if not self.file_path.exists():
            # seed and save
            self.scores = {agent: {domain: float(val) for domain, val in domains.items()} for agent, domains in SEED_AGENTS.items()}
            self.save()
        else:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # ensure numeric values
                self.scores = {agent: {domain: float(val) for domain, val in domains.items()} for agent, domains in data.items()}

    def save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(self.scores, f, indent=4, sort_keys=True)

    def get_score(self, agent_name: str, domain: str) -> float:
        return self.scores.get(agent_name, {}).get(domain, self.scores.get(agent_name, {}).get("_default", 0.0))

    def set_score(self, agent_name: str, domain: str, value: float) -> None:
        if agent_name not in self.scores:
            self.scores[agent_name] = {}
        self.scores[agent_name][domain] = max(0.0, min(100.0, float(value)))
        self.save()

    # EMA score update — new score = 70% old + 30% task
    def update_score(self, agent_name: str, domain: str, task_score: float) -> float:
        old = self.get_score(agent_name, domain)
        new = old * 0.7 + task_score * 0.3
        new = max(0.0, min(100.0, new))
        self.set_score(agent_name, domain, new)
        return new

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
