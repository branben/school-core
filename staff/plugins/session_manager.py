from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

from staff.plugin import StaffPlugin, StaffContext, StaffResult, PluginTrust
from staff.sandbox import StaffSandbox


class SessionManagerPlugin(StaffPlugin):
    """Monitors active sessions and orchestrates sleep/wake cycles.

    First production consumer of the sleep/wake protocol (issue #002).
    Scans Director's _active_sessions, triggers sleep for timed-out sessions,
    and reports session health metrics.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._timeout_minutes = self._config.get("timeout_minutes", 15)

    @property
    def name(self) -> str:
        return "session-manager"

    @property
    def trust(self) -> PluginTrust:
        explicit = self._config.get("trust")
        if explicit:
            return PluginTrust(explicit)
        return PluginTrust.CORE

    def health_check(self) -> dict:
        return {
            "session_read": "available",
            "sleep_trigger": "available",
            "wake_trigger": "available",
            "library_log_read": "available",
        }

    def run(self, sandbox: StaffSandbox, context: StaffContext) -> StaffResult:
        from director import _active_sessions, sleep, SLEEP_TIMEOUT_MINUTES
        from sleep_state import read_library_log

        timeout = self._timeout_minutes or SLEEP_TIMEOUT_MINUTES
        now = datetime.now(timezone.utc)

        # Read recent sleep events to avoid re-sleeping
        try:
            log_entries = read_library_log()
        except Exception:
            log_entries = []

        recently_slept = set()
        for entry in log_entries[-20:]:
            if entry.get("event") == "sleep" and entry.get("timestamp"):
                recently_slept.add(entry.get("session_id"))

        active_count = 0
        sleep_triggered = 0
        sessions_scanned = 0
        errors = []

        for session_id, info in _active_sessions.items():
            sessions_scanned += 1
            active_count += 1

            if session_id in recently_slept:
                continue

            last_activity = info.get("last_activity", "")
            if not last_activity:
                continue

            try:
                last_dt = datetime.fromisoformat(last_activity)
                elapsed = (now - last_dt).total_seconds() / 60
            except (ValueError, TypeError):
                continue

            if elapsed >= timeout:
                try:
                    sleep(
                        session_id=session_id,
                        agent=info.get("agent", "unknown"),
                        store=context.score_store,
                    )
                    sleep_triggered += 1
                except Exception as e:
                    errors.append(f"Failed to sleep {session_id}: {e}")

        status = "success" if not errors else "degraded"
        summary = (
            f"Scanned {sessions_scanned} sessions, "
            f"{active_count} active, {sleep_triggered} slept"
        )
        if errors:
            summary += f", {len(errors)} errors"

        return StaffResult(
            plugin_name=self.name,
            status=status,
            summary=summary,
            score_recommendations=[],
            vault_writes=[],
            metrics={
                "active_count": active_count,
                "sleep_triggered_count": sleep_triggered,
                "sessions_scanned": sessions_scanned,
                "timeout_minutes": timeout,
            },
        )
