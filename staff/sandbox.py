from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from staff.plugin import PluginTrust

# Write directories plugins are allowed to write to
ALLOWED_WRITE_DIRS = ["engram/", "archives/", "staff-output/"]
ALLOWED_EXTENSIONS = {".md", ".yaml", ".json", ".txt"}
MAX_FILE_SIZE = 50_000  # 50KB max per write


@dataclass
class StaffSandbox:
    trust: PluginTrust
    vault_path: str
    _reads: int = 0
    _writes: int = 0

    _LIMITS = {
        PluginTrust.CORE:      {"reads": None, "writes": None,  "scores": True,  "prompts": True},
        PluginTrust.VERIFIED:  {"reads": 500,  "writes": 50,    "scores": True,  "prompts": False},
        PluginTrust.COMMUNITY: {"reads": 100,  "writes": 5,     "scores": False, "prompts": False},
        PluginTrust.UNTRUSTED: {"reads": 20,   "writes": 0,     "scores": False, "prompts": False},
    }

    def _limit(self, key):
        return self._LIMITS[self.trust].get(key)

    @property
    def can_modify_scores(self) -> bool:
        return self._LIMITS[self.trust]["scores"]

    @property
    def can_write_vault(self) -> bool:
        return self._LIMITS[self.trust]["writes"] > 0

    @property
    def can_execute_prompts(self) -> bool:
        return self._LIMITS[self.trust]["prompts"]

    def read_vault(self, path: str) -> str:
        limit = self._limit("reads")
        if limit is not None and self._reads >= limit:
            raise SandboxError(f"Read limit exceeded ({limit})")
        p = Path(self.vault_path) / path
        if not p.exists():
            raise SandboxError(f"Path not found: {path}")
        self._reads += 1
        return p.read_text()

    def write_vault(self, path: str, content: str) -> None:
        if not self.can_write_vault:
            raise SandboxError(f"trust={self.trust.value} cannot write vault")
        limit = self._limit("writes")
        if limit is not None and self._writes >= limit:
            raise SandboxError(f"Write limit exceeded ({limit})")
        p = Path(path)
        if p.suffix not in ALLOWED_EXTENSIONS:
            raise SandboxError(f"Extension {p.suffix} not allowed")
        rel = str(p.relative_to(p.anchor)) if p.is_absolute() else str(p)
        if not any(rel.startswith(d) for d in ALLOWED_WRITE_DIRS):
            raise SandboxError(f"Write to {rel} not allowed — must be under {ALLOWED_WRITE_DIRS}")
        if len(content) > MAX_FILE_SIZE:
            raise SandboxError(f"Content exceeds {MAX_FILE_SIZE}B limit")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self._writes += 1

    def get_stats(self) -> dict:
        return {"reads": self._reads, "writes": self._writes, "trust": self.trust.value}


class SandboxError(Exception):
    pass
