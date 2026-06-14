import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional


GROWTH_DIR = Path(__file__).parent / "data" / "growth"


@dataclass
class CapabilityProfile:
    agent: str
    strengths: List[str]
    weaknesses: List[str]
    growth_rate: float
    difficulty_progression: List[float]
    insufficient_data: bool = False


@dataclass
class GrowthReport:
    agent: str
    period: str
    tasks_attempted: int
    tasks_succeeded: int
    avg_difficulty: float
    score_trend: float
    is_better_than_previous: Optional[bool]


def _agent_path(agent: str) -> Path:
    return GROWTH_DIR / f"{agent}.json"


def _load_agent(path: Path) -> list:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _save_agent(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def calculate_difficulty_weight(files_touched: int, test_failures: int, complexity: int) -> float:
    return 1.0 + 0.5 * math.log2(files_touched + 1) + 0.3 * test_failures + 0.2 * complexity


def _linear_regression_slope(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    num = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


class GrowthTracker:
    def __init__(self, data_dir: str = None):
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = GROWTH_DIR

    def _agent_path(self, agent: str) -> Path:
        return self.data_dir / f"{agent}.json"

    def _load(self, agent: str) -> list:
        p = self._agent_path(agent)
        if not p.exists():
            return []
        return json.loads(p.read_text())

    def _save(self, agent: str, data: list) -> None:
        p = self._agent_path(agent)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))

    def record_performance(
        self,
        agent: str,
        domain: str,
        score: float,
        difficulty_weight: float,
        timestamp: str = None,
    ) -> None:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        data = self._load(agent)
        entry = {
            "domain": domain,
            "score": float(score),
            "difficulty_weight": float(difficulty_weight),
            "timestamp": timestamp,
        }
        data.append(entry)
        self._save(agent, data)

    def get_capability_profile(self, agent: str) -> CapabilityProfile:
        data = self._load(agent)
        if not data:
            return CapabilityProfile(
                agent=agent,
                strengths=[],
                weaknesses=[],
                growth_rate=0.0,
                difficulty_progression=[],
                insufficient_data=True,
            )

        by_domain: Dict[str, list] = {}
        for entry in data:
            d = entry["domain"]
            by_domain.setdefault(d, []).append(entry)

        strengths = []
        weaknesses = []
        for dom, entries in by_domain.items():
            scores = [e["score"] for e in entries]
            avg = sum(scores) / len(scores)
            if avg > 60:
                strengths.append(dom)
            elif avg < 40:
                weaknesses.append(dom)

        strengths.sort()
        weaknesses.sort()

        sorted_data = sorted(data, key=lambda e: e["timestamp"])
        dw_scores = [e["score"] * e["difficulty_weight"] for e in sorted_data]
        growth_rate = _linear_regression_slope(dw_scores)

        difficulty_progression = [e["difficulty_weight"] for e in sorted_data]

        return CapabilityProfile(
            agent=agent,
            strengths=strengths,
            weaknesses=weaknesses,
            growth_rate=round(growth_rate, 4),
            difficulty_progression=difficulty_progression,
            insufficient_data=False,
        )

    def query_growth(self, agent: str, days: int = 7) -> GrowthReport:
        data = self._load(agent)
        if not data:
            return GrowthReport(
                agent=agent,
                period=f"last {days} days",
                tasks_attempted=0,
                tasks_succeeded=0,
                avg_difficulty=0.0,
                score_trend=0.0,
                is_better_than_previous=None,
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = []
        for entry in data:
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts >= cutoff:
                recent.append(entry)

        if not recent:
            return GrowthReport(
                agent=agent,
                period=f"last {days} days",
                tasks_attempted=0,
                tasks_succeeded=0,
                avg_difficulty=0.0,
                score_trend=0.0,
                is_better_than_previous=None,
            )

        tasks_attempted = len(recent)
        tasks_succeeded = sum(1 for e in recent if e["score"] >= 50)
        avg_difficulty = sum(e["difficulty_weight"] for e in recent) / len(recent)

        sorted_recent = sorted(recent, key=lambda e: e["timestamp"])
        dw_scores = [e["score"] * e["difficulty_weight"] for e in sorted_recent]
        score_trend = _linear_regression_slope(dw_scores)

        previous_cutoff = cutoff - timedelta(days=days)
        previous = []
        for entry in data:
            ts = datetime.fromisoformat(entry["timestamp"])
            if previous_cutoff <= ts < cutoff:
                previous.append(entry)

        is_better = None
        if previous and recent:
            prev_avg = sum(e["score"] for e in previous) / len(previous)
            curr_avg = sum(e["score"] for e in recent) / len(recent)
            is_better = curr_avg > prev_avg

        return GrowthReport(
            agent=agent,
            period=f"last {days} days",
            tasks_attempted=tasks_attempted,
            tasks_succeeded=tasks_succeeded,
            avg_difficulty=round(avg_difficulty, 2),
            score_trend=round(score_trend, 4),
            is_better_than_previous=is_better,
        )
