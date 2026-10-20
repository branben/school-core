"""ACRouter — outcome-feedback routing for OmniRoute combo selection.

Implements the "Agent as Router" pattern (arXiv:2606.22902) on top of the
school-core director/executor. The router is treated as an experience-gathering
agent: instead of a fixed ``COMBO_MAP`` (role -> model combo) that never
adapts, every routing decision records an *outcome* (success + quality), and
future selections consult that experience to favour the combo that actually
works for the current routing context (role + optional domain).

The selection strategy is an epsilon-greedy multi-armed bandit:

  * With probability ``exploration_rate`` we *explore* — pick a random known
    candidate combo, so the router keeps sampling alternatives and never
    freezes on a possibly-lucky early win.
  * Otherwise we *exploit* — pick the candidate with the best empirical value
    (success_rate blended with mean quality, with tie-breaking by trial count
    so under-sampled combos still get a look).

Until a routing context has any recorded experience, ``select_combo`` falls
back to the static ``default_resolver`` (the original COMBO_MAP behaviour), so
deployment is safe and the router warms up from existing config.

Experience is persisted to a JSON file so the router improves across runs.

Thread-safety note: callers are the single-threaded director/executor loop; no
locking is performed. The API is intentionally small and pure-Python.
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


# Blend weight: how much a successful-but-low-quality call is worth versus a
# successful high-quality one. value = success_rate * (1 - QUALITY_WEIGHT)
#                                       + mean_quality * QUALITY_WEIGHT
QUALITY_WEIGHT = 0.5

# Minimum trials before a candidate's value is trusted for exploitation. Below
# this, a candidate is treated as a promising exploratory option.
MIN_TRIALS_BEFORE_TRUST = 1


def combo_candidates_from(combo_map: Dict[str, str]) -> List[str]:
    """Derive the distinct set of model combos a router can choose between.

    Given the static ``COMBO_MAP`` (role -> combo), the full set of
    selectable combos is simply the set of its values (de-duplicated, order
    preserved by first appearance).
    """
    seen: List[str] = []
    for combo in combo_map.values():
        if combo not in seen:
            seen.append(combo)
    return seen


@dataclass
class _ComboStat:
    trials: int = 0
    successes: int = 0
    quality_sum: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.trials == 0:
            return 0.0
        return self.successes / self.trials

    @property
    def quality(self) -> float:
        if self.trials == 0:
            return 0.0
        return self.quality_sum / self.trials

    def value(self) -> float:
        """Blended empirical value in [0, 1] used for exploitation ranking."""
        return self.success_rate * (1 - QUALITY_WEIGHT) + self.quality * QUALITY_WEIGHT

    def to_dict(self) -> dict:
        return {
            "trials": self.trials,
            "successes": self.successes,
            "quality_sum": self.quality_sum,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_ComboStat":
        return cls(
            trials=d.get("trials", 0),
            successes=d.get("successes", 0),
            quality_sum=d.get("quality_sum", 0.0),
        )


class RouterExperience:
    """Epsilon-greedy bandit over OmniRoute combo selection.

    Args:
        candidates: The full set of combos eligible for routing. Any chosen
            combo outside this set is rejected at record time.
        default_resolver: Callable mapping a routing context (role name) to the
            static default combo (e.g. ``COMBO_MAP.get``). Used for the cold
            start / fallback path when no experience exists for a context.
        file_path: Where to persist experience. If None, runs in-memory only.
        exploration_rate: Epsilon in [0, 1]. 0 = pure exploitation, 1 = pure
            exploration.
    """

    def __init__(
        self,
        candidates: List[str],
        default_resolver: Callable[[str], Optional[str]],
        file_path: Optional[str] = None,
        exploration_rate: float = 0.15,
    ):
        if not (0.0 <= exploration_rate <= 1.0):
            raise ValueError(f"exploration_rate must be in [0, 1], got {exploration_rate}")
        self.candidates = list(candidates)
        self._candidate_set = set(self.candidates)
        self.default_resolver = default_resolver
        self.file_path = file_path
        self.exploration_rate = exploration_rate
        # stats[context][combo] = _ComboStat
        self.stats: Dict[str, Dict[str, _ComboStat]] = {}
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.file_path or not os.path.exists(self.file_path):
            self.stats = {}
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.stats = {
                ctx: {combo: _ComboStat.from_dict(d) for combo, d in combos.items()}
                for ctx, combos in raw.items()
            }
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file → start fresh rather than crash routing.
            self.stats = {}

    def _save(self) -> None:
        if not self.file_path:
            return
        raw = {
            ctx: {combo: stat.to_dict() for combo, stat in combos.items()}
            for ctx, combos in self.stats.items()
        }
        tmp = f"{self.file_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, self.file_path)

    # ── Outcome recording ───────────────────────────────────────────────────
    def record_outcome(
        self,
        context: str,
        combo: str,
        success: bool,
        quality: float = 1.0,
    ) -> None:
        """Record the outcome of routing *context* through *combo*.

        Args:
            context: Routing key (typically the role, optionally with domain,
                e.g. ``"coder"`` or ``"coder:python-testing"``).
            combo: The model combo actually used (must be a known candidate).
            success: Whether the call produced a usable result.
            quality: Outcome quality in [0, 1] (e.g. review score normalized,
                or 1.0/0.0 for a binary pass/fail).
        """
        if combo not in self._candidate_set:
            # Unknown combo — ignore so stats stay aligned with candidates.
            return
        quality = max(0.0, min(1.0, float(quality)))
        ctx_stats = self.stats.setdefault(context, {})
        stat = ctx_stats.get(combo, _ComboStat())
        stat.trials += 1
        stat.successes += 1 if success else 0
        stat.quality_sum += quality
        ctx_stats[combo] = stat
        self._save()

    # ── Selection ───────────────────────────────────────────────────────────
    def select_combo(self, context: str, rng: random.Random = None) -> Optional[str]:
        """Choose a combo for *context*.

        Returns the experience-best combo, or the static default when the
        context has no recorded experience yet. Falls back to the default
        resolver result even after experience if every candidate has zero
        trials (defensive).
        """
        rng = rng or random
        ctx_stats = self.stats.get(context)

        # Cold start: no experience for this context → static default.
        if not ctx_stats:
            return self.default_resolver(context)

        # Explore: random known candidate.
        if rng.random() < self.exploration_rate:
            return rng.choice(self.candidates)

        # Exploit: choose the candidate with the highest empirical value,
        # tie-broken by trial count (prefer more-sampled) then candidate order.
        best_combo: Optional[str] = None
        best_value = -math.inf
        best_trials = -1
        for combo in self.candidates:
            stat = ctx_stats.get(combo)
            if stat is None or stat.trials == 0:
                continue
            value = stat.value()
            # Newly-eligible tie-break: prefer more evidence.
            if (value > best_value) or (
                value == best_value and stat.trials > best_trials
            ):
                best_value = value
                best_trials = stat.trials
                best_combo = combo

        if best_combo is None:
            # All candidates unrecorded in this context (shouldn't happen given
            # the cold-start guard, but defensive) → static default.
            return self.default_resolver(context)
        return best_combo

    # ── Introspection ────────────────────────────────────────────────────────
    def summary(self, context: str) -> List[dict]:
        """Return per-combo stats for *context*, sorted by value desc."""
        ctx_stats = self.stats.get(context, {})
        rows = []
        for combo in self.candidates:
            stat = ctx_stats.get(combo)
            if stat is None or stat.trials == 0:
                continue
            rows.append(
                {
                    "combo": combo,
                    "trials": stat.trials,
                    "success_rate": stat.success_rate,
                    "quality": stat.quality,
                    "value": stat.value(),
                }
            )
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows
