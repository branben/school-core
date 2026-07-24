#!/usr/bin/env python3
"""Launcher for a persistent TeacherWorktree review loop.

Invoked by the conductor inside the teacher's Orca worktree terminal:
    python3 /path/to/run_teacher_loop.py <role>

Using a script file (instead of `python3 -c "..."` with nested escaped
quotes) avoids the quoting mangling that Orca's `terminal send` does to
inline `-c` commands, which previously produced empty/dead teacher
terminals.
"""
import sys

from teacher import TeacherWorktree


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: run_teacher_loop.py <role>", file=sys.stderr)
        sys.exit(2)
    role = sys.argv[1]
    teacher = TeacherWorktree(role)
    teacher.boot()
    teacher.run_loop()


if __name__ == "__main__":
    main()
