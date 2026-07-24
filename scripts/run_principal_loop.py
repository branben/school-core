#!/usr/bin/env python3
"""Persistent principal loop launcher.

Run inside the principal's Orca terminal by `conductor.py --serve`:
    python3 /path/to/run_principal_loop.py

Keeps the Agent School principal orchestrating indefinitely (booting
teachers, dispatching leaves, polling for two-judge verdicts) so the
orchestrator survives beyond a single foreground `conductor.py` invocation.
"""
import sys

from conductor import main

if __name__ == "__main__":
    # Simulate argv for an infinite async principal loop.
    sys.argv = [
        "conductor.py",
        "--loop",
        "--async",
        "--rounds",
        "999999",
        "--handoff-timeout",
        "300",
    ]
    main()
