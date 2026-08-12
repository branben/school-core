#!/usr/bin/env python3
"""Sanitize runtime data files before they are committed to git.

Rewrites absolute home-directory paths (``/Users/<username>/...``) to the
portable ``~/...`` form so board state can be made durable in git without
leaking PII (the macOS username + home layout).

Why this is safe: the board renderer only uses ``Path(trajectory).name``
(basename) for the session deep-link, and the activity server serves
trajectories by basename only — so a scrubbed path renders and serves
identically to the absolute one.

Usage:
    python scripts/sanitize_data.py data/last_run.json data/issues_cache.json ...
"""

# Python 3.9 (the runner's venv is built from macOS /usr/bin/python3 = 3.9.6)
# evaluates `Path | None` annotations at runtime; this defers them so the
# sanitizer survives the checkpoint step (2026-08-12).
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

# 1) Any absolute repo-root prefix ending in /school-core/data/ → relative data/
#    Handles both the local Mac (/Users/<name>/school-core/data/...) and the
#    GitHub runner (/home/runner/work/school-core/school-core/data/...) layouts.
REPO_PREFIX_RE = re.compile(r"(?:/[A-Za-z0-9_.~-]+)+/school-core/data/")

# Default cap for committed trajectory history (U2). Trajectory filenames are
# timestamp-prefixed (YYYYmmdd_HHMMSS_ffffff), so the newest N by name = the
# last N cycles. Keeps git history bounded while preserving Layer 2 memory.
DEFAULT_TRAJECTORY_KEEP = 60
# Layer 3 consolidation is also runtime-generated. Keep a bounded number of
# session artifacts so durable context does not grow without limit.
DEFAULT_CONSOLIDATION_KEEP = 60

# 2) Any remaining /Users/<username> prefix → ~  (removes username + home layout)
HOME_RE = re.compile(r"/Users/[A-Za-z0-9_.-]+")

# Runtime observations are untrusted. Redact sensitive fields before any
# durable checkpoint, even when they arrive inside YAML/text artifacts.
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|auth|secret|password|credential|private[_-]?key)"
)
SESSION_ID_RE = re.compile(r"^loop-\d{8}-\d{6}$")
SENSITIVE_LINE_RE = re.compile(
    r"(?im)^(\s*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|auth|secret|password|credential)[^:]*:\s*).*$"
)
TOKEN_RE = re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{12,})\b")


def _scrub_text(value: str) -> str:
    value = REPO_PREFIX_RE.sub("data/", value)
    value = HOME_RE.sub("~", value)
    value = SENSITIVE_LINE_RE.sub(r"\1[REDACTED]", value)
    return TOKEN_RE.sub("[REDACTED]", value)


def scrub_value(value):
    """Recursively replace paths and sensitive values in JSON-like data."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if SENSITIVE_KEY_RE.match(str(k).strip()) else scrub_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def trim_trajectories(keep: int = DEFAULT_TRAJECTORY_KEEP, traj_dir: Path | None = None) -> int:
    """Delete all but the newest *keep* trajectory files. Returns count removed.

    Trajectory filenames carry a UTC timestamp prefix, so lexicographic order
    == chronological order. Called by the school-loop checkpoint before the
    sanitize + git add -f step (U2), so a fresh checkout sees the last N cycles
    of Layer 2 history without unbounded repo growth.

    *traj_dir* is injectable for tests; defaults to <repo>/data/trajectories.
    """
    # files[:-0] == files[:0] == [] silently keeps everything — a keep=0 (or
    # negative) is a footgun, not a mode. Clamp to a sane minimum.
    keep = max(1, int(keep))
    traj_dir = Path(traj_dir) if traj_dir else Path(__file__).parent.parent / "data" / "trajectories"
    if not traj_dir.exists():
        return 0
    files = sorted(traj_dir.glob("*.json"))
    if len(files) <= keep:
        return 0
    removed = 0
    for f in files[:-keep]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def trim_consolidations(keep: int = DEFAULT_CONSOLIDATION_KEEP, consolidation_dir: Path | None = None) -> int:
    """Delete all but the newest *keep* Layer 3 YAML artifacts.

    Consolidations are grouped by session directory. Sorting by the relative
    path keeps the timestamped loop-session naming deterministic while allowing
    multiple domains in one session to survive together. The helper is
    injectable for tests and never raises for a missing directory.
    """
    keep = max(1, int(keep))
    consolidation_dir = (
        Path(consolidation_dir)
        if consolidation_dir
        else Path(__file__).parent.parent / "data" / "sessions" / "consolidation"
    )
    if not consolidation_dir.exists():
        return 0
    files = sorted(consolidation_dir.glob("*/*.yaml"))
    # Only timestamped loop sessions participate in retention. Preserve
    # malformed/manual directories rather than deleting unknown state.
    sessions = sorted({path.parent.name for path in files if SESSION_ID_RE.fullmatch(path.parent.name)})
    if len(sessions) <= keep:
        return 0
    stale_sessions = set(sessions[:-keep])
    removed = 0
    for path in files:
        if path.parent.name not in stale_sessions:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    for session in stale_sessions:
        session_dir = consolidation_dir / session
        try:
            session_dir.rmdir()
        except OSError:
            pass
    return removed


def sanitize_file(path: Path) -> int:
    """Sanitize one JSON or YAML file in place. Returns replacement count."""
    try:
        raw = path.read_text()
    except OSError as e:
        print(f"[sanitize] SKIP {path}: {e}")
        return 0

    hits_before = (
        len(HOME_RE.findall(raw))
        + len(REPO_PREFIX_RE.findall(raw))
        + len(SENSITIVE_LINE_RE.findall(raw))
        + len(TOKEN_RE.findall(raw))
    )
    if hits_before == 0:
        return 0

    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(raw)
            data = scrub_value(data)
            path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        else:
            data = json.loads(raw)
            data = scrub_value(data)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except (json.JSONDecodeError, yaml.YAMLError, TypeError):
        # Not structured data we own — fall back to line-level replacement,
        # preserving the rest of the file.
        cleaned = _scrub_text(raw)
        path.write_text(cleaned)
    print(f"[sanitize] {path}: {hits_before} sensitive/path matches scrubbed")
    return hits_before


def unsafe_text(path: Path) -> list[str]:
    """Return common secret indicators still present after sanitization."""
    try:
        text = path.read_text()
    except OSError:
        return []
    findings = []
    for pattern, label in ((SENSITIVE_LINE_RE, "sensitive field"), (TOKEN_RE, "token"), (HOME_RE, "home path")):
        if pattern.search(text):
            findings.append(label)
    return findings


def verify_consolidations_safe(consolidation_dir: Path | None = None) -> list[tuple[Path, list[str]]]:
    """Check all checkpoint YAMLs for residual secret/path indicators."""
    root = Path(consolidation_dir) if consolidation_dir else Path(__file__).parent.parent / "data" / "sessions" / "consolidation"
    findings = []
    if not root.exists():
        return findings
    for path in sorted(root.glob("*/*.yaml")):
        residual = unsafe_text(path)
        if residual:
            findings.append((path, residual))
    return findings


def main() -> int:
    args = list(sys.argv[1:])
    # U2: `--trim-trajectories [N]` trims the trajectory dir to the newest N
    # files (default DEFAULT_TRAJECTORY_KEEP) before sanitizing, then sanitizes
    # each remaining trajectory alongside the explicit file args.
    trim_keep = None
    if "--trim-consolidations" in args:
        idx = args.index("--trim-consolidations")
        del args[idx]
        consolidation_keep = DEFAULT_CONSOLIDATION_KEEP
        if idx < len(args) and not args[idx].startswith("--"):
            try:
                consolidation_keep = int(args[idx])
                del args[idx]
            except ValueError:
                pass
        removed = trim_consolidations(keep=consolidation_keep)
        print(f"[sanitize] consolidations trimmed: removed {removed}")
        consolidation_dir = Path(__file__).parent.parent / "data" / "sessions" / "consolidation"
        if consolidation_dir.exists():
            args += sorted(str(p) for p in consolidation_dir.glob("*/*.yaml"))

    trim_keep = None
    if "--trim-trajectories" in args:
        idx = args.index("--trim-trajectories")
        del args[idx]
        if idx < len(args) and not args[idx].startswith("--"):
            try:
                trim_keep = int(args[idx])
                del args[idx]
            except ValueError:
                pass
        if trim_keep is None:
            trim_keep = DEFAULT_TRAJECTORY_KEEP
        removed = trim_trajectories(keep=trim_keep)
        print(f"[sanitize] trajectories trimmed: removed {removed}")
        # Sanitize whatever remains so committed trajectories are PII-free.
        traj_dir = Path(__file__).parent.parent / "data" / "trajectories"
        if traj_dir.exists():
            args += sorted(str(p) for p in traj_dir.glob("*.json"))

    if not args:
        print("usage: python scripts/sanitize_data.py [--trim-trajectories [N]] [--trim-consolidations [N]] <file.json> [...]")
        return 2
    total = 0
    for arg in args:
        total += sanitize_file(Path(arg))
    findings = verify_consolidations_safe()
    if findings:
        for path, labels in findings:
            print(f"[sanitize] UNSAFE {path}: {', '.join(labels)}", file=sys.stderr)
        return 1
    print(f"[sanitize] done: {total} paths scrubbed across {len(args)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
