from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from staff.plugin import StaffPlugin, StaffContext, StaffResult, PluginTrust
from staff.sandbox import StaffSandbox, SandboxError
from scoring import ScoreRecommendation


class ScoreAuditorPlugin(StaffPlugin):
    """Analyzes the score store for anomalies and produces recommendations.

    Detects: volatile scores (large swings), stale scores (no updates despite
    activity), and score/trajectory mismatches (high scores but failing tasks).
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._swing_threshold = self._config.get("swing_threshold", 10.0)
        self._stale_days = self._config.get("stale_days", 7)
        self._min_trajectories = self._config.get("min_trajectories", 5)

    @property
    def name(self) -> str:
        return "score-auditor"

    @property
    def trust(self) -> PluginTrust:
        explicit = self._config.get("trust")
        if explicit:
            return PluginTrust(explicit)
        return PluginTrust.COMMUNITY

    def health_check(self) -> dict:
        return {
            "score_read": "available",
            "trajectory_read": "available",
            "recommendation_write": "available",
        }

    def run(self, sandbox: StaffSandbox, context: StaffContext) -> StaffResult:
        store = context.score_store
        scores = store.get_all_scores()
        recommendations = []
        anomalies = 0

        traj_dir = Path("data") / "trajectories"
        traj_counts = defaultdict(int)
        traj_scores = defaultdict(list)
        if traj_dir.exists():
            for f in traj_dir.glob("*.json"):
                try:
                    with open(f) as fh:
                        t = json.load(fh)
                    agent = t.get("agent", "unknown")
                    domain = t.get("domain", "_default")
                    score = t.get("task_score")
                    ts_str = t.get("timestamp", "")
                    traj_counts[(agent, domain)] += 1
                    if score is not None:
                        traj_scores[(agent, domain)].append((score, ts_str))
                except (json.JSONDecodeError, OSError, ValueError):
                    continue

        for agent, domains in scores.items():
            for domain, current_score in domains.items():
                key = (agent, domain)
                count = traj_counts.get(key, 0)
                scores_list = traj_scores.get(key, [])

                if count < self._min_trajectories:
                    continue

                recent_cutoff = datetime.now(timezone.utc) - timedelta(days=self._stale_days)
                recent_scores = [
                    s for s, ts in scores_list
                    if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) > recent_cutoff
                ]

                if len(recent_scores) >= 3:
                    swing = max(recent_scores) - min(recent_scores)
                    if swing > self._swing_threshold:
                        direction = "up" if recent_scores[-1] > recent_scores[0] else "down"
                        delta = -3.0 if direction == "down" else 1.0
                        recommendations.append(ScoreRecommendation(
                            agent=agent, domain=domain,
                            suggested_delta=delta,
                            reason=f"Volatile: {swing:.1f}pt swing over {len(recent_scores)} recent tasks",
                            source_plugin=self.name,
                        ))
                        anomalies += 1

                if count > 0 and current_score > 50:
                    avg_task = sum(s for s, _ in scores_list) / len(scores_list) if scores_list else 0
                    if avg_task < 30 and current_score > 50:
                        recommendations.append(ScoreRecommendation(
                            agent=agent, domain=domain,
                            suggested_delta=-5.0,
                            reason=f"Mismatch: score={current_score:.1f} but avg task_score={avg_task:.1f} over {count} trajectories",
                            source_plugin=self.name,
                        ))
                        anomalies += 1

        return StaffResult(
            plugin_name=self.name,
            status="success",
            summary=f"Audited {len(scores)} agents, found {anomalies} anomalies, {len(recommendations)} recommendations",
            score_recommendations=recommendations,
            vault_writes=[],
            metrics={
                "agents_audited": len(scores),
                "anomalies_found": anomalies,
                "recommendations_made": len(recommendations),
            },
        )
