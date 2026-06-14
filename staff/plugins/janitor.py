from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from staff.plugin import StaffPlugin, StaffContext, StaffResult, PluginTrust
from staff.sandbox import StaffSandbox, SandboxError
from scoring import ScoreRecommendation


class JanitorPlugin(StaffPlugin):
    """Memory/vault hygiene — prunes stale trajectories, archives outdated notes."""

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._max_age_days = self._config.get("max_age_days", 30)

    @property
    def name(self) -> str:
        return "janitor"

    @property
    def trust(self) -> PluginTrust:
        explicit = self._config.get("trust")
        if explicit:
            return PluginTrust(explicit)
        return PluginTrust.COMMUNITY

    def health_check(self) -> dict:
        return {
            "trajectory_read": "available",
            "trajectory_prune": "available",
            "vault_search": "degraded",
        }

    def run(self, sandbox: StaffSandbox, context: StaffContext) -> StaffResult:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._max_age_days)
        pruned = 0
        scanned = 0
        recommendations = []

        traj_dir = Path("data") / "trajectories"
        if traj_dir.exists():
            for f in traj_dir.glob("*.json"):
                try:
                    with open(f) as fh:
                        t = json.load(fh)
                    scanned += 1
                    ts_str = t.get("timestamp", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        task_score = t.get("task_score")
                        if ts < cutoff and task_score is not None and task_score < 30:
                            f.unlink()
                            pruned += 1
                except (json.JSONDecodeError, OSError, ValueError):
                    continue

        if scanned > 0:
            agent = self._config.get("target_agent", "smollm2:1.7b")
            recommendations.append(ScoreRecommendation(
                agent=agent,
                domain="_default",
                suggested_delta=-2.0,
                reason=f"Janitor: pruned {pruned} stale trajectories (scanned {scanned})",
                source_plugin=self.name,
            ))

        return StaffResult(
            plugin_name=self.name,
            status="success",
            summary=f"Scanned {scanned} trajectories, pruned {pruned} stale ones",
            score_recommendations=recommendations,
            vault_writes=[],
            metrics={"scanned": scanned, "pruned": pruned},
        )
