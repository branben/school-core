"""Pure admission policy for bounded school-loop crew dispatch."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str
    effective_cap: int
    remaining_seconds: float


def decide_admission(
    *,
    dispatched: int,
    configured_cap: int,
    runner_slots: int,
    active_claims: int,
    remaining_seconds: float,
    crew_timeout_seconds: float,
    retry_pressure: int,
    retry_pressure_limit: int = 2,
    reserve_seconds: float = 30.0,
) -> AdmissionDecision:
    """Decide whether one more crew may start without unsafe overcommitment."""
    effective_cap = max(0, min(int(configured_cap), int(runner_slots)))
    remaining = max(0.0, float(remaining_seconds))
    if effective_cap <= 0 or int(dispatched) >= effective_cap:
        return AdmissionDecision(False, "crew_cap_reached", effective_cap, remaining)
    if int(active_claims) >= max(1, int(runner_slots)):
        return AdmissionDecision(False, "crew_in_flight", effective_cap, remaining)
    if remaining < float(crew_timeout_seconds) + float(reserve_seconds):
        return AdmissionDecision(False, "insufficient_cycle_time", effective_cap, remaining)
    if int(retry_pressure) >= max(1, int(retry_pressure_limit)):
        return AdmissionDecision(False, "retry_pressure", effective_cap, remaining)
    return AdmissionDecision(True, "admitted", effective_cap, remaining)
