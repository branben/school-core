from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from staff.plugin import PluginTrust


@dataclass
class ScoreRecommendation:
    agent: str
    domain: str
    suggested_delta: float
    reason: str
    source_plugin: str


@dataclass
class StaffSchedule:
    plugin_name: str
    cron: Optional[str] = None  # cron expression
    interval_seconds: Optional[int] = None  # or fixed interval
    on_events: list = field(default_factory=list)  # system event triggers


class StaffLoader:
    """Discovers and loads Staff plugins."""

    def __init__(self, school_root: str = None):
        self.school_root = Path(school_root) if school_root else Path(__file__).parent.parent
        self.plugins: dict = {}
        self.schedules: dict = {}

    def discover(self, config: dict = None) -> dict:
        config = config or {}
        plugins_dir = self.school_root / "staff" / "plugins"
        if not plugins_dir.exists():
            return self.plugins

        for plugin_file in plugins_dir.glob("*.py"):
            if plugin_file.name.startswith("_"):
                continue
            try:
                plugin = self._load_plugin(plugin_file, config.get(plugin_file.stem, {}))
                if plugin:
                    self.plugins[plugin.name] = plugin
                    self.schedules[plugin.name] = config.get(plugin.name, {})
            except Exception as e:
                sys.stderr.write(f"[staff] Failed to load {plugin_file.name}: {e}\n")

        return self.plugins

    def _load_plugin(self, path: str, config: dict) -> Optional[object]:
        import importlib.util
        from staff.plugin import StaffPlugin
        spec = importlib.util.spec_from_file_location("staff_plugin", path)
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr_name in dir(module):
            if attr_name.startswith("_") or attr_name == "StaffPlugin":
                continue
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, StaffPlugin) and attr is not StaffPlugin:
                return attr(config=config)
        return None
