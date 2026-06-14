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


class AdversarialReviewerPlugin(StaffPlugin):
    """Reviews recent high-scoring trajectories for potential score inflation.

    Compares trajectory-level task scores against the agent's stored EMA score.
    Flags cases where the EMA score is high but recent trajectories show
    consistently low execution quality — a sign of score inflation or
    adversarial sycophancy in the verification pipeline.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._inflation_threshold = self._config.get("inflation_threshold", 20.0)
        self._min_trajectories = self._config.get("min_trajectories", 3)
        self._lookback_days = self._config.get("lookback_days", 14)

    @property
    def name(self) -> str:
        return "adversarial-reviewer"

    @property
    def trust(self) -> PluginTrust:
        explicit = self._config.get("trust")
        if explicit:
            return PluginTrust(explicit)
        return PluginTrust.VERIFIED

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
        inflation_flags = 0

        traj_dir = Path("data") / "trajectories"
        traj_scores = defaultdict(list)
        if traj_dir.exists():
            for f in traj_dir.glob("*.json"):
                try:
                    with open(f) as fh:
                        t = json.load(fh)
                    agent = t.get("agent", "unknown")
                    domain = t.get("domain", "_default")
                    task_score = t.get("task_score")
                    ts_str = t.get("timestamp", "")
                    if task_score is not None:
                        traj_scores[(agent, domain)].append((task_score, ts_str))
                except (json.JSONDecodeError, OSError, ValueError):
                    continue

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._lookback_days)

        for agent, domains in scores.items():
            for domain, current_score in domains.items():
                key = (agent, domain)
                entries = traj_scores.get(key, [])

                recent = [
                    s for s, ts in entries
                    if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff
                ]

                if len(recent) < self._min_trajectories:
                    continue

                avg_recent = sum(recent) / len(recent)
                gap = current_score - avg_recent

                if gap > self._inflation_threshold:
                    recommendations.append(ScoreRecommendation(
                        agent=agent,
                        domain=domain,
                        suggested_delta=-min(gap * 0.5, 10.0),
                        reason=(
                            f"Score inflation: EMA={current_score:.1f} but "
                            f"avg recent task_score={avg_recent:.1f} over "
                            f"{len(recent)} trajectories (gap={gap:.1f})"
                        ),
                        source_plugin=self.name,
                    ))
                    inflation_flags += 1

                adversarial_scores = [
                    s for s, ts in entries
                    if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff
                ]
                if adversarial_scores:
                    low_count = sum(1 for s in adversarial_scores if s < 40)
                    if low_count >= 2 and current_score > 50:
                        recommendations.append(ScoreRecommendation(
                            agent=agent,
                            domain=domain,
                            suggested_delta=-8.0,
                            reason=(
                                f"Trajectory mismatch: {low_count} low-scoring "
                                f"(<40) recent tasks but EMA={current_score:.1f}"
                            ),
                            source_plugin=self.name,
                        ))
                        inflation_flags += 1

        return StaffResult(
            plugin_name=self.name,
            status="success",
            summary=(
                f"Reviewed {len(scores)} agents, found {inflation_flags} "
                f"inflation flags, {len(recommendations)} recommendations"
            ),
            score_recommendations=recommendations,
            vault_writes=[],
            metrics={
                "agents_reviewed": len(scores),
                "inflation_flags": inflation_flags,
                "recommendations_made": len(recommendations),
            },
        )
