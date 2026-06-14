from __future__ import annotations

import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class PluginTrust(Enum):
    CORE = "core"
    VERIFIED = "verified"
    COMMUNITY = "community"
    UNTRUSTED = "untrusted"


@dataclass
class StaffContext:
    vault_path: str
    score_store: object  # ScoreStore — typed as object to avoid circular import
    engram_available: bool
    cocoindex_available: bool
    building: str
    config: dict = field(default_factory=dict)


@dataclass
class StaffResult:
    plugin_name: str
    status: str  # "success" | "degraded" | "error"
    summary: str
    score_recommendations: list = field(default_factory=list)
    vault_writes: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class StaffPlugin(ABC):
    """Base class for all Staff plugins.

    Staff plugins operate on the School's own state (memory hygiene,
    score auditing, knowledge management) rather than on user tasks.
    They run on a schedule or on system events, not on task routing.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def trust(self) -> PluginTrust: ...

    @abstractmethod
    def health_check(self) -> dict:
        """Return capability availability. Dict of {capability: status}."""

    @abstractmethod
    def run(self, sandbox: "StaffSandbox", context: StaffContext) -> StaffResult: ...
