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
    """Decide whether one more crew may start without unsafe overcommitment.

    N3.1 (worst-day-ever): the time reservation scales with the effective cap,
    not just one crew. Admitting cap=2 crews each polling up to
    ``crew_timeout_seconds`` must reserve ``cap * timeout + reserve`` so the
    cycle budget (30-min CI job) can't be blown by N long crews plus grading.
    """
    effective_cap = max(0, min(int(configured_cap), int(runner_slots)))
    remaining = max(0.0, float(remaining_seconds))
    if effective_cap <= 0 or int(dispatched) >= effective_cap:
        return AdmissionDecision(False, "crew_cap_reached", effective_cap, remaining)
    if int(active_claims) >= max(1, int(runner_slots)):
        return AdmissionDecision(False, "crew_in_flight", effective_cap, remaining)
    # Reserve cap timeouts (worst case: every admitted crew runs to its full
    # poll budget) plus a grading reserve, so the job can't be killed mid-grade.
    required_seconds = float(crew_timeout_seconds) * effective_cap + float(reserve_seconds)
    if remaining < required_seconds:
        return AdmissionDecision(False, "insufficient_cycle_time", effective_cap, remaining)
    if int(retry_pressure) >= max(1, int(retry_pressure_limit)):
        return AdmissionDecision(False, "retry_pressure", effective_cap, remaining)
    return AdmissionDecision(True, "admitted", effective_cap, remaining)
