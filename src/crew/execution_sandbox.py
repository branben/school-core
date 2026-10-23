"""macOS Seatbelt profile helpers for the verify gate.

Apple marks sandbox-exec deprecated. It is available only on macOS; callers
must fail closed if it is missing. Nix shells provide tools but are not runtime
sandboxes.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def sandbox_exec_path() -> str | None:
    """Return sandbox-exec only on macOS."""
    return shutil.which("sandbox-exec") if sys.platform == "darwin" else None


def _sbpl_string(value: str) -> str:
    """Quote a filesystem path for an SBPL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_sandbox_profile(
    path: Path,
    *,
    writable_paths: list[Path],
    allow_network: bool,
    denied_read_paths: list[Path] | None = None,
    deny_home: bool = False,
    confine_reads: bool = False,
    readable_paths: list[Path] | None = None,
) -> Path:
    """Create a profile with optional read confinement and scratch-only writes."""
    writable = sorted({str(Path(item).resolve()) for item in writable_paths})
    if not writable:
        raise ValueError("sandbox profile requires at least one writable path")

    lines = ["(version 1)", "(allow default)"]
    if confine_reads:
        allowed_reads = sorted({str(Path(item).resolve()) for item in (readable_paths or []) + writable_paths})
        if not allowed_reads:
            raise ValueError("read confinement requires at least one readable path")
        lines.append("(deny file-read*)")
        lines.extend(f"(allow file-read* (subpath {_sbpl_string(item)}))" for item in allowed_reads)
    else:
        if deny_home:
            lines.append(f"(deny file-read* (subpath {_sbpl_string(str(Path.home().resolve()))}))")
        lines.extend(
            f"(deny file-read* (subpath {_sbpl_string(str(Path(item).resolve()))}))"
            for item in (denied_read_paths or [])
        )
    lines.append("(deny file-write*)")
    lines.extend(
        f"(allow file-write* (subpath {_sbpl_string(item)}))"
        for item in writable
    )
    lines.append("(allow network*)" if allow_network else "(deny network*)")

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n")
    os.chmod(output, 0o600)
    return output
