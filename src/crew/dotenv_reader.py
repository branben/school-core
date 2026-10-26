"""Dependency-free dotenv reading.

The project does not use python-dotenv for these reads on purpose: reading a
value must not mutate ``os.environ``. ``load_dotenv`` does mutate it, and
calling it at import time (as ``crew_dispatch`` used to) made pytest's
collection order decide whether a test saw a key the developer never
exported -- see school-core-3r4n.
"""

import os
from pathlib import Path
from typing import Optional


def read_dotenv_value(path: Path, name: str) -> str:
    """Best-effort read of a ``NAME=VALUE`` line from a dotenv file.

    Skips comments and blank lines; strips optional surrounding quotes.
    Returns ``""`` when the file is missing, unreadable, or lacks the key.
    """
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def repo_root() -> Path:
    """The school-core repo root (``src/crew/dotenv_reader.py`` -> 3 up)."""
    return Path(__file__).resolve().parent.parent.parent


def repo_env_file() -> Path:
    """The repo-root ``.env`` -- the project's secrets store."""
    return repo_root() / ".env"


def env_or_file(name: str, default: str = "", env_path: Optional[Path] = None) -> str:
    """Resolve ``name`` from ``os.environ``, then the repo ``.env`` file.

    This is the read-side counterpart to ``read_dotenv_value`` and exists for
    settings that used to reach ``.env`` only because *some other module* ran
    ``load_dotenv`` at import time. It preserves the previous resolution order
    (the live environment always wins) WITHOUT mutating ``os.environ``, so
    importing a module never invents credentials for the rest of the process.

    Returns ``default`` when the key is set in neither place.
    """
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    return read_dotenv_value(env_path or repo_env_file(), name) or default
