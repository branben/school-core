#!/usr/bin/env python3
"""Shared helpers for school-core — extracted to end duplication across
conductor.py, director.py, issue_bridge.py, crew_dispatch.py, orca_executor.py, etc."""

import json
import os
import subprocess
import sys
from pathlib import Path

BOOKBAG_BASE = Path.home() / ".hermes" / "bookbag"
REPO_GLOBAL = "__global__"


def run_cmd(args, timeout=30, check=True, capture=True):
    """Run a subprocess, return stdout. Raises on non-zero exit (if check)."""
    result = subprocess.run(
        args,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"stderr: {result.stderr.strip()[:300]}"
        )
    return result.stdout.strip() if capture else None


def log(msg):
    """Write a timestamped message to stderr."""
    sys.stderr.write(f"[school-core] {msg}\n")


def read_json(path):
    """Read a JSON file. Returns {} if missing or invalid."""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_json(path, data, indent=2):
    """Write a JSON file atomically (tmp + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    tmp.replace(path)


def bookbag_dir(repo):
    """Return the bookbag directory path for a repo namespace."""
    if repo == REPO_GLOBAL:
        return BOOKBAG_BASE
    return BOOKBAG_BASE / repo.replace("/", "__")


def bead_path(bead, repo=REPO_GLOBAL):
    """Return the full path to a bookbag JSON file."""
    return bookbag_dir(repo) / f"{bead}.json"
