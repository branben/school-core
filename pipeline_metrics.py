"""Bounded, redacted measurements for one school-loop issue.

This module deliberately records only aggregate numbers. It is safe to attach a
snapshot to ``last_run.json``: prompts, model responses, reports, tokens, and
filesystem paths never enter the packet.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional


SCHEMA_VERSION = 1
_DEFAULT_MAX_EVENTS = 64
_MAX_NAME_CHARS = 32


def _name(value: object, fallback: str = "other") -> str:
    """Return a small metric label without accepting arbitrary payload text."""
    text = str(value or fallback).strip().lower()
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in text)
    return (safe or fallback)[:_MAX_NAME_CHARS]


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_float(value: object) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return 0.0


class PipelineMetrics:
    """Collect bounded aggregate timings and invocation counts for one issue.

    The object is intentionally dependency-free and mutable so callers can
    thread it through optional instrumentation seams without changing verdict
    or result contracts.
    """

    def __init__(self, max_events: int = _DEFAULT_MAX_EVENTS) -> None:
        self._lock = threading.RLock()
        self._started = time.perf_counter()
        self._max_events = max(1, _nonnegative_int(max_events) or _DEFAULT_MAX_EVENTS)
        self._event_count = 0
        self._timings: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._model = {
            "call_count": 0,
            "retry_count": 0,
            "prompt_chars": 0,
            "output_chars": 0,
            "roles": {},
        }
        self._verification = {
            "gate_invocations": 0,
            "shell_starts": 0,
            "commands": 0,
            "copied_bytes": 0,
        }
        self._context: dict[str, dict[str, int]] = {}
        self._crew = {
            "spawn_count": 0,
            "poll_count": 0,
            "blocked_wait_count": 0,
            "fallback_count": 0,
            "teardown_count": 0,
            "in_flight_count": 0,
        }
        self._quality = {
            "accepted": None,
            "critical_findings": 0,
            "retry_count": 0,
        }

    def _event(self) -> None:
        with self._lock:
            if self._event_count < self._max_events:
                self._event_count += 1

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Measure a named stage in milliseconds, including failed stages."""
        label = _name(name)
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.record_stage_duration(label, elapsed)

    def record_stage_duration(self, name: str, duration_ms: float) -> None:
        """Add an externally measured stage duration."""
        label = _name(name)
        with self._lock:
            self._timings[label] = round(
                _nonnegative_float(self._timings.get(label)) + _nonnegative_float(duration_ms), 3
            )
            self._event()

    def ensure_stage(self, name: str) -> None:
        """Declare a stage even when an optional path did not run."""
        with self._lock:
            self._timings.setdefault(_name(name), 0.0)

    def record_call(self, name: str, count: int = 1) -> None:
        """Record a bounded pipeline seam invocation by label."""
        label = _name(name)
        with self._lock:
            self._calls[label] = _nonnegative_int(self._calls.get(label, 0)) + _nonnegative_int(count)
            self._event()

    def record_model(
        self,
        role: str,
        *,
        prompt_chars: int = 0,
        output_chars: int = 0,
        retries: int = 0,
    ) -> None:
        """Record model aggregate sizes and calls, never model content."""
        with self._lock:
            self._model["call_count"] += 1
            self._model["retry_count"] += _nonnegative_int(retries)
            self._model["prompt_chars"] += _nonnegative_int(prompt_chars)
            self._model["output_chars"] += _nonnegative_int(output_chars)
            label = _name(role)
            roles = self._model["roles"]
            roles[label] = _nonnegative_int(roles.get(label, 0)) + 1
            self._event()

    def record_verification(
        self,
        *,
        invocations: int = 0,
        shell_starts: int = 0,
        commands: int = 0,
        copied_bytes: int = 0,
    ) -> None:
        """Accumulate verify-gate counts used by later optimization waves."""
        with self._lock:
            for key, value in (
                ("gate_invocations", invocations),
                ("shell_starts", shell_starts),
                ("commands", commands),
                ("copied_bytes", copied_bytes),
            ):
                self._verification[key] += _nonnegative_int(value)
            self._event()

    def record_context(self, source: str, *, hit: bool, latency_ms: float = 0.0) -> None:
        """Record source hit/miss and latency without retaining context text."""
        label = _name(source)
        with self._lock:
            stats = self._context.setdefault(label, {"hits": 0, "misses": 0, "latency_ms": 0.0})
            stats["hits" if hit else "misses"] += 1
            stats["latency_ms"] = round(
                _nonnegative_float(stats["latency_ms"]) + _nonnegative_float(latency_ms), 3
            )
            self._event()

    def record_crew(self, event: str) -> None:
        """Record a bounded crew lifecycle event by category."""
        key = {
            "spawn": "spawn_count",
            "poll": "poll_count",
            "blocked": "blocked_wait_count",
            "fallback": "fallback_count",
            "teardown": "teardown_count",
            "in_flight": "in_flight_count",
        }.get(_name(event))
        with self._lock:
            if key:
                self._crew[key] += 1
            self._event()

    def record_quality(
        self,
        *,
        accepted: Optional[bool],
        critical_findings: int = 0,
        retry_count: int = 0,
    ) -> None:
        """Record the issue-level quality outcome, not review prose."""
        with self._lock:
            self._quality["accepted"] = bool(accepted) if accepted is not None else None
            self._quality["critical_findings"] = _nonnegative_int(critical_findings)
            self._quality["retry_count"] = _nonnegative_int(retry_count)
            self._event()

    def snapshot(self) -> dict:
        """Return a JSON-safe, bounded measurement packet."""
        with self._lock:
            timings = dict(self._timings)
            timings.setdefault("total", round((time.perf_counter() - self._started) * 1000.0, 3))
            context = {
                source: {
                    "hits": _nonnegative_int(stats.get("hits")),
                    "misses": _nonnegative_int(stats.get("misses")),
                    "latency_ms": round(_nonnegative_float(stats.get("latency_ms")), 3),
                }
                for source, stats in list(self._context.items())[:16]
            }
            model = dict(self._model)
            model["roles"] = dict(list(model["roles"].items())[:16])
            return {
                "schema_version": SCHEMA_VERSION,
                "events": min(self._event_count, self._max_events),
                "calls": dict(list(self._calls.items())[:16]),
                "timings_ms": timings,
                "model": model,
                "verification": dict(self._verification),
                "context": {"sources": context},
                "crew": dict(self._crew),
                "quality": dict(self._quality),
            }
