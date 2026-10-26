"""Dependency-free dotenv reading.

The project does not use python-dotenv for these reads on purpose: reading a
value must not mutate ``os.environ``. ``load_dotenv`` does mutate it, and
calling it at import time (as ``crew_dispatch`` used to) made pytest's
collection order decide whether a test saw a key the developer never
exported -- see school-core-3r4n.
"""

from pathlib import Path


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
